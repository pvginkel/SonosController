"""Tests for the household mirror, on the in-memory household.

The mirror runs on the fake Sonos client and the real SSE connection manager;
a change made "elsewhere" is a call on the fake, as the Sonos app or another
display would make it.
"""

import logging
import time
from collections.abc import Callable

import pytest

from app.core_exceptions import ExternalServiceException
from app.exceptions import SpeakerNotFoundException
from app.services.event_sender import COALESCE_SECONDS
from app.services.household_mirror import (
    HouseholdMirror,
    PlaybackAction,
    PlaybackSource,
    PlaybackStatus,
    PlayingTrack,
    Speaker,
    SpeakerGroup,
)
from app.services.sonos_client import SonosService, Track
from app.utils.lifecycle_coordinator import LifecycleCoordinator
from tests.fakes.sonos_client import FakeSonosClient, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
KITCHEN = "RINCON_FAKE0000000301400"
LOUNGE_SURROUNDS = ("RINCON_FAKE0000000211400", "RINCON_FAKE0000000221400")

PLAY = PlaybackAction.PLAY
PAUSE = PlaybackAction.PAUSE
NEXT = PlaybackAction.NEXT
PREVIOUS = PlaybackAction.PREVIOUS


def speakers_changed() -> dict:
    return {"type": "speakers_changed", "payload": {}}


def volume_changed(speaker_id: str) -> dict:
    return {"type": "volume_changed", "payload": {"speaker_id": speaker_id}}


def playback_changed(speaker_id: str) -> dict:
    return {"type": "playback_changed", "payload": {"speaker_id": speaker_id}}


def in_any_order(events: list[dict]) -> list[dict]:
    return sorted(events, key=lambda event: (event["type"], str(event["payload"])))


def eventually(condition: Callable[[], bool]) -> None:
    """Wait for what the mirror's healing thread does: ``settle()`` waits for
    the household's events alone."""
    deadline = time.perf_counter() + 5.0
    while not condition():
        assert time.perf_counter() < deadline, "the mirror did not heal in time"
        time.sleep(0.01)


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """What the mirror logged, each line with its level."""
    return [
        (record.levelname, record.message)
        for record in caplog.records
        if record.name == "app.services.household_mirror"
    ]


def once(change: Callable[[], None]) -> Callable[[str], None]:
    """A ``before_call`` that makes the change before the first call that
    reaches a room, and not again."""
    made = []

    def before_call(speaker_id: str) -> None:
        if not made:
            made.append(speaker_id)
            change()

    return before_call


@pytest.fixture
def household(sonos: FakeSonosClient) -> FakeSonosClient:
    """Two rooms: one device with three tracks queued, and a room of three."""
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.add_room(
        LOUNGE, "Lounge", model="Playbase", volume=40, hidden_device_ids=LOUNGE_SURROUNDS
    )
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2), queue_track(3)])
    return sonos


@pytest.fixture
def started(household: FakeSonosClient, mirror: HouseholdMirror) -> HouseholdMirror:
    """The mirror, holding the household."""
    mirror.start()
    household.settle()
    return mirror


