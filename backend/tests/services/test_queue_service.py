"""Tests for the queue service, on the in-memory household and Plex.

The service runs on the fake Sonos client, the fake Plex and the real
household mirror.
"""

import logging
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import replace
from datetime import date

import pytest
from flask import Flask

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.exceptions import (
    ItemNotFoundException,
    PlaylistNotPlayableException,
    SpeakerNotFoundException,
)
from app.schemas.queue_schema import QueueListQuerySchema, QueueRequestSchema
from app.services.household_mirror import HouseholdMirror
from app.services.queue_service import QueueEntry, QueueingTask, QueueService
from app.services.sonos_client import (
    PlexItem,
    PlexItemKind,
    SoCoSonosClient,
    TransportState,
)
from app.services.sse_connection_manager import LOCAL_ADMIN_TARGET
from app.services.task_service import TaskProgressHandle
from app.utils.artwork_id import plex_artwork_id
from app.utils.plex_queue_entry import parse_plex_queue_entry
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import FakeSonosClient, app_track, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
KITCHEN = "RINCON_FAKE0000000301400"
NOWHERE = "RINCON_FAKE0000000991400"

ALBUM = "2001"
# The rating keys of queue_track(1) to queue_track(7).
TRACKS = [str(1000 + number) for number in range(1, 8)]

DID_NOT_ANSWER = "the speaker did not answer (ConnectTimeout)"


@pytest.fixture
def household(
    sonos: FakeSonosClient, plex: FakePlexClient, mirror: HouseholdMirror
) -> FakeSonosClient:
    """The Garage with seven Plex tracks of one album queued, the Lounge
    with two tracks of another service and the Kitchen with nothing,
    mirrored."""
    sonos.add_room(GARAGE, "Garage")
    sonos.add_room(LOUNGE, "Lounge")
    sonos.add_room(KITCHEN, "Kitchen")
    sonos.load_queue(GARAGE, [queue_track(number) for number in range(1, 8)])
    sonos.load_queue(LOUNGE, [app_track(1), app_track(2)])
    plex.add_album(ALBUM, TRACKS)
    mirror.start()
    sonos.settle()
    return sonos


@pytest.fixture
def queue(app: Flask, household: FakeSonosClient) -> QueueService:
    service: QueueService = app.container.queue_service()
    return service


def query(**fields: object) -> QueueListQuerySchema:
    return QueueListQuerySchema.model_validate(fields)


def positions(entries: list[QueueEntry]) -> list[int]:
    return [entry.position for entry in entries]


def calls_from_here(household: FakeSonosClient) -> list[str]:
    """The room of each call the test's thread makes to a speaker from here
    on."""
    thread = threading.current_thread()
    reached: list[str] = []

    def before_call(speaker_id: str) -> None:
        if threading.current_thread() is thread:
            reached.append(speaker_id)

    household.before_call = before_call
    return reached


def picture_requests(
    plex: FakePlexClient, monkeypatch: pytest.MonkeyPatch
) -> list[list[str]]:
    """The rating keys of each call for the pictures of tracks from here on."""
    asked: list[list[str]] = []
    read_track_pictures = plex.read_track_pictures

    def recorded(rating_keys: Iterable[str]) -> dict[str, str]:
        keys = list(rating_keys)
        asked.append(keys)
        return read_track_pictures(keys)

    monkeypatch.setattr(plex, "read_track_pictures", recorded)
    return asked


