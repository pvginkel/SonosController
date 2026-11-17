"""The SoCo client against the real speakers: what the fake cannot prove."""

import re
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

import pytest

from app.core_exceptions import ExternalServiceException
from app.exceptions import CommandRefusedException
from app.services.health_service import CheckStatus, HealthService
from app.services.plex_client import PlexapiPlexClient
from app.services.sonos_client import (
    UNREPORTED,
    FetchRefusedException,
    GroupVolumeChanged,
    HouseholdChanged,
    Playback,
    PlaybackChanged,
    PlexItem,
    PlexItemKind,
    QueueChanged,
    QueuePage,
    SoCoSonosClient,
    SonosService,
    Track,
    TransportAction,
    TransportState,
    Volume,
    VolumeChanged,
)
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist
from app.utils.plex_queue_entry import parse_plex_queue_entry

pytestmark = pytest.mark.live

LOW_VOLUME = 3

# An event has been seen to take 20 seconds from the speaker to the listener.
EVENT_TIMEOUT_SECONDS = 60.0


@pytest.fixture
def quiet(sonos: SoCoSonosClient, garage: str) -> Generator[Volume]:
    """The Garage at low volume for one test, then as it was found."""
    found = sonos.read_volume(garage)
    sonos.set_volume(garage, min(found.volume, LOW_VOLUME))
    try:
        yield found
    finally:
        sonos.set_muted(garage, found.muted)
        sonos.set_volume(garage, found.volume)
        assert sonos.read_volume(garage) == found


def _wait_for(read: Callable[[], Any], accept: Callable[[Any], bool]) -> Any:
    """Read until the speaker shows what a command asked for."""
    deadline = time.perf_counter() + 15.0
    while True:
        value = read()
        if accept(value) or time.perf_counter() >= deadline:
            return value
        time.sleep(0.2)