class TestHouseholdMirrorSpeakers:
    def test_a_speaker_is_a_room_with_its_volume_and_its_group(self, started):
        assert started.speaker(GARAGE) == Speaker(
            id=GARAGE,
            name="Garage",
            model="Play:3",
            volume=20,
            muted=False,
            group=SpeakerGroup(
                coordinator_id=GARAGE, member_ids=(GARAGE,), volume=20, muted=False
            ),
        )

    def test_speakers_are_every_room_by_name(self, household, mirror):
        household.add_room(KITCHEN, "attic")
        mirror.start()

        assert [speaker.name for speaker in mirror.speakers()] == [
            "attic",
            "Garage",
            "Lounge",
        ]

    def test_a_hidden_device_is_not_a_speaker(self, started):
        assert [speaker.id for speaker in started.speakers()] == [GARAGE, LOUNGE]
        with pytest.raises(SpeakerNotFoundException):
            started.speaker(LOUNGE_SURROUNDS[0])

    def test_an_unknown_speaker_is_not_found(self, started):
        with pytest.raises(SpeakerNotFoundException) as error:
            started.speaker("RINCON_UNKNOWN")
        assert error.value.error_code == "SPEAKER_NOT_FOUND"
        assert error.value.message == "Speaker RINCON_UNKNOWN was not found"

        with pytest.raises(SpeakerNotFoundException):
            started.playback("RINCON_UNKNOWN")

    def test_the_rooms_of_a_group_share_its_coordinator_and_its_volume(
        self, household, mirror
    ):
        household.join(LOUNGE, GARAGE)
        mirror.start()

        group = SpeakerGroup(
            coordinator_id=GARAGE, member_ids=(GARAGE, LOUNGE), volume=30, muted=False
        )
        assert mirror.speaker(GARAGE).group == group
        assert mirror.speaker(LOUNGE).group == group
        assert mirror.speaker(GARAGE).volume == 20
        assert mirror.speaker(LOUNGE).volume == 40

    def test_a_volume_change_made_elsewhere_is_mirrored(self, started, household):
        household.set_volume(GARAGE, 35)
        household.set_muted(GARAGE, True)
        household.settle()

        speaker = started.speaker(GARAGE)
        assert (speaker.volume, speaker.muted) == (35, True)
        assert (speaker.group.volume, speaker.group.muted) == (35, True)

    def test_a_group_volume_change_is_mirrored_for_every_room(self, household, mirror):
        household.join(LOUNGE, GARAGE)
        mirror.start()
        household.settle()

        household.set_group_volume(LOUNGE, 60)
        household.settle()

        assert mirror.speaker(GARAGE).volume == 40
        assert mirror.speaker(LOUNGE).volume == 80
        assert mirror.speaker(GARAGE).group.volume == 60
        assert mirror.speaker(LOUNGE).group.volume == 60

    def test_it_answers_without_asking_a_speaker(self, started, household):
        household.reachable = False

        assert len(started.speakers()) == 2
        assert started.speaker(GARAGE).volume == 20
        assert started.playback(GARAGE).queue_length == 3

    def test_a_name_and_a_model_are_nfc(self, sonos, mirror):
        sonos.add_room(GARAGE, "Café", model="Play:3 é")
        mirror.start()

        speaker = mirror.speaker(GARAGE)
        assert speaker.name == "Café"
        assert speaker.model == "Play:3 é"

    def test_it_holds_nothing_before_it_starts(self, household, mirror):
        assert mirror.speakers() == []
        with pytest.raises(SpeakerNotFoundException):
            mirror.speaker(GARAGE)