class TestQueueServiceListQueue:
    def test_the_queue_as_the_speaker_gives_it(self, queue, plex):
        listing = queue.list_queue(GARAGE, query())

        assert (listing.total, listing.page, listing.current_position) == (7, 1, 1)
        assert listing.entries[0] == QueueEntry(
            position=1,
            title="Track 1",
            artist="An Artist",
            album="An Album",
            duration_ms=181000,
            artwork_id=plex_artwork_id(plex.read_track_picture("1001")),
            track_id="1001",
        )
        assert positions(listing.entries) == [1, 2, 3, 4, 5, 6, 7]
        assert [entry.track_id for entry in listing.entries] == TRACKS
        assert [entry.title for entry in listing.entries] == [
            f"Track {number}" for number in range(1, 8)
        ]

    def test_the_tracks_of_an_album_share_its_picture(self, queue):
        listing = queue.list_queue(GARAGE, query())

        assert len({entry.artwork_id for entry in listing.entries}) == 1
        assert listing.entries[0].artwork_id is not None

    def test_the_pictures_of_a_page_are_one_call_to_plex(
        self, queue, plex, monkeypatch
    ):
        asked = picture_requests(plex, monkeypatch)

        queue.list_queue(GARAGE, query(page=2, per_page=3))

        assert asked == [["1004", "1005", "1006"]]

    def test_a_page(self, queue):
        listing = queue.list_queue(GARAGE, query(page=3, per_page=3))

        assert positions(listing.entries) == [7]
        assert (listing.total, listing.page) == (7, 3)

    def test_a_page_past_the_last_is_empty(self, queue):
        listing = queue.list_queue(GARAGE, query(page=4, per_page=3))

        assert listing.entries == []
        assert (listing.total, listing.page) == (7, 4)

    def test_an_empty_queue(self, queue):
        listing = queue.list_queue(KITCHEN, query())

        assert listing.entries == []
        assert (listing.total, listing.page, listing.current_position) == (0, 1, None)

    def test_the_current_position_follows_the_track_that_is_loaded(
        self, queue, household
    ):
        household.next(GARAGE)
        household.next(GARAGE)
        household.settle()

        assert queue.list_queue(GARAGE, query()).current_position == 3

    def test_there_is_no_current_position_while_a_stream_plays(self, queue, household):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        listing = queue.list_queue(GARAGE, query())

        assert listing.current_position is None
        # The queue stays on the speaker, and is listed.
        assert listing.total == 7

    @pytest.mark.parametrize("page", [1, 2, 3])
    def test_locate_answers_the_page_that_holds_the_current_track(
        self, queue, household, page
    ):
        for _ in range(4):
            household.next(GARAGE)
        household.settle()

        listing = queue.list_queue(
            GARAGE, query(page=page, per_page=3, locate="current")
        )

        assert listing.current_position == 5
        assert listing.page == 2
        assert positions(listing.entries) == [4, 5, 6]

    @pytest.mark.parametrize(
        ("current", "page"), [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3)]
    )
    def test_locate_at_each_end_of_a_page(self, queue, household, current, page):
        for _ in range(current - 1):
            household.next(GARAGE)
        household.settle()

        listing = queue.list_queue(GARAGE, query(page=3, per_page=3, locate="current"))

        assert listing.page == page
        assert current in positions(listing.entries)

    def test_locate_without_a_current_track_answers_the_page_asked_for(
        self, queue, household
    ):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        listing = queue.list_queue(GARAGE, query(page=2, per_page=3, locate="current"))

        assert listing.page == 2
        assert positions(listing.entries) == [4, 5, 6]

    def test_a_shuffled_queue_is_listed_in_the_order_it_plays(self, queue, household):
        household.set_shuffle(GARAGE, True)
        household.settle()

        listing = queue.list_queue(GARAGE, query())

        played = household.read_queue(GARAGE, 0, 100).tracks
        assert [entry.title for entry in listing.entries] == [
            track.title for track in played
        ]
        assert [entry.title for entry in listing.entries] != [
            f"Track {number}" for number in range(1, 8)
        ]
        assert listing.current_position == 1

    def test_every_room_of_a_group_answers_the_groups_queue(self, queue, household):
        household.join(KITCHEN, GARAGE)
        household.settle()

        assert queue.list_queue(KITCHEN, query()) == queue.list_queue(GARAGE, query())

    def test_what_changes_elsewhere_is_read_at_once(self, queue, household):
        queue.list_queue(GARAGE, query())

        # Before the speaker's events have arrived.
        household.load_queue(GARAGE, [app_track(5)])

        listing = queue.list_queue(GARAGE, query())
        assert [entry.title for entry in listing.entries] == ["App Track 5"]
        assert listing.total == 1

    def test_an_entry_that_is_not_from_plex_has_neither_id(self, queue):
        listing = queue.list_queue(LOUNGE, query())

        assert listing.entries[0] == QueueEntry(
            position=1,
            title="App Track 1",
            artist="An App Artist",
            album="An App Album",
            duration_ms=201000,
            artwork_id=None,
            track_id=None,
        )

    def test_an_entry_of_another_plex_server_has_neither_id(
        self, queue, household, plex, monkeypatch
    ):
        # The other server's track has a rating key the library has too.
        household.load_queue(
            GARAGE,
            [queue_track(1, machine_identifier="anotherserver"), queue_track(2)],
        )
        asked = picture_requests(plex, monkeypatch)

        entries = queue.list_queue(GARAGE, query()).entries

        assert [(entry.track_id, entry.artwork_id is None) for entry in entries] == [
            (None, True),
            ("1002", False),
        ]
        assert asked == [["1002"]]

    def test_a_page_without_plex_tracks_asks_plex_nothing(self, queue, plex, caplog):
        plex.reachable = False

        with caplog.at_level(logging.WARNING):
            listing = queue.list_queue(LOUNGE, query())

        assert listing.total == 2
        assert caplog.records == []

    def test_a_plex_track_without_a_picture_has_no_artwork(
        self, queue, household, plex
    ):
        plex.add_album("2002", ["1101"], size=None)
        household.load_queue(GARAGE, [queue_track(101), queue_track(102)])

        entries = queue.list_queue(GARAGE, query()).entries

        # 1101 is on an album without a cover, 1102 is a track Plex does not
        # have.
        assert [(entry.track_id, entry.artwork_id) for entry in entries] == [
            ("1101", None),
            ("1102", None),
        ]

    def test_what_an_entry_does_not_give_is_none(self, queue, household):
        household.load_queue(
            GARAGE, [replace(app_track(1), album="", duration_seconds=0)]
        )

        (entry,) = queue.list_queue(GARAGE, query()).entries

        assert (entry.album, entry.duration_ms) == (None, None)

    def test_the_texts_are_nfc(self, queue, household):
        decomposed = {
            "title": unicodedata.normalize("NFD", "Café"),
            "artist": unicodedata.normalize("NFD", "Björk"),
            "album": unicodedata.normalize("NFD", "Début"),
        }
        household.load_queue(GARAGE, [replace(queue_track(1), **decomposed)])

        (entry,) = queue.list_queue(GARAGE, query()).entries

        assert (entry.title, entry.artist, entry.album) == ("Café", "Björk", "Début")
        for text in (entry.title, entry.artist, entry.album):
            assert unicodedata.is_normalized("NFC", text)

    def test_while_plex_has_never_answered_plex_tracks_have_neither_id(
        self, queue, household, plex, caplog
    ):
        household.load_queue(GARAGE, [queue_track(1), app_track(2)])
        plex.reachable = False

        with caplog.at_level(logging.WARNING, "app.services.queue_service"):
            entries = queue.list_queue(GARAGE, query()).entries

        assert [
            (entry.title, entry.track_id, entry.artwork_id) for entry in entries
        ] == [
            ("Track 1", None, None),
            ("App Track 2", None, None),
        ]
        assert [record.levelname for record in caplog.records] == ["WARNING"]
        assert caplog.records[0].getMessage() == (
            f"The queue of speaker {GARAGE} is served without the track ids and "
            "the artwork of its Plex tracks: Cannot read which server Plex is "
            "because external service failed: Plex did not answer (ConnectionError)"
        )

    def test_plex_that_fails_to_give_the_pictures_fails_the_listing(self, queue, plex):
        # Plex answered once: which server it is is known.
        plex.machine_identifier()
        plex.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            queue.list_queue(GARAGE, query())

        assert raised.value.message == (
            "Cannot read the pictures of 7 Plex tracks because external service "
            "failed: Plex did not answer (ConnectionError)"
        )

    def test_a_speaker_that_does_not_answer_fails_the_listing(self, queue, household):
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException) as raised:
            queue.list_queue(GARAGE, query())
        household.answer_again(GARAGE)

        assert raised.value.message == (
            f"Cannot read the queue of speaker {GARAGE} because external service "
            f"failed: {DID_NOT_ANSWER}"
        )

    def test_a_silent_coordinator_fails_the_listing_of_its_group(
        self, queue, household
    ):
        household.join(KITCHEN, GARAGE)
        household.settle()
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException):
            queue.list_queue(KITCHEN, query())
        household.answer_again(GARAGE)

    def test_an_unknown_speaker_is_not_found_without_asking_a_speaker(
        self, queue, household
    ):
        reached = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException) as raised:
            queue.list_queue(NOWHERE, query())

        assert raised.value.error_code == "SPEAKER_NOT_FOUND"
        assert reached == []