class _Listener:
    """Keeps what a subscription delivers until a test takes it."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events: list[Any] = []
        self.lost = 0

    def on_event(self, event: Any) -> None:
        with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    def on_lost(self) -> None:
        self.lost += 1

    def take(self, accept: Callable[[Any], bool]) -> Any:
        """The first event that ``accept`` takes; fails when none arrives."""
        deadline = time.perf_counter() + EVENT_TIMEOUT_SECONDS
        with self._condition:
            while True:
                for index, event in enumerate(self._events):
                    if accept(event):
                        return self._events.pop(index)
                remaining = deadline - time.perf_counter()
                if remaining <= 0 or not self._condition.wait(remaining):
                    pytest.fail(
                        f"No such event arrived within {EVENT_TIMEOUT_SECONDS:g} s; "
                        f"these did: {self._events}. The speakers must reach "
                        "SONOS_EVENT_ADVERTISE_HOST on SONOS_EVENT_PORT."
                    )


class TestLiveHousehold:
    def test_the_layout_lists_rooms_not_devices(self, sonos, garage):
        household = sonos.read_household()

        ids = [room.id for room in household.rooms]
        names = [room.name for room in household.rooms]
        assert garage in ids
        assert ids == sorted(set(ids))
        assert all(speaker_id.startswith("RINCON_") for speaker_id in ids)
        # A room's hidden devices carry the room's name.
        assert len(names) == len(set(names))
        for room in household.rooms:
            IPv4Address(room.address)
            assert room.boot_seq > 0
        members = [m for group in household.groups for m in group.member_ids]
        assert sorted(members) == ids
        for group in household.groups:
            assert group.coordinator_id in group.member_ids

    def test_the_readiness_check_passes_on_a_seed_that_answers(self, sonos):
        assert sonos.check_readiness().status is CheckStatus.OK

    def test_readyz_passes_when_the_seed_asked_first_does_not_answer(
        self, app, live_settings
    ):
        health = HealthService(
            lifecycle_coordinator=app.container.lifecycle_coordinator(),
            settings=app.container.config(),
        )
        # 192.0.2.1 is of a range no host has: asked first, it never answers.
        SoCoSonosClient(
            settings=live_settings.model_copy(
                update={
                    "sonos_seed_hosts": [
                        "192.0.2.1",
                        *live_settings.sonos_seed_hosts,
                    ]
                }
            ),
            health_service=health,
        )

        with app.app_context():
            body, status = health.check_readyz()

        assert status == 200
        assert body["checks"] == {"sonos": {"status": "ok", "critical": False}}

    def test_every_room_can_be_read(self, sonos):
        for room in sonos.read_household().rooms:
            model = sonos.read_model(room.id)
            assert model and not model.startswith("Sonos")
            for volume in (
                sonos.read_volume(room.id),
                sonos.read_group_volume(room.id),
            ):
                assert 0 <= volume.volume <= 100
                assert isinstance(volume.muted, bool)
            assert isinstance(sonos.read_playback(room.id), Playback)

    def test_the_garages_playback_is_its_queue(self, sonos, garage):
        playback = sonos.read_playback(garage)

        assert playback.transport_state is TransportState.STOPPED
        assert playback.transport_uri == f"x-rincon-queue:{garage}#0"
        assert playback.transport_title == ""
        assert {TransportAction.PLAY, TransportAction.STOP} <= playback.actions
        assert 1 <= playback.queue_position <= playback.queue_length
        assert playback.track.title
        assert playback.track.uri
        assert playback.track.duration_seconds > 0

    def test_an_unknown_speaker_fails(self, sonos):
        with pytest.raises(ExternalServiceException, match="is not in the household"):
            sonos.read_volume("RINCON_000000000000001400")


class TestLiveVolume:
    def test_the_rooms_volume_round_trip(self, sonos, garage, quiet):
        sonos.set_volume(garage, 2)
        assert sonos.read_volume(garage).volume == 2

        assert sonos.change_volume(garage, 1) == 3
        assert sonos.read_volume(garage).volume == 3

        assert sonos.change_volume(garage, -100) == 0

        sonos.set_muted(garage, True)
        assert sonos.read_volume(garage).muted is True
        sonos.set_muted(garage, False)
        assert sonos.read_volume(garage).muted is False

    def test_the_group_volume_of_a_room_alone_moves_the_room(
        self, sonos, garage, quiet
    ):
        sonos.set_group_volume(garage, 2)
        assert sonos.read_group_volume(garage).volume == 2
        assert sonos.read_volume(garage).volume == 2

        assert sonos.change_group_volume(garage, 1) == 3
        assert sonos.read_volume(garage).volume == 3

        sonos.set_group_muted(garage, True)
        assert sonos.read_group_volume(garage).muted is True
        assert sonos.read_volume(garage).muted is True
        sonos.set_group_muted(garage, False)
        assert sonos.read_group_volume(garage).muted is False


class TestLiveTransport:
    def test_the_transport_round_trip(self, sonos, garage, quiet):
        found = sonos.read_playback(garage)
        forward = TransportAction.NEXT in found.actions
        there, back = (
            (sonos.next, sonos.previous) if forward else (sonos.previous, sonos.next)
        )
        elsewhere = found.queue_position + (1 if forward else -1)

        def state_is(state: TransportState) -> Playback:
            return _wait_for(
                lambda: sonos.read_playback(garage),
                lambda playback: playback.transport_state is state,
            )

        def position_is(position: int) -> Playback:
            return _wait_for(
                lambda: sonos.read_playback(garage),
                lambda playback: playback.queue_position == position,
            )

        try:
            sonos.play(garage)
            assert state_is(TransportState.PLAYING).transport_state is (
                TransportState.PLAYING
            )

            there(garage)
            moved = position_is(elsewhere)
            assert moved.queue_position == elsewhere
            assert moved.track.uri != found.track.uri

            # The speaker serves a track's picture whatever plays.
            assert moved.track.picture != found.track.picture
            assert sonos.read_picture(garage, found.track.picture).startswith(
                b"\xff\xd8"
            )

            sonos.pause(garage)
            assert state_is(TransportState.PAUSED).transport_state is (
                TransportState.PAUSED
            )

            back(garage)
            assert position_is(found.queue_position).track.uri == found.track.uri
        finally:
            sonos.stop(garage)
            left = position_is(found.queue_position)
            if left.queue_position != found.queue_position:
                back(garage)
                sonos.stop(garage)

        left = state_is(TransportState.STOPPED)
        assert left.transport_state is TransportState.STOPPED
        assert left.transport_uri == found.transport_uri
        assert (left.queue_position, left.queue_length) == (
            found.queue_position,
            found.queue_length,
        )
        assert left.track == found.track
        assert left.shuffle == found.shuffle

    def test_a_command_the_speaker_refuses_is_a_refusal_in_the_clients_words(
        self, sonos, garage
    ):
        found = sonos.read_playback(garage)
        refused = [
            command
            for action, command in (
                (TransportAction.PREVIOUS, sonos.previous),
                (TransportAction.NEXT, sonos.next),
            )
            if action not in found.actions
        ]
        assert refused, (
            "The Garage allows both next and previous, so it refuses neither: "
            "put its queue at the first or the last track and stop it."
        )

        with pytest.raises(CommandRefusedException) as raised:
            refused[0](garage)

        # The words are the client's: SoCo's own name the speaker's address.
        # A previous at a queue's first track is answered 711 (Illegal seek
        # target), a next on a radio stream 701 (docs/api-design.md 1.7).
        assert re.fullmatch(
            r"the speaker answered UPnP error "
            r"(701 \(Transition not available\)|711 \(Illegal seek target\))",
            raised.value.cause,
        )
        assert sonos.read_playback(garage) == found

    def test_a_picture_the_speaker_does_not_serve_fails_in_the_clients_words(
        self, sonos, garage
    ):
        with pytest.raises(ExternalServiceException) as raised:
            sonos.read_picture(garage, "/no-such-picture")

        # The words are the client's: requests' own name the speaker's address.
        assert raised.value.message == (
            f"Cannot read the picture speaker {garage} serves because external "
            "service failed: the speaker answered 404"
        )

    def test_the_speaker_serves_the_tracks_picture(self, sonos, garage):
        picture = sonos.read_playback(garage).track.picture
        assert picture.startswith("/getaa?")

        content = sonos.read_picture(garage, picture)

        assert content.startswith(b"\xff\xd8")


class TestLiveSubscriptions:
    def test_each_service_sends_its_state_in_full_at_once(self, sonos, garage):
        listener = _Listener()
        subscriptions = [
            sonos.subscribe(garage, service, listener.on_event, listener.on_lost)
            for service in SonosService
        ]
        try:
            playback = listener.take(lambda e: isinstance(e, PlaybackChanged))
            volume = listener.take(lambda e: isinstance(e, VolumeChanged))
            group = listener.take(lambda e: isinstance(e, GroupVolumeChanged))
            queue = listener.take(lambda e: isinstance(e, QueueChanged))
            layout = listener.take(lambda e: isinstance(e, HouseholdChanged))
        finally:
            for subscription in subscriptions:
                subscription.unsubscribe()

        read = sonos.read_playback(garage)
        assert playback == PlaybackChanged(
            garage,
            transport_state=read.transport_state,
            transport_uri=read.transport_uri,
            transport_title=read.transport_title,
            actions=read.actions,
            shuffle=read.shuffle,
            queue_position=read.queue_position,
            queue_length=read.queue_length,
            track=read.track,
        )
        assert playback.track is not UNREPORTED
        assert volume == VolumeChanged(
            garage,
            volume=sonos.read_volume(garage).volume,
            muted=sonos.read_volume(garage).muted,
        )
        # The speaker has been seen to send a group volume here that the room
        # had left behind: only the read is held to be current.
        assert group.speaker_id == garage
        assert 0 <= group.volume <= 100
        assert group.muted is False
        assert queue == QueueChanged(garage)
        assert layout == HouseholdChanged(garage, sonos.read_household())
        assert listener.lost == 0

    def test_the_speaker_grants_a_lease_of_minutes(self, sonos, garage):
        # A subscription left behind whose callback does not answer holds up
        # the speaker's notifications to every subscriber until its lease runs
        # out: 24 hours when none is asked.
        listener = _Listener()
        subscription = sonos.subscribe(
            garage, SonosService.RENDERING_CONTROL, listener.on_event, listener.on_lost
        )
        try:
            lease = subscription.lease_seconds
            subscription.renew()
            renewed = subscription.lease_seconds
        finally:
            subscription.unsubscribe()

        assert lease == 300
        assert renewed == 300

    def test_a_subscription_delivers_changes_and_renews(self, sonos, garage, quiet):
        listener = _Listener()
        subscription = sonos.subscribe(
            garage,
            SonosService.RENDERING_CONTROL,
            listener.on_event,
            listener.on_lost,
        )
        try:
            sonos.set_volume(garage, 2)
            assert listener.take(lambda e: e.volume == 2).speaker_id == garage

            subscription.renew()

            sonos.set_volume(garage, 1)
            assert listener.take(lambda e: e.volume == 1).speaker_id == garage
        finally:
            subscription.unsubscribe()
        assert listener.lost == 0

    def test_the_listener_keeps_its_port_across_the_last_unsubscribe(
        self, sonos, garage
    ):
        for _ in range(2):
            listener = _Listener()
            subscription = sonos.subscribe(
                garage,
                SonosService.GROUP_RENDERING_CONTROL,
                listener.on_event,
                listener.on_lost,
            )
            try:
                listener.take(lambda e: isinstance(e, GroupVolumeChanged))
            finally:
                subscription.unsubscribe()

    def test_a_transport_change_arrives_as_an_event(self, sonos, garage, quiet):
        listener = _Listener()
        subscription = sonos.subscribe(
            garage, SonosService.AV_TRANSPORT, listener.on_event, listener.on_lost
        )
        try:
            sonos.play(garage)
            listener.take(lambda e: e.transport_state is TransportState.PLAYING)
        finally:
            try:
                sonos.stop(garage)
                stopped = listener.take(
                    lambda e: e.transport_state is TransportState.STOPPED
                    and e.transport_uri is None
                )
            finally:
                subscription.unsubscribe()
        # A partial event: the track and its place, and no actions.
        assert stopped.actions is None
        assert stopped.track == sonos.read_playback(garage).track
        assert stopped.queue_position == sonos.read_playback(garage).queue_position


# The most tracks an item the queueing tests take holds: a queue that size
# reads in one call and is cleared at once.
_MOST_TRACKS = 100


@dataclass(frozen=True)
class _Library:
    """The items of the real library the queueing tests queue."""

    machine_identifier: str
    section_id: str
    artist: PlexArtist
    # The artist's albums, newest first.
    albums: list[PlexAlbum]
    playlist: PlexPlaylist

    def item(self, kind: PlexItemKind, rating_key: str, title: str) -> PlexItem:
        return PlexItem(
            kind, self.machine_identifier, self.section_id, rating_key, title
        )

    def album(self, album: PlexAlbum) -> PlexItem:
        return self.item(PlexItemKind.ALBUM, album.rating_key, album.title)


@pytest.fixture(scope="module")
def library(live_plex: PlexapiPlexClient) -> _Library:
    """An artist of two albums or more and a playlist, each of 100 tracks or
    fewer."""
    for artist in live_plex.read_artists():
        albums = live_plex.read_artist_albums(artist.rating_key, newest_first=True)
        if len(albums) >= 2 and sum(a.track_count for a in albums) <= _MOST_TRACKS:
            break
    else:
        pytest.fail(
            f"No artist has two albums or more and {_MOST_TRACKS} tracks or fewer"
        )
    # Plex lists a smart playlist with a count it stored earlier: its own read
    # has the count it has now.
    playlists = (
        live_plex.read_playlist(p.rating_key) for p in live_plex.read_playlists()
    )
    playlist = next(
        (p for p in playlists if p is not None and 1 <= p.track_count <= _MOST_TRACKS),
        None,
    )
    assert playlist is not None, f"No playlist holds 1 to {_MOST_TRACKS} tracks"
    return _Library(
        live_plex.machine_identifier(), live_plex.section_id(), artist, albums, playlist
    )


def _incomplete(page: QueuePage) -> list[Track]:
    """The entries that lack a title, an artist, an album, a duration or a
    picture."""
    return [
        track
        for track in page.tracks
        if not (
            track.title
            and track.artist
            and track.album
            and track.duration_seconds > 0
            and track.picture.startswith("/getaa?")
        )
    ]


def _keys(page: QueuePage) -> list[str]:
    """The rating keys of a page's entries, each a Plex track."""
    entries = [parse_plex_queue_entry(track.uri) for track in page.tracks]
    assert None not in entries
    return [entry.rating_key for entry in entries if entry is not None]