class TestHouseholdMirrorPlayback:
    def test_a_queued_plex_track(self, started):
        playback = started.playback(GARAGE)

        assert playback.state is PlaybackStatus.STOPPED
        assert playback.source is PlaybackSource.QUEUE
        assert playback.actions == (PLAY, PAUSE, NEXT)
        assert playback.shuffle is False
        assert (playback.queue_position, playback.queue_length) == (1, 3)
        assert playback.track == PlayingTrack(
            title="Track 1",
            artist="An Artist",
            album="An Album",
            duration_ms=181000,
            track_id="1001",
        )

    def test_nothing_loaded(self, started):
        playback = started.playback(LOUNGE)

        assert playback.state is PlaybackStatus.STOPPED
        assert playback.source is PlaybackSource.QUEUE
        assert playback.actions == ()
        assert (playback.queue_position, playback.queue_length) == (None, 0)
        assert playback.track is None

    def test_playing_pausing_and_stopping_elsewhere_is_mirrored(
        self, started, household
    ):
        household.play(GARAGE)
        household.settle()
        assert started.playback(GARAGE).state is PlaybackStatus.PLAYING

        household.pause(GARAGE)
        household.settle()
        assert started.playback(GARAGE).state is PlaybackStatus.PAUSED

        household.stop(GARAGE)
        household.settle()
        assert started.playback(GARAGE).state is PlaybackStatus.STOPPED

    def test_a_transport_that_waits_is_buffering(self, started, household):
        household.start_buffering(GARAGE)
        household.settle()

        assert started.playback(GARAGE).state is PlaybackStatus.BUFFERING

    def test_a_partial_event_is_merged_into_what_is_held(self, started, household):
        # A track change arrives without the transport's address, and its
        # actions in an event of their own.
        household.next(GARAGE)
        household.settle()

        playback = started.playback(GARAGE)
        assert playback.source is PlaybackSource.QUEUE
        assert playback.actions == (PLAY, PAUSE, NEXT, PREVIOUS)
        assert (playback.queue_position, playback.queue_length) == (2, 3)
        assert playback.track.title == "Track 2"
        assert playback.track.track_id == "1002"

        household.next(GARAGE)
        household.settle()
        assert started.playback(GARAGE).actions == (PLAY, PAUSE, PREVIOUS)

    def test_shuffle_is_mirrored(self, started, household):
        household.set_shuffle(GARAGE, True)
        household.settle()

        assert started.playback(GARAGE).shuffle is True

    def test_a_radio_stream_can_be_paused_and_not_skipped(self, started, household):
        household.start_stream(GARAGE, "Radio One", "An Artist - A Song")
        household.settle()

        playback = started.playback(GARAGE)
        assert playback.state is PlaybackStatus.PLAYING
        assert playback.source is PlaybackSource.RADIO
        # The speaker allows Play and Stop: the API's pause stops a stream.
        assert playback.actions == (PLAY, PAUSE)
        assert (playback.queue_position, playback.queue_length) == (None, None)
        assert playback.track == PlayingTrack(
            title="An Artist - A Song",
            artist="Radio One",
            album=None,
            duration_ms=None,
            track_id=None,
        )

    def test_a_stream_that_reports_nothing_shows_its_station(self, started, household):
        household.start_stream(GARAGE, "Radio One")
        household.settle()
        assert started.playback(GARAGE).track.title == "Radio One"
        assert started.playback(GARAGE).track.artist == "Radio One"

        household.report_stream_content(GARAGE, "Another Song")
        household.settle()
        assert started.playback(GARAGE).track.title == "Another Song"

    @pytest.mark.parametrize(
        ("scheme", "source"),
        [
            ("x-rincon-mp3radio", PlaybackSource.RADIO),
            ("x-sonosapi-stream", PlaybackSource.RADIO),
            ("x-sonosapi-radio", PlaybackSource.RADIO),
            ("x-sonosapi-hls", PlaybackSource.RADIO),
            ("aac", PlaybackSource.RADIO),
            ("hls-radio", PlaybackSource.RADIO),
            ("x-rincon-stream", PlaybackSource.LINE_IN),
            ("x-sonos-htastream", PlaybackSource.TV),
            ("x-sonos-vli", PlaybackSource.OTHER),
        ],
    )
    def test_the_source_is_read_from_the_transports_address(
        self, started, household, scheme, source
    ):
        household.start_stream(GARAGE, "Something", scheme=scheme)
        household.settle()

        assert started.playback(GARAGE).source is source

    def test_a_track_that_is_not_from_plex_has_no_track_id(self, started, household):
        household.load_queue(
            GARAGE,
            [
                Track(
                    title="A Song",
                    artist="A Band",
                    album="",
                    stream_content="",
                    duration_seconds=0,
                    uri="x-file-cifs://nas.invalid/music/a-song.mp3",
                    picture="/getaa?u=x-file-cifs%3a%2f%2fnas.invalid%2fa-song.mp3",
                )
            ],
        )
        household.settle()

        assert started.playback(GARAGE).track == PlayingTrack(
            title="A Song", artist="A Band", album=None, duration_ms=None, track_id=None
        )

    def test_a_tracks_texts_are_nfc(self, started, household):
        household.load_queue(
            GARAGE,
            [queue_track(1, album="Björk Album", artist="Björk")],
        )
        household.start_stream(LOUNGE, "Radio Café", "Sigur Rós - Saéglópur")
        household.settle()

        track = started.playback(GARAGE).track
        assert (track.artist, track.album) == ("Björk", "Björk Album")
        stream = started.playback(LOUNGE).track
        assert stream.title == "Sigur Rós - Saéglópur"
        assert stream.artist == "Radio Café"

    def test_a_room_that_follows_plays_what_its_coordinator_plays(
        self, household, mirror
    ):
        household.join(LOUNGE, GARAGE)
        mirror.start()
        household.settle()

        household.play(LOUNGE)
        household.settle()

        assert mirror.playback(LOUNGE) == mirror.playback(GARAGE)
        assert mirror.playback(LOUNGE).state is PlaybackStatus.PLAYING
        assert mirror.playback(LOUNGE).track.title == "Track 1"