class TestQueueServiceClear:
    def test_it_empties_the_queue(self, queue, household, mirror):
        queue.clear(GARAGE)

        assert household.read_queue(GARAGE, 0, 100).total == 0
        household.settle()
        assert mirror.playback(GARAGE).queue_length == 0
        assert queue.list_queue(GARAGE, query()).current_position is None

    def test_a_room_of_a_group_clears_the_groups_queue(self, queue, household):
        household.join(KITCHEN, GARAGE)
        household.settle()
        reached = calls_from_here(household)

        queue.clear(KITCHEN)

        assert reached == [KITCHEN]
        assert household.read_queue(GARAGE, 0, 100).total == 0

    def test_the_other_queues_stay(self, queue, household):
        queue.clear(GARAGE)

        assert household.read_queue(LOUNGE, 0, 100).total == 2

    def test_an_empty_queue_is_cleared(self, queue, household):
        queue.clear(KITCHEN)

        assert household.read_queue(KITCHEN, 0, 100).total == 0

    def test_a_queue_is_cleared_while_a_stream_plays(self, queue, household, mirror):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        queue.clear(GARAGE)
        household.settle()

        assert household.read_queue(GARAGE, 0, 100).total == 0
        assert mirror.playback(GARAGE).track.artist == "Radio One"

    def test_a_speaker_that_does_not_answer_fails(self, queue, household):
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException) as raised:
            queue.clear(GARAGE)
        household.answer_again(GARAGE)

        assert raised.value.message == (
            f"Cannot clear the queue of speaker {GARAGE} because external service "
            f"failed: {DID_NOT_ANSWER}"
        )

    def test_an_unknown_speaker_is_not_found_without_asking_a_speaker(
        self, queue, household
    ):
        reached = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException):
            queue.clear(NOWHERE)

        assert reached == []