def _carrying_the_token(page: QueuePage, token: str) -> list[str]:
    """The titles of the entries whose address or picture holds the token:
    the token itself is never printed."""
    return [
        track.title
        for track in page.tracks
        if token in track.uri or token in track.picture
    ]


class TestLiveQueue:
    def test_a_page_of_the_queue_and_its_size(self, sonos, garage):
        whole = sonos.read_queue(garage, 0, 1000)

        page = sonos.read_queue(garage, 1, 2)

        assert page.total == whole.total == sonos.read_playback(garage).queue_length
        assert page.tracks == whole.tracks[1:3]
        assert sonos.read_queue(garage, whole.total, 10).tracks == ()

    def test_an_album_is_queued_in_one_call_with_every_entry_complete(
        self, sonos, garage, kept_queue, library, plex, live_settings
    ):
        album = library.albums[0]
        sonos.clear_queue(garage)

        sonos.add_to_queue(garage, library.album(album))

        page = sonos.read_queue(garage, 0, 1000)
        tracks = plex.read_album_tracks(album.rating_key)
        assert _keys(page) == [track.rating_key for track in tracks]
        assert _incomplete(page) == []
        assert {track.album for track in page.tracks} == {album.title}
        assert {
            parse_plex_queue_entry(track.uri).machine_identifier
            for track in page.tracks
        } == {library.machine_identifier}
        assert _carrying_the_token(page, live_settings.plex_token) == []
        # The speaker serves each entry's picture.
        picture = sonos.read_picture(garage, page.tracks[0].picture)
        assert picture.startswith(b"\xff\xd8")

    def test_a_playlist_is_queued_in_one_call_with_every_entry_complete(
        self, sonos, garage, kept_queue, library, live_settings
    ):
        playlist = library.playlist
        sonos.clear_queue(garage)

        sonos.add_to_queue(
            garage,
            library.item(PlexItemKind.PLAYLIST, playlist.rating_key, playlist.title),
        )

        page = sonos.read_queue(garage, 0, 1000)
        assert page.total == playlist.track_count
        assert len(_keys(page)) == playlist.track_count
        assert _incomplete(page) == []
        assert _carrying_the_token(page, live_settings.plex_token) == []

    def test_an_artist_is_queued_in_one_call_with_every_entry_complete(
        self, sonos, garage, kept_queue, library, plex, live_settings
    ):
        artist = library.artist
        sonos.clear_queue(garage)

        sonos.add_to_queue(
            garage, library.item(PlexItemKind.ARTIST, artist.rating_key, artist.title)
        )

        page = sonos.read_queue(garage, 0, 1000)
        tracks = [
            track.rating_key
            for album in library.albums
            for track in plex.read_album_tracks(album.rating_key)
        ]
        assert sorted(_keys(page)) == sorted(tracks)
        assert _incomplete(page) == []
        assert _carrying_the_token(page, live_settings.plex_token) == []

    def test_albums_are_queued_in_one_call_in_the_order_given(
        self, sonos, garage, kept_queue, library, plex
    ):
        oldest_first = list(reversed(library.albums))[:16]
        sonos.clear_queue(garage)

        sonos.add_many_to_queue(garage, [library.album(a) for a in oldest_first])

        page = sonos.read_queue(garage, 0, 1000)
        assert _keys(page) == [
            track.rating_key
            for album in oldest_first
            for track in plex.read_album_tracks(album.rating_key)
        ]
        assert _incomplete(page) == []

    def test_add_appends_and_changes_nothing_else(
        self, sonos, garage, kept_queue, library, plex
    ):
        found = sonos.read_playback(garage)
        album = library.albums[0]

        sonos.add_to_queue(garage, library.album(album))

        assert sonos.read_queue(garage, 0, kept_queue.total).tracks == (
            kept_queue.tracks
        )
        added = sonos.read_queue(garage, kept_queue.total, 1000)
        assert _keys(added) == [
            track.rating_key for track in plex.read_album_tracks(album.rating_key)
        ]
        left = sonos.read_playback(garage)
        assert left.queue_length == kept_queue.total + album.track_count
        assert (
            left.transport_state,
            left.queue_position,
            left.track,
            left.shuffle,
        ) == (
            found.transport_state,
            found.queue_position,
            found.track,
            found.shuffle,
        )

    def test_shuffle_presents_the_queue_in_play_order(
        self, sonos, garage, quiet, kept_queue, library
    ):
        # quiet before kept_queue: the Garage stops before its volume is put
        # back.
        album = max(library.albums, key=lambda a: a.track_count)
        assert album.track_count >= 2
        sonos.clear_queue(garage)
        sonos.add_to_queue(garage, library.album(album))
        in_order = sonos.read_queue(garage, 0, 1000)

        sonos.set_shuffle(garage, True)

        shuffled = sonos.read_queue(garage, 0, 1000)
        assert sonos.read_playback(garage).shuffle is True
        assert sorted(_keys(shuffled)) == sorted(_keys(in_order))
        # The current track stays first.
        assert shuffled.tracks[0] == in_order.tracks[0]

        sonos.play_from(garage, 1)
        playing = _wait_for(
            lambda: sonos.read_playback(garage),
            lambda p: p.transport_state is TransportState.PLAYING,
        )
        assert (playing.queue_position, playing.track) == (1, shuffled.tracks[0])
        sonos.next(garage)
        moved = _wait_for(
            lambda: sonos.read_playback(garage), lambda p: p.queue_position == 2
        )
        assert moved.track == shuffled.tracks[1]
        sonos.stop(garage)

        sonos.set_shuffle(garage, False)

        assert sonos.read_queue(garage, 0, 1000) == in_order
        assert sonos.read_playback(garage).shuffle is False

    def test_an_album_plays_from_one_of_its_tracks(
        self, sonos, garage, quiet, kept_queue, library, plex
    ):
        # What a queueing task does to play an album from a track: the album
        # replaces the queue, the track is looked for in it, and the speaker
        # plays from there. quiet before kept_queue: the Garage stops before
        # its volume is put back.
        album = max(library.albums, key=lambda a: a.track_count)
        assert album.track_count >= 2
        tracks = plex.read_album_tracks(album.rating_key)
        assert tracks is not None
        start = tracks[-1]
        sonos.clear_queue(garage)
        sonos.select_queue(garage)
        sonos.set_shuffle(garage, False)
        sonos.add_to_queue(garage, library.album(album))

        page = sonos.read_queue(garage, 0, 1000)
        # The service queues the album in Plex's disc and track order.
        assert _keys(page) == [track.rating_key for track in tracks]
        position = _keys(page).index(start.rating_key) + 1
        # A seek past the last track is refused, and changes nothing.
        with pytest.raises(ExternalServiceException) as raised:
            sonos.play_from(garage, page.total + 1)
        assert raised.value.cause == (
            "the speaker answered UPnP error 711 (Illegal seek target)"
        )
        assert sonos.read_playback(garage).transport_state is TransportState.STOPPED

        sonos.play_from(garage, position)

        playing = _wait_for(
            lambda: sonos.read_playback(garage),
            lambda p: p.transport_state is TransportState.PLAYING,
        )
        assert playing.transport_state is TransportState.PLAYING
        assert (playing.queue_position, playing.queue_length) == (
            position,
            album.track_count,
        )
        assert playing.track == page.tracks[position - 1]
        sonos.stop(garage)

    def test_the_emptied_queue_is_selected_and_shuffled_before_it_is_filled(
        self, sonos, garage, kept_queue, library
    ):
        # What a queueing task's play or shuffle does before it queues: with
        # a stream as the source, the speaker refuses shuffle (UPnP 712)
        # until the queue is the source again.
        sonos.clear_queue(garage)

        sonos.select_queue(garage)
        sonos.set_shuffle(garage, True)

        selected = sonos.read_playback(garage)
        assert selected.transport_uri.startswith("x-rincon-queue:")
        assert (selected.transport_state, selected.queue_length, selected.shuffle) == (
            TransportState.STOPPED,
            0,
            True,
        )
        album = library.albums[0]
        sonos.add_to_queue(garage, library.album(album))
        filled = sonos.read_playback(garage)
        assert (filled.transport_state, filled.queue_position, filled.shuffle) == (
            TransportState.STOPPED,
            1,
            True,
        )
        assert filled.queue_length == album.track_count

    def test_a_queue_change_arrives_as_a_queue_event(
        self, sonos, garage, kept_queue, library
    ):
        listener = _Listener()
        subscription = sonos.subscribe(
            garage, SonosService.QUEUE, listener.on_event, listener.on_lost
        )
        try:
            # The state in full a subscription starts with.
            assert listener.take(lambda e: True) == QueueChanged(garage)

            sonos.add_to_queue(garage, library.album(library.albums[0]))
            added = listener.take(lambda e: True)
        finally:
            subscription.unsubscribe()

        assert added == QueueChanged(garage)
        assert listener.lost == 0

    def test_a_container_the_service_does_not_have_is_refused_in_the_clients_words(
        self, sonos, garage, kept_queue, library
    ):
        # A refused fetch, asked again QUEUE_ATTEMPTS times in all.
        with pytest.raises(FetchRefusedException) as raised:
            sonos.add_to_queue(
                garage, library.item(PlexItemKind.ALBUM, "999999999", "No Album")
            )

        assert raised.value.cause == "the speaker answered UPnP error 800"
        assert sonos.read_queue(garage, 0, 1000) == kept_queue

    def test_a_refused_fetch_refuses_the_whole_call_and_queues_nothing(
        self, sonos, garage, kept_queue, library
    ):
        # Fetched before the refused one, and still not queued: a refused
        # call can be made again without queueing anything twice (SCTL-17).
        fetched = [library.album(album) for album in library.albums[:2]]
        unknown = library.item(PlexItemKind.ALBUM, "999999999", "No Album")

        with pytest.raises(FetchRefusedException):
            sonos.add_many_to_queue(garage, [*fetched, unknown])

        assert sonos.read_queue(garage, 0, 1000) == kept_queue

    def test_playing_an_empty_queue_from_the_start_fails_in_the_clients_words(
        self, sonos, garage, kept_queue
    ):
        sonos.clear_queue(garage)

        with pytest.raises(ExternalServiceException) as raised:
            sonos.play_from(garage, 1)

        assert raised.value.cause == (
            "the speaker answered UPnP error 711 (Illegal seek target)"
        )
        assert sonos.read_playback(garage).transport_state is TransportState.STOPPED