class TestHouseholdMirrorLayout:
    def test_a_rename_is_mirrored(self, started, household):
        household.rename_room(GARAGE, "Workshop")
        household.settle()

        assert started.speaker(GARAGE).name == "Workshop"

    def test_a_room_that_joins_a_group_plays_what_the_group_plays(
        self, started, household
    ):
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert started.speaker(LOUNGE).group == SpeakerGroup(
            coordinator_id=GARAGE, member_ids=(GARAGE, LOUNGE), volume=30, muted=False
        )
        assert started.playback(LOUNGE).track.title == "Track 1"

    def test_a_room_that_leaves_a_group_is_read_again(self, household, mirror):
        # Mirrored while it followed the Garage: what was read of it then was
        # the group's, and its own device's events were not the group's.
        household.join(LOUNGE, GARAGE)
        mirror.start()
        household.settle()
        household.set_group_volume(GARAGE, 60)
        household.settle()

        household.unjoin(LOUNGE)
        household.settle()

        lounge = mirror.speaker(LOUNGE)
        assert lounge.group == SpeakerGroup(
            coordinator_id=LOUNGE, member_ids=(LOUNGE,), volume=80, muted=False
        )
        assert mirror.playback(LOUNGE).track is None
        assert mirror.playback(LOUNGE).queue_length == 0
        assert mirror.speaker(GARAGE).group.member_ids == (GARAGE,)

    def test_a_room_that_appears_is_mirrored(self, started, household):
        household.add_room(KITCHEN, "Kitchen", model="One", volume=15)
        household.settle()

        assert [speaker.name for speaker in started.speakers()] == [
            "Garage",
            "Kitchen",
            "Lounge",
        ]
        assert started.speaker(KITCHEN).model == "One"
        assert household.subscription_count(KITCHEN) == 4

        household.set_volume(KITCHEN, 16)
        household.settle()
        assert started.speaker(KITCHEN).volume == 16

    def test_a_room_that_goes_is_dropped(self, started, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        household.remove_room(GARAGE)
        household.settle()

        assert [speaker.id for speaker in started.speakers()] == [LOUNGE]
        with pytest.raises(SpeakerNotFoundException):
            started.playback(GARAGE)
        assert started.speaker(LOUNGE).group.coordinator_id == LOUNGE


class TestHouseholdMirrorSubscriptions:
    def test_it_subscribes_to_the_four_services_of_every_room(self, started, household):
        for speaker_id in (GARAGE, LOUNGE):
            for service in SonosService:
                assert household.subscription_count(speaker_id, service) == 1

    def test_what_it_reads_is_not_overwritten_by_a_subscriptions_first_event(
        self, household, mirror
    ):
        # The device starts a new subscription with a group volume the room
        # has left; the read says what it is now.
        household.lag_group_volume(GARAGE, 55)
        first_events = []
        probe = household.subscribe(
            GARAGE,
            SonosService.GROUP_RENDERING_CONTROL,
            first_events.append,
            lambda: None,
        )
        household.settle()
        probe.unsubscribe()
        assert first_events[0].volume == 55

        mirror.start()
        household.settle()

        assert mirror.speaker(GARAGE).group.volume == 20

    def test_a_change_made_while_start_reads_the_household_is_kept(
        self, household, mirror
    ):
        # The Garage is subscribed to and read before the Lounge. A change to
        # the Garage made after that arrives as an event before start() holds
        # what it read.
        changed = []

        def change_the_garage_once_it_is_read(speaker_id):
            if speaker_id == LOUNGE and not changed:
                changed.append(speaker_id)
                household.set_volume(GARAGE, 33)
                household.settle()

        household.before_call = change_the_garage_once_it_is_read
        mirror.start()
        household.settle()

        assert changed == [LOUNGE]
        assert mirror.speaker(GARAGE).volume == 33
        assert mirror.speaker(GARAGE).group.volume == 33

    def test_stop_ends_every_subscription_and_empties_the_mirror(
        self, started, household
    ):
        started.stop()

        assert household.subscription_count(GARAGE) == 0
        assert household.subscription_count(LOUNGE) == 0
        assert started.speakers() == []

    def test_a_layout_that_arrives_after_stop_changes_nothing(
        self, started, household
    ):
        started.stop()

        household.add_room(KITCHEN, "Kitchen")
        household.settle()

        assert started.speakers() == []
        assert household.subscription_count(KITCHEN) == 0

    def test_it_starts_and_stops_with_the_apps_lifecycle(self, app, household):
        coordinator = LifecycleCoordinator(graceful_shutdown_timeout=1)
        mirror = HouseholdMirror(
            sonos_client=household,
            event_sender=app.container.event_sender(),
            lifecycle_coordinator=coordinator,
        )
        assert mirror.speakers() == []

        coordinator.fire_startup()
        assert len(mirror.speakers()) == 2
        assert household.subscription_count(GARAGE) == 4

        coordinator.shutdown()
        assert mirror.speakers() == []
        assert household.subscription_count(GARAGE) == 0

    def test_the_container_builds_it_on_the_sonos_client(self, mirror, household):
        mirror.start()

        assert isinstance(mirror, HouseholdMirror)
        assert mirror.speaker(GARAGE).name == "Garage"

    def test_start_mirrors_nothing_when_no_speaker_answers(self, household, mirror):
        household.reachable = False

        mirror.start()

        assert mirror.speakers() == []

    def test_a_room_that_fails_leaves_nothing_mirrored(self, household, mirror):
        household.silence_room(LOUNGE)

        mirror.start()

        assert mirror.speakers() == []
        assert household.subscription_count(GARAGE) == 0
        assert household.subscription_count(LOUNGE) == 0

    def test_stop_ends_the_other_subscriptions_when_a_speaker_fails(
        self, started, household
    ):
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException):
            started.stop()

        assert household.subscription_count(GARAGE) == 0
        assert household.subscription_count(LOUNGE) == 0
        assert started.speakers() == []