# The library the queueing tests queue from: an artist of three albums, an
# artist of eighteen, an artist without albums and playlists.
BAND = "10"
NEW_ALBUM, OLD_ALBUM, UNDATED_ALBUM = "12", "11", "13"
NEW_TRACKS, OLD_TRACKS, UNDATED_TRACKS = ["121", "122"], ["111", "112", "113"], ["131"]
BAND_TRACKS = [*OLD_TRACKS, *NEW_TRACKS, *UNDATED_TRACKS]
PROLIFIC = "20"
# Album 300 is from 1990, album 317 from 2007: one track each.
PROLIFIC_ALBUMS = [str(300 + n) for n in range(18)]
SILENT = "30"
MIX, EMPTY, HUGE = "40", "41", "42"
MIX_TRACKS = ["121", "111", "131"]


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    """Without pictures, which queueing does not read."""
    plex.add_artist(BAND, "The Band", size=None)
    plex.add_album(
        OLD_ALBUM, OLD_TRACKS, None, artist=BAND, released=date(2001, 5, 1)
    )
    plex.add_album(
        NEW_ALBUM, NEW_TRACKS, None, artist=BAND, released=date(2010, 1, 1)
    )
    plex.add_album(UNDATED_ALBUM, UNDATED_TRACKS, None, artist=BAND)
    plex.add_artist(PROLIFIC, "The Prolific", size=None)
    for n, album in enumerate(PROLIFIC_ALBUMS):
        plex.add_album(
            album, [f"3{n:02}0"], None, artist=PROLIFIC, released=date(1990 + n, 1, 1)
        )
    plex.add_artist(SILENT, "The Silent", size=None)
    plex.add_playlist(MIX, "A Mix", 3, (1, 1), track_keys=MIX_TRACKS)
    plex.add_playlist(EMPTY, "Empty", 0)
    plex.add_playlist(HUGE, "All Music", 12019, (1, 1))
    return plex


def asked(kind: str, item_id: str, action: str, **fields: str) -> QueueRequestSchema:
    return QueueRequestSchema.model_validate(
        {"kind": kind, "item_id": item_id, "action": action, **fields}
    )


def keys(household: FakeSonosClient, speaker_id: str) -> list[str]:
    """The rating keys of the group's queue, as the speaker presents it."""
    entries = [
        parse_plex_queue_entry(track.uri)
        for track in household.read_queue(speaker_id, 0, 1000).tracks
    ]
    return [entry.rating_key if entry else "" for entry in entries]


def queue_calls(
    household: FakeSonosClient, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[object, ...]]:
    """The calls that change a queue from here on, whichever thread makes
    them: the call, the room, and what it queues by rating key."""
    calls: list[tuple[object, ...]] = []

    def record(name: str) -> None:
        original = getattr(household, name)

        def recorded(speaker_id: str, *args: object) -> None:
            shown: list[object] = []
            for arg in args:
                if isinstance(arg, PlexItem):
                    shown += [arg.kind.value, arg.rating_key]
                elif isinstance(arg, list):
                    shown.append([item.rating_key for item in arg])
                else:
                    shown.append(arg)
            calls.append((name, speaker_id, *shown))
            original(speaker_id, *args)

        monkeypatch.setattr(household, name, recorded)

    for name in (
        "clear_queue",
        "select_queue",
        "set_shuffle",
        "add_to_queue",
        "add_many_to_queue",
        "play_from_start",
    ):
        record(name)
    return calls


def replaced(shuffle: bool, *containers: tuple[object, ...]) -> list[tuple]:
    """The calls of a play or shuffle on the Garage: the queue replaced, the
    first container queued and played, the rest added."""
    first, *rest = containers
    return [
        ("clear_queue", GARAGE),
        ("select_queue", GARAGE),
        ("set_shuffle", GARAGE, shuffle),
        ("add_to_queue", GARAGE, *first),
        ("play_from_start", GARAGE),
        *(("add_many_to_queue", GARAGE, list(more)) for more in rest),
    ]


class TestQueueServiceQueue:
    """What a queueing task does to the group's queue."""

    @pytest.fixture(autouse=True)
    def items(self, library: FakePlexClient) -> FakePlexClient:
        return library

    def run(self, queue: QueueService, tasks, speaker_id: str, request) -> list[dict]:
        started = queue.queue(speaker_id, request)
        assert started.status == "pending"
        events = tasks.finish(started.task_id)
        assert [event["event_type"] for event in events] == [
            "task_started",
            "task_completed",
        ], events[-1]["data"]
        return events

    def test_play_replaces_the_queue_with_the_item_and_plays_it(
        self, queue, household, tasks, monkeypatch
    ):
        household.next(GARAGE)
        calls = queue_calls(household, monkeypatch)

        self.run(queue, tasks, GARAGE, asked("album", OLD_ALBUM, "play"))

        assert calls == replaced(False, ("album", OLD_ALBUM))
        assert keys(household, GARAGE) == OLD_TRACKS
        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert (playback.queue_position, playback.shuffle) == (1, False)

    def test_play_turns_shuffle_off_before_it_queues(self, queue, household, tasks):
        household.set_shuffle(GARAGE, True)

        self.run(queue, tasks, GARAGE, asked("playlist", MIX, "play"))

        assert keys(household, GARAGE) == MIX_TRACKS
        playback = household.read_playback(GARAGE)
        assert (playback.queue_position, playback.shuffle) == (1, False)
        assert playback.transport_state is TransportState.PLAYING

    def test_shuffle_replaces_the_queue_with_the_item_and_plays_it_shuffled(
        self, queue, household, tasks, monkeypatch
    ):
        calls = queue_calls(household, monkeypatch)

        self.run(queue, tasks, GARAGE, asked("album", OLD_ALBUM, "shuffle"))

        assert calls == replaced(True, ("album", OLD_ALBUM))
        assert sorted(keys(household, GARAGE)) == OLD_TRACKS
        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert (playback.queue_position, playback.shuffle) == (1, True)

    def test_add_appends_and_changes_nothing_else(
        self, queue, household, tasks, monkeypatch
    ):
        household.next(GARAGE)
        household.play(GARAGE)
        found = household.read_playback(GARAGE)
        calls = queue_calls(household, monkeypatch)

        self.run(queue, tasks, GARAGE, asked("album", OLD_ALBUM, "add"))

        assert calls == [("add_to_queue", GARAGE, "album", OLD_ALBUM)]
        assert keys(household, GARAGE) == [*TRACKS, *OLD_TRACKS]
        left = household.read_playback(GARAGE)
        assert (left.transport_state, left.queue_position, left.track) == (
            found.transport_state,
            found.queue_position,
            found.track,
        )
        assert left.shuffle is False

    def test_add_leaves_shuffle_on(self, queue, household, tasks):
        household.set_shuffle(GARAGE, True)
        current = household.read_playback(GARAGE).track

        self.run(queue, tasks, GARAGE, asked("track", "112", "add"))

        playback = household.read_playback(GARAGE)
        assert (playback.shuffle, playback.track) == (True, current)
        assert sorted(keys(household, GARAGE)) == sorted([*TRACKS, "112"])

    @pytest.mark.parametrize(
        ("order", "albums"),
        [
            ("newest_first", [NEW_ALBUM, OLD_ALBUM, UNDATED_ALBUM]),
            ("oldest_first", [OLD_ALBUM, NEW_ALBUM, UNDATED_ALBUM]),
        ],
    )
    def test_play_queues_an_artist_as_its_albums_in_release_order(
        self, queue, household, tasks, monkeypatch, order, albums
    ):
        calls = queue_calls(household, monkeypatch)

        self.run(queue, tasks, GARAGE, asked("artist", BAND, "play", order=order))

        # Music starts after the first album, and the rest follows.
        assert calls == replaced(False, ("album", albums[0]), albums[1:])
        tracks = {NEW_ALBUM: NEW_TRACKS, OLD_ALBUM: OLD_TRACKS}
        assert keys(household, GARAGE) == [
            key for album in albums for key in tracks.get(album, UNDATED_TRACKS)
        ]
        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert playback.queue_position == 1

    def test_newest_first_is_the_default(self, queue, household, tasks):
        self.run(queue, tasks, GARAGE, asked("artist", BAND, "play"))

        assert keys(household, GARAGE) == [*NEW_TRACKS, *OLD_TRACKS, *UNDATED_TRACKS]

    def test_the_rest_of_an_artist_is_queued_sixteen_albums_a_call(
        self, queue, household, tasks, monkeypatch
    ):
        calls = queue_calls(household, monkeypatch)

        self.run(
            queue, tasks, GARAGE, asked("artist", PROLIFIC, "play", order="oldest_first")
        )

        assert calls == replaced(
            False,
            ("album", PROLIFIC_ALBUMS[0]),
            PROLIFIC_ALBUMS[1:17],
            PROLIFIC_ALBUMS[17:],
        )
        assert keys(household, GARAGE) == [f"3{n:02}0" for n in range(18)]

    def test_add_queues_an_artist_as_its_albums_and_plays_nothing(
        self, queue, household, tasks, monkeypatch
    ):
        found = household.read_playback(GARAGE)
        calls = queue_calls(household, monkeypatch)

        self.run(queue, tasks, GARAGE, asked("artist", BAND, "add"))

        assert calls == [
            ("add_to_queue", GARAGE, "album", NEW_ALBUM),
            ("add_many_to_queue", GARAGE, [OLD_ALBUM, UNDATED_ALBUM]),
        ]
        assert keys(household, GARAGE) == [
            *TRACKS,
            *NEW_TRACKS,
            *OLD_TRACKS,
            *UNDATED_TRACKS,
        ]
        left = household.read_playback(GARAGE)
        assert (left.transport_state, left.track) == (
            found.transport_state,
            found.track,
        )

    def test_shuffle_queues_an_artist_as_one_container(
        self, queue, household, tasks, monkeypatch
    ):
        calls = queue_calls(household, monkeypatch)

        self.run(
            queue, tasks, GARAGE, asked("artist", BAND, "shuffle", order="oldest_first")
        )

        # Every track is there before the first is drawn.
        assert calls == replaced(True, ("artist", BAND))
        assert sorted(keys(household, GARAGE)) == sorted(BAND_TRACKS)
        assert household.read_playback(GARAGE).shuffle is True

    @pytest.mark.parametrize(
        ("kind", "item_id", "tracks"),
        [
            ("album", NEW_ALBUM, NEW_TRACKS),
            ("track", "112", ["112"]),
            ("playlist", MIX, MIX_TRACKS),
        ],
    )
    @pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
    def test_every_other_item_is_one_container_whatever_the_order(
        self, queue, household, tasks, monkeypatch, kind, item_id, tracks, order
    ):
        calls = queue_calls(household, monkeypatch)

        self.run(queue, tasks, GARAGE, asked(kind, item_id, "play", order=order))

        assert calls == replaced(False, (kind, item_id))
        assert keys(household, GARAGE) == tracks

    @pytest.mark.parametrize("action", ["play", "shuffle"])
    def test_a_stream_gives_way_to_the_queue_before_the_mode_is_set(
        self, queue, household, tasks, action
    ):
        # A speaker refuses shuffle while a stream is its source.
        household.start_stream(GARAGE, "Radio One")

        self.run(queue, tasks, GARAGE, asked("album", OLD_ALBUM, action))

        playback = household.read_playback(GARAGE)
        assert playback.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert playback.transport_state is TransportState.PLAYING
        assert playback.shuffle is (action == "shuffle")
        assert sorted(keys(household, GARAGE)) == OLD_TRACKS

    def test_a_room_of_a_group_queues_on_the_groups_queue(
        self, queue, household, tasks
    ):
        household.join(KITCHEN, GARAGE)
        household.settle()

        self.run(queue, tasks, KITCHEN, asked("album", OLD_ALBUM, "play"))

        assert keys(household, GARAGE) == OLD_TRACKS
        assert keys(household, KITCHEN) == OLD_TRACKS
        assert household.read_queue(LOUNGE, 0, 100).total == 2

    def test_each_step_reaches_the_mirror(self, queue, household, tasks, mirror):
        self.run(queue, tasks, GARAGE, asked("album", NEW_ALBUM, "play"))
        household.settle()

        playback = mirror.playback(GARAGE)
        assert playback.state == "playing"
        assert (playback.queue_position, playback.queue_length) == (1, 2)
        assert mirror.queue_position(GARAGE) == 1