class TestHouseholdMirrorEvents:
    def test_a_volume_change_made_elsewhere_reaches_every_connection(
        self, started, household, connect
    ):
        first, second = connect(), connect()

        household.set_volume(GARAGE, 35)
        household.settle()

        # The room's volume and its group's both changed: one event, and the
        # last of the two after it.
        assert first.events() == [volume_changed(GARAGE)] * 2
        assert second.events() == [volume_changed(GARAGE)] * 2

    def test_a_groups_volume_goes_out_for_every_member(
        self, household, mirror, connect
    ):
        household.add_room(KITCHEN, "Kitchen")
        household.join(LOUNGE, GARAGE)
        mirror.start()
        household.settle()
        stream = connect()

        household.set_volume(LOUNGE, 50)
        household.settle()

        # The Lounge's own volume, and the group's for both members; the
        # Kitchen is not in the group.
        assert in_any_order(stream.events()) == [
            volume_changed(GARAGE),
            volume_changed(LOUNGE),
            volume_changed(LOUNGE),
        ]

    def test_a_groups_playback_goes_out_for_every_member(
        self, household, mirror, connect
    ):
        household.add_room(KITCHEN, "Kitchen")
        household.join(LOUNGE, GARAGE)
        mirror.start()
        household.settle()
        stream = connect()

        household.pause(LOUNGE)
        household.settle()

        assert in_any_order(stream.events()) == [
            playback_changed(GARAGE),
            playback_changed(LOUNGE),
        ]

    def test_a_burst_of_changes_is_coalesced(self, started, household, connect):
        stream = connect()
        began = time.perf_counter()

        for volume in range(21, 41):
            household.set_volume(GARAGE, volume)
        for _ in range(2):
            household.next(GARAGE)
        household.settle()
        windows = (time.perf_counter() - began) // COALESCE_SECONDS

        # Forty volume events and four playback events of the speaker: one of
        # each type at once, one for every 250 ms the burst took, and the
        # last one after it.
        events = stream.events()
        for event in (volume_changed(GARAGE), playback_changed(GARAGE)):
            assert 2 <= events.count(event) <= 2 + windows
        assert len(events) <= 4 + 2 * windows
        assert started.speaker(GARAGE).volume == 40
        assert started.playback(GARAGE).queue_position == 3

    def test_an_event_that_changes_nothing_sends_nothing(
        self, started, household, connect
    ):
        stream = connect()

        household.set_volume(GARAGE, 20)
        household.set_shuffle(GARAGE, False)
        household.settle()

        assert stream.events() == []

    def test_a_rename_sends_speakers_changed(self, started, household, connect):
        stream = connect()

        household.rename_room(GARAGE, "Workshop")
        household.settle()

        # Every room's device reports the layout: one event all the same.
        assert stream.events() == [speakers_changed()]

    def test_a_room_read_again_that_has_not_changed_sends_nothing(
        self, started, household, connect
    ):
        stream = connect()

        household.move_room(GARAGE)
        household.settle()

        assert stream.events() == []

    def test_joining_a_group_changes_the_speakers_and_what_the_room_plays(
        self, started, household, connect
    ):
        stream = connect()

        household.join(LOUNGE, GARAGE)
        household.settle()

        # Both rooms' group changed, and with it their group volume; the
        # Lounge now plays what the Garage plays. The Garage's own event for
        # its group's new volume follows. The Lounge's device reports its own
        # transport, which names the Garage: that is not the group's playback,
        # and the Garage's has not changed.
        assert in_any_order(stream.events()) == [
            playback_changed(LOUNGE),
            speakers_changed(),
            volume_changed(GARAGE),
            volume_changed(GARAGE),
            volume_changed(LOUNGE),
            volume_changed(LOUNGE),
        ]

    def test_leaving_a_group_changes_the_speakers_and_what_the_room_plays(
        self, household, mirror, connect
    ):
        household.join(LOUNGE, GARAGE)
        mirror.start()
        household.settle()
        stream = connect()

        household.unjoin(LOUNGE)
        household.settle()

        events = stream.events()
        assert speakers_changed() in events
        assert playback_changed(LOUNGE) in events
        assert playback_changed(GARAGE) not in events
        assert volume_changed(LOUNGE) in events
        assert volume_changed(GARAGE) in events

    def test_a_room_that_appears_or_goes_sends_speakers_changed(
        self, started, household, connect
    ):
        stream = connect()

        household.add_room(KITCHEN, "Kitchen")
        household.settle()
        assert stream.events() == [speakers_changed()]

        household.remove_room(KITCHEN)
        household.settle()
        assert stream.events() == [speakers_changed()]


class TestHouseholdMirrorHealing:
    """What the speakers' events set off: the layout they send, and a renewal
    that failed. The mirror's own reads of the layout are not waited for."""

    def test_a_failed_renewal_is_subscribed_to_again_and_read_again(
        self, started, household, connect
    ):
        stream = connect()
        # Made while the room has no subscription: no event reports it.
        household.before_call = once(lambda: household.set_volume(GARAGE, 33))

        household.fail_renewals(GARAGE)
        eventually(lambda: started.speaker(GARAGE).volume == 33)

        assert started.speaker(GARAGE).group.volume == 33
        assert household.subscription_count(GARAGE) == 4
        assert stream.events() == [volume_changed(GARAGE)]

        household.set_volume(GARAGE, 34)
        household.play(GARAGE)
        household.settle()
        assert started.speaker(GARAGE).volume == 34
        assert started.playback(GARAGE).state is PlaybackStatus.PLAYING

    def test_a_failed_renewal_leaves_the_other_rooms_as_they_are(
        self, started, household
    ):
        reached = []
        household.before_call = reached.append

        household.fail_renewals(GARAGE)

        # Four subscriptions and the four reads of a room, and no more.
        eventually(lambda: len(reached) == 8)
        assert reached == [GARAGE] * 8
        assert household.subscription_count(LOUNGE) == 4

    def test_a_speaker_that_restarted_is_subscribed_to_again_and_read_again(
        self, started, household, connect
    ):
        household.play(GARAGE)
        household.settle()
        stream = connect()
        stream.events()
        # A restarted device plays nothing, and reports nothing: it has
        # forgotten its subscriptions.
        household.before_call = once(lambda: household.stop(GARAGE))

        household.restart_room(GARAGE)
        household.settle()

        assert started.playback(GARAGE).state is PlaybackStatus.STOPPED
        assert household.subscription_count(GARAGE) == 4
        assert stream.events() == [playback_changed(GARAGE)]

        household.play(GARAGE)
        household.settle()
        assert started.playback(GARAGE).state is PlaybackStatus.PLAYING

    def test_a_speaker_with_a_new_address_is_subscribed_to_again(
        self, started, household
    ):
        address = household.move_room(GARAGE)
        household.settle()

        assert household.subscribed_addresses(GARAGE) == {address}
        assert household.subscription_count(GARAGE) == 4

        household.set_volume(GARAGE, 21)
        household.settle()
        assert started.speaker(GARAGE).volume == 21

    def test_a_coordinator_read_again_changes_what_its_group_plays(
        self, started, household, connect
    ):
        household.join(LOUNGE, GARAGE)
        household.play(GARAGE)
        household.settle()
        stream = connect()
        stream.events()
        household.before_call = once(lambda: household.stop(GARAGE))

        household.restart_room(GARAGE)
        household.settle()

        assert started.playback(LOUNGE).state is PlaybackStatus.STOPPED
        assert in_any_order(stream.events()) == [
            playback_changed(GARAGE),
            playback_changed(LOUNGE),
        ]

    def test_a_change_made_while_a_room_is_read_again_is_kept(
        self, started, household
    ):
        # A room's playback is read last, as the eighth call to it. A change
        # of volume made then, after the volume was read, arrives as an event
        # of the new subscriptions before the mirror holds what it read.
        calls = []

        def change_the_volume_once_it_is_read(speaker_id):
            calls.append(speaker_id)
            if len(calls) == 8:
                household.set_volume(GARAGE, 33)
                household.settle()

        household.before_call = change_the_volume_once_it_is_read
        household.fail_renewals(GARAGE)

        eventually(lambda: started.speaker(GARAGE).volume == 33)
        assert started.speaker(GARAGE).group.volume == 33

    def test_a_restarted_speaker_that_does_not_answer_keeps_what_is_held(
        self, started, household, connect, caplog
    ):
        stream = connect()
        household.silence_room(GARAGE)

        with caplog.at_level(logging.WARNING, "app.services.household_mirror"):
            household.restart_room(GARAGE)
            household.settle()

        assert started.speaker(GARAGE).volume == 20
        assert started.playback(GARAGE).queue_length == 3
        assert household.subscription_count(GARAGE) == 0
        assert stream.events() == []
        assert logged(caplog) == [
            (
                "WARNING",
                "The household is not mirrored in full; the layout is read again "
                "every 10.0 s: Cannot subscribe to AVTransport events of speaker "
                f"{GARAGE} because external service failed: timed out",
            )
        ]
        # The mirror's stop() ends the subscriptions it holds at the room.
        household.answer_again(GARAGE)

    def test_the_rest_of_a_layout_is_applied_when_a_held_room_does_not_answer(
        self, started, household
    ):
        household.silence_room(GARAGE)
        household.restart_room(GARAGE)
        household.rename_room(LOUNGE, "Living Room")
        household.settle()

        assert started.speaker(LOUNGE).name == "Living Room"
        assert started.speaker(GARAGE).volume == 20
        # The mirror's stop() ends the subscriptions it holds at the room.
        household.answer_again(GARAGE)

    def test_a_lost_subscription_of_a_room_that_went_is_not_healed(
        self, started, household
    ):
        household.fail_renewals(GARAGE)
        household.remove_room(GARAGE)
        household.settle()

        eventually(lambda: [speaker.id for speaker in started.speakers()] == [LOUNGE])
        assert household.subscription_count(GARAGE) == 0