class TestQueueServiceQueueRefusals:
    """What is refused before any task starts."""

    @pytest.fixture(autouse=True)
    def items(self, library: FakePlexClient) -> FakePlexClient:
        return library

    @pytest.fixture
    def small(self, app: Flask, household: FakeSonosClient) -> QueueService:
        """The service with a playlist limit of three tracks."""
        container = app.container
        return QueueService(
            household_mirror=container.household_mirror(),
            sonos_client=household,
            plex_client=container.plex_client(),
            task_service=container.task_service(),
            playlist_max_tracks=3,
        )

    def test_an_unknown_speaker_is_not_found_without_asking_a_speaker(
        self, queue, household
    ):
        reached = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException) as raised:
            queue.queue(NOWHERE, asked("album", OLD_ALBUM, "play"))

        assert raised.value.message == f"Speaker {NOWHERE} was not found"
        assert reached == []

    @pytest.mark.parametrize(
        ("kind", "item_id", "named"),
        [
            ("artist", "99", "Artist"),
            ("album", "99", "Album"),
            ("track", "99", "Track"),
            ("playlist", "99", "Playlist"),
            # An item of another kind.
            ("artist", OLD_ALBUM, "Artist"),
            ("album", BAND, "Album"),
            ("track", OLD_ALBUM, "Track"),
            ("playlist", OLD_ALBUM, "Playlist"),
        ],
    )
    def test_an_item_plex_does_not_have_is_not_found(
        self, queue, household, kind, item_id, named
    ):
        reached = calls_from_here(household)

        with pytest.raises(ItemNotFoundException) as raised:
            queue.queue(GARAGE, asked(kind, item_id, "add"))

        assert raised.value.message == f"{named} {item_id} was not found"
        assert raised.value.error_code == "ITEM_NOT_FOUND"
        assert reached == []

    def test_a_playlist_without_tracks_is_not_playable(self, queue, household):
        with pytest.raises(PlaylistNotPlayableException) as raised:
            queue.queue(GARAGE, asked("playlist", EMPTY, "play"))

        assert raised.value.message == (
            f"Cannot queue playlist {EMPTY} because it holds no tracks"
        )
        assert raised.value.error_code == "PLAYLIST_NOT_PLAYABLE"
        assert keys(household, GARAGE) == TRACKS

    def test_a_playlist_over_the_limit_is_not_playable(self, queue, household):
        with pytest.raises(PlaylistNotPlayableException) as raised:
            queue.queue(GARAGE, asked("playlist", HUGE, "add"))

        assert raised.value.message == (
            f"Cannot queue playlist {HUGE} because it holds 12019 tracks, more "
            "than the 1000 a playlist may hold to be queued"
        )
        assert keys(household, GARAGE) == TRACKS

    def test_the_limit_is_the_settings(self, small, household, tasks, plex):
        plex.add_playlist("43", "Four", 4, (1, 1), track_keys=[*OLD_TRACKS, "121"])

        with pytest.raises(PlaylistNotPlayableException, match="more than the 3 "):
            small.queue(GARAGE, asked("playlist", "43", "play"))
        started = small.queue(GARAGE, asked("playlist", MIX, "play"))

        assert tasks.finish(started.task_id)[-1]["event_type"] == "task_completed"
        assert keys(household, GARAGE) == MIX_TRACKS

    def test_plex_that_fails_refuses_before_any_task(self, queue, household, plex):
        plex.reachable = False
        reached = calls_from_here(household)

        with pytest.raises(ExternalServiceException) as raised:
            queue.queue(GARAGE, asked("album", OLD_ALBUM, "play"))

        assert raised.value.operation == f"read Plex album {OLD_ALBUM}"
        assert reached == []
        assert keys(household, GARAGE) == TRACKS