class TestHouseholdMirrorChecks:
    """What the mirror's own reads of the layout heal, read here every 50 ms."""

    @pytest.fixture
    def check_seconds(self) -> float:
        return 0.05

    def test_a_household_that_does_not_answer_at_start_is_mirrored_once_it_does(
        self, household, mirror, connect
    ):
        household.reachable = False
        mirror.start()
        stream = connect()
        assert mirror.speakers() == []
        assert stream.events() == []

        household.reachable = True
        eventually(lambda: len(mirror.speakers()) == 2)

        assert mirror.playback(GARAGE).queue_length == 3
        assert household.subscription_count(GARAGE) == 4
        assert household.subscription_count(LOUNGE) == 4
        assert stream.events() == [speakers_changed()]

        household.set_volume(LOUNGE, 41)
        household.settle()
        assert mirror.speaker(LOUNGE).volume == 41

    def test_the_app_starts_when_no_speaker_answers(self, app, household):
        household.reachable = False
        coordinator = LifecycleCoordinator(graceful_shutdown_timeout=1)
        mirror = HouseholdMirror(
            sonos_client=household,
            event_sender=app.container.event_sender(),
            lifecycle_coordinator=coordinator,
            check_seconds=0.05,
        )

        coordinator.fire_startup()
        assert mirror.speakers() == []

        household.reachable = True
        eventually(lambda: len(mirror.speakers()) == 2)

        coordinator.shutdown()
        assert mirror.speakers() == []
        assert household.subscription_count(GARAGE) == 0

    def test_a_room_that_does_not_answer_at_start_is_mirrored_once_it_does(
        self, household, mirror
    ):
        household.silence_room(LOUNGE)
        mirror.start()
        assert mirror.speakers() == []

        household.answer_again(LOUNGE)
        eventually(lambda: len(mirror.speakers()) == 2)

        assert household.subscription_count(GARAGE) == 4
        assert household.subscription_count(LOUNGE) == 4

    def test_a_new_room_that_does_not_answer_is_mirrored_once_it_does(
        self, started, household
    ):
        household.silence_room(KITCHEN)
        household.add_room(KITCHEN, "Kitchen")
        household.settle()
        assert len(started.speakers()) == 2

        household.answer_again(KITCHEN)
        eventually(lambda: len(started.speakers()) == 3)

        assert household.subscription_count(KITCHEN) == 4

    def test_a_speaker_that_restarted_without_a_word_is_read_again(
        self, sonos, mirror, connect
    ):
        # A room alone in its household: no other room reports its restart.
        sonos.add_room(GARAGE, "Garage")
        sonos.load_queue(GARAGE, [queue_track(1), queue_track(2)])
        sonos.play(GARAGE)
        mirror.start()
        sonos.settle()
        stream = connect()
        stream.events()
        sonos.before_call = once(lambda: sonos.stop(GARAGE))

        sonos.restart_room(GARAGE)
        eventually(lambda: mirror.playback(GARAGE).state is PlaybackStatus.STOPPED)

        assert sonos.subscription_count(GARAGE) == 4
        assert stream.events() == [playback_changed(GARAGE)]

        sonos.play(GARAGE)
        sonos.settle()
        assert mirror.playback(GARAGE).state is PlaybackStatus.PLAYING

    def test_a_restarted_speaker_that_does_not_answer_is_read_again_once_it_does(
        self, started, household
    ):
        household.play(GARAGE)
        household.settle()
        household.silence_room(GARAGE)
        household.restart_room(GARAGE)
        household.settle()
        assert started.playback(GARAGE).state is PlaybackStatus.PLAYING

        household.answer_again(GARAGE)
        household.stop(GARAGE)
        eventually(lambda: started.playback(GARAGE).state is PlaybackStatus.STOPPED)

        assert household.subscription_count(GARAGE) == 4
        household.set_volume(GARAGE, 21)
        household.settle()
        assert started.speaker(GARAGE).volume == 21

    def test_a_room_that_does_not_answer_does_not_keep_another_from_healing(
        self, started, household
    ):
        household.silence_room(LOUNGE)
        household.restart_room(LOUNGE)
        household.restart_room(GARAGE)

        eventually(lambda: household.subscription_count(GARAGE) == 4)
        household.set_volume(GARAGE, 33)
        eventually(lambda: started.speaker(GARAGE).volume == 33)
        assert household.subscription_count(LOUNGE) == 0

        household.answer_again(LOUNGE)
        eventually(lambda: household.subscription_count(LOUNGE) == 4)

    def test_a_failure_is_logged_once_and_its_end_once(
        self, household, mirror, caplog
    ):
        household.reachable = False

        with caplog.at_level(logging.INFO, "app.services.household_mirror"):
            mirror.start()
            time.sleep(0.3)
            household.reachable = True
            eventually(lambda: len(mirror.speakers()) == 2)
            time.sleep(0.3)

        assert logged(caplog) == [
            (
                "WARNING",
                "The household is not mirrored in full; the layout is read again "
                "every 0.05 s: Cannot read the household's layout because "
                "external service failed: none of the seed speakers answered",
            ),
            ("INFO", "The household is mirrored in full again"),
        ]

    def test_nothing_is_read_after_stop(self, household, mirror):
        household.reachable = False
        mirror.start()
        mirror.stop()

        household.reachable = True
        time.sleep(0.3)

        assert mirror.speakers() == []
        assert household.subscription_count(GARAGE) == 0