class TestQueueServiceQueueFailures:
    """What a queueing task that fails reports, and leaves."""

    @pytest.fixture(autouse=True)
    def items(self, library: FakePlexClient) -> FakePlexClient:
        return library

    def failure(self, queue: QueueService, tasks, request) -> dict:
        started = queue.queue(GARAGE, request)
        events = tasks.finish(started.task_id)
        assert [event["event_type"] for event in events] == [
            "task_started",
            "task_failed",
        ]
        return events[-1]["data"]

    def test_a_speaker_that_fails_fails_the_task_in_the_clients_words(
        self, queue, household, tasks
    ):
        household.silence_room(GARAGE)
        try:
            data = self.failure(queue, tasks, asked("album", OLD_ALBUM, "play"))
        finally:
            household.answer_again(GARAGE)

        # The message alone: no traceback, whose chained causes name the
        # speaker's address.
        assert data == {
            "error": (
                f"Cannot clear the queue of speaker {GARAGE} because external "
                f"service failed: {DID_NOT_ANSWER}"
            )
        }
        assert keys(household, GARAGE) == TRACKS

    def test_a_failed_play_leaves_the_queue_as_far_as_it_got(
        self, queue, household, tasks
    ):
        household.plex_linked = False

        data = self.failure(queue, tasks, asked("artist", BAND, "play"))

        assert data == {
            "error": (
                f"Cannot add Plex album {NEW_ALBUM} to the queue of speaker "
                f"{GARAGE} because external service failed: the speaker lists "
                "no Plex service"
            )
        }
        assert household.read_queue(GARAGE, 0, 100).total == 0

    def test_a_failed_add_of_the_rest_keeps_what_was_queued(
        self, queue, household, tasks, monkeypatch
    ):
        add_many = household.add_many_to_queue

        def refused(speaker_id: str, items: list[PlexItem]) -> None:
            household.plex_linked = False
            add_many(speaker_id, items)

        monkeypatch.setattr(household, "add_many_to_queue", refused)

        data = self.failure(queue, tasks, asked("artist", BAND, "play"))

        assert data["error"].startswith(
            f"Cannot add 2 Plex items to the queue of speaker {GARAGE} because"
        )
        assert keys(household, GARAGE) == NEW_TRACKS
        assert household.read_playback(GARAGE).transport_state is (
            TransportState.PLAYING
        )

    def test_plex_that_fails_in_the_task_leaves_the_queue_as_it_was(
        self, queue, household, tasks, plex
    ):
        plex.has_library = False

        data = self.failure(queue, tasks, asked("album", OLD_ALBUM, "play"))

        assert data == {
            "error": (
                "Cannot read Plex's music library because external service "
                "failed: Plex has no music library named 'Music'"
            )
        }
        assert keys(household, GARAGE) == TRACKS

    def test_an_artist_without_albums_fails_before_the_queue_changes(
        self, queue, household, tasks
    ):
        data = self.failure(queue, tasks, asked("artist", SILENT, "play"))

        assert data == {
            "error": (
                f"Cannot queue artist {SILENT} on speaker {GARAGE} because Plex "
                "has no albums of the artist"
            )
        }
        assert keys(household, GARAGE) == TRACKS

    def test_an_artist_plex_no_longer_has_fails_before_the_queue_changes(
        self, app, household, plex
    ):
        task = QueueingTask(
            household,
            plex,
            GARAGE,
            PlexItemKind.ARTIST,
            "99",
            "Gone",
            "play",
            newest_first=True,
        )
        handle = TaskProgressHandle(
            "a-task", app.container.sse_connection_manager(), LOCAL_ADMIN_TARGET
        )

        with pytest.raises(ItemNotFoundException, match="Artist 99 was not found"):
            task.execute(handle)

        assert keys(household, GARAGE) == TRACKS

    def test_a_failure_of_the_soco_client_names_no_address(self, app, plex, tasks):
        # The SoCo client's failure chains the one it met, whose message
        # names the speaker's address.
        soco_client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.6"],
                sonos_event_advertise_host="sonos-events.invalid",
                plex_url="http://192.0.2.1:32400",
                plex_token="the-suites-plex-token",
            ),
            health_service=app.container.health_service(),
        )
        task = QueueingTask(
            soco_client,
            plex,
            GARAGE,
            PlexItemKind.ALBUM,
            OLD_ALBUM,
            "Album 11",
            "play",
            newest_first=True,
        )

        started = app.container.task_service().start_task(task)
        (data,) = [
            event["data"]
            for event in tasks.finish(started.task_id, timeout=15.0)
            if event["event_type"] == "task_failed"
        ]

        assert list(data) == ["error"]
        assert data["error"].startswith(
            f"Cannot clear the queue of speaker {GARAGE} because external service "
            "failed: none of the 1 seed speakers answered"
        )
        assert "192.0.2" not in data["error"]
