"""Tests for the speaker service, on the in-memory household.

The service runs on the fake Sonos client and the real household mirror.
"""

import logging
import threading
from collections.abc import Callable

import pytest
from flask import Flask

from app.core_exceptions import ExternalServiceException
from app.exceptions import (
    ActionNotAvailableException,
    CommandRefusedException,
    SpeakerNotFoundException,
)
from app.services.household_mirror import HouseholdMirror, PlaybackAction
from app.services.sonos_client import Group, TransportState, Volume
from app.services.speaker_service import SpeakerService
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import FakeSonosClient, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
KITCHEN = "RINCON_FAKE0000000301400"
NOWHERE = "RINCON_FAKE0000000991400"

PLAY = PlaybackAction.PLAY
PAUSE = PlaybackAction.PAUSE
NEXT = PlaybackAction.NEXT
PREVIOUS = PlaybackAction.PREVIOUS

DID_NOT_ANSWER = "the speaker did not answer (ConnectTimeout)"


def _audible(volume: Volume) -> int:
    """How loud a speaker plays at a volume and mute: muted, not at all."""
    return 0 if volume.muted else volume.volume


def calls_from_here(
    household: FakeSonosClient, before_first: Callable[[], None] | None = None
) -> list[str]:
    """The room of each call the test's thread makes to a speaker from here
    on, as they come. The mirror's reads of a room whose group changed are
    made on its own threads and are not among them. ``before_first`` happens
    in the household before the first call."""
    thread = threading.current_thread()
    reached: list[str] = []

    def before_call(speaker_id: str) -> None:
        if threading.current_thread() is not thread:
            return
        reached.append(speaker_id)
        if before_first is not None and len(reached) == 1:
            before_first()

    household.before_call = before_call
    return reached


@pytest.fixture
def household(sonos: FakeSonosClient, mirror: HouseholdMirror) -> FakeSonosClient:
    """The Garage with three tracks queued and the Lounge with nothing,
    mirrored."""
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.add_room(LOUNGE, "Lounge", model="Playbase", volume=40)
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2), queue_track(3)])
    mirror.start()
    sonos.settle()
    return sonos


@pytest.fixture
def speakers(app: Flask, household: FakeSonosClient) -> SpeakerService:
    service: SpeakerService = app.container.speaker_service()
    return service


class TestSpeakerServiceCommand:
    def test_play_and_pause(self, speakers, household):
        speakers.command(GARAGE, PLAY)
        assert household.read_playback(GARAGE).transport_state is TransportState.PLAYING

        speakers.command(GARAGE, PAUSE)
        assert household.read_playback(GARAGE).transport_state is TransportState.PAUSED

    def test_next_and_previous(self, speakers, household):
        speakers.command(GARAGE, NEXT)
        assert household.read_playback(GARAGE).queue_position == 2
        household.settle()

        speakers.command(GARAGE, PREVIOUS)
        assert household.read_playback(GARAGE).queue_position == 1

    def test_pause_stops_what_the_speaker_cannot_pause(self, speakers, household):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        speakers.command(GARAGE, PAUSE)

        assert household.read_playback(GARAGE).transport_state is TransportState.STOPPED

    def test_a_command_goes_to_the_coordinator_of_the_group(self, speakers, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        speakers.command(LOUNGE, PLAY)
        speakers.command(LOUNGE, NEXT)

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert playback.queue_position == 2

    @pytest.mark.parametrize(
        ("action", "operation"),
        [
            (PLAY, f"play on speaker {LOUNGE}"),
            (PAUSE, f"pause speaker {LOUNGE}"),
            (NEXT, f"skip to the next track on speaker {LOUNGE}"),
            (PREVIOUS, f"go back to the previous track on speaker {LOUNGE}"),
        ],
    )
    def test_no_command_is_available_with_nothing_loaded(
        self, speakers, action, operation
    ):
        with pytest.raises(ActionNotAvailableException) as raised:
            speakers.command(LOUNGE, action)

        assert raised.value.error_code == "ACTION_NOT_AVAILABLE"
        assert raised.value.message == (
            f"Cannot {operation} because the speaker does not allow it now"
        )

    def test_a_command_the_speaker_does_not_allow_now_is_not_sent(
        self, speakers, household
    ):
        with pytest.raises(ActionNotAvailableException):
            speakers.command(GARAGE, PREVIOUS)

        household.start_stream(GARAGE, "Radio One")
        household.settle()
        with pytest.raises(ActionNotAvailableException):
            speakers.command(GARAGE, NEXT)

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert playback.transport_title == "Radio One"

    def test_what_is_allowed_is_what_the_mirror_holds(self, speakers, household):
        # The queue ends at its first track, and the event that says so has
        # not arrived.
        household.before_call = lambda speaker_id: household.load_queue(
            GARAGE, [queue_track(1)]
        )

        with pytest.raises(ActionNotAvailableException) as raised:
            speakers.command(GARAGE, NEXT)

        assert raised.value.message == (
            f"Cannot skip to the next track on speaker {GARAGE} because the "
            "speaker does not allow it now"
        )
        refusal = raised.value.__cause__
        assert isinstance(refusal, CommandRefusedException)
        assert refusal.cause == (
            "the speaker answered UPnP error 711 (Illegal seek target)"
        )

    def test_a_refused_stop_is_a_pause_that_is_not_available(
        self, speakers, household
    ):
        household.start_stream(GARAGE, "Radio One", "A Song")
        household.settle()
        # The stream ends and the queue is empty, and the event that says so
        # has not arrived: the mirror still lists stop.
        household.before_call = lambda speaker_id: household.load_queue(GARAGE, [])

        with pytest.raises(ActionNotAvailableException) as raised:
            speakers.command(GARAGE, PAUSE)

        assert raised.value.message == (
            f"Cannot pause speaker {GARAGE} because the speaker does not allow "
            "it now"
        )
        refusal = raised.value.__cause__
        assert isinstance(refusal, CommandRefusedException)
        assert refusal.operation == f"stop speaker {GARAGE}"
        assert refusal.cause == (
            "the speaker answered UPnP error 701 (Transition not available)"
        )

    def test_an_unknown_speaker_is_not_found(self, speakers):
        with pytest.raises(SpeakerNotFoundException) as raised:
            speakers.command(NOWHERE, PLAY)

        assert raised.value.message == f"Speaker {NOWHERE} was not found"

    def test_a_speaker_that_does_not_answer_fails(self, speakers, household):
        household.silence_room(GARAGE)
        try:
            with pytest.raises(ExternalServiceException) as raised:
                speakers.command(GARAGE, PLAY)
        finally:
            household.answer_again(GARAGE)

        assert raised.value.cause == DID_NOT_ANSWER

    def test_a_command_does_not_ask_plex(
        self, speakers, household, plex: FakePlexClient, caplog
    ):
        plex.reachable = False

        with caplog.at_level(logging.WARNING):
            speakers.command(GARAGE, PLAY)

        assert household.read_playback(GARAGE).transport_state is TransportState.PLAYING
        assert caplog.records == []


class TestSpeakerServiceVolume:
    def test_a_volume_is_set(self, speakers, household):
        assert speakers.change_volume(GARAGE, volume=35) == Volume(35, False)

        assert household.read_volume(GARAGE) == Volume(35, False)

    def test_a_delta_is_a_step_from_the_volume_as_it_is(self, speakers, household):
        assert speakers.change_volume(GARAGE, delta=5) == Volume(25, False)
        assert speakers.change_volume(GARAGE, delta=-10) == Volume(15, False)

    def test_the_speaker_keeps_a_step_within_its_range(self, speakers):
        assert speakers.change_volume(GARAGE, delta=-100) == Volume(0, False)
        assert speakers.change_volume(GARAGE, delta=100) == Volume(100, False)
        assert speakers.change_volume(GARAGE, delta=100) == Volume(100, False)

    def test_the_mute_is_set_with_the_volume(self, speakers, household):
        assert speakers.change_volume(GARAGE, volume=10, muted=True) == Volume(10, True)
        assert speakers.change_volume(GARAGE, delta=0) == Volume(10, True)
        assert speakers.change_volume(GARAGE, delta=0, muted=False) == Volume(10, False)

        assert household.read_volume(GARAGE) == Volume(10, False)

    @pytest.mark.parametrize(
        ("start", "change"),
        [
            (Volume(10, False), {"volume": 80, "muted": True}),
            (Volume(10, False), {"delta": 70, "muted": True}),
            (Volume(80, True), {"volume": 10, "muted": False}),
        ],
    )
    def test_it_never_plays_louder_than_where_it_starts_or_ends(
        self, speakers, household, start, change
    ):
        household.set_volume(GARAGE, start.volume)
        household.set_muted(GARAGE, start.muted)
        # The speaker's volume and mute before each call the service makes.
        heard: list[Volume] = []
        household.before_call = lambda speaker_id: heard.append(
            household.read_volume(GARAGE)
        )

        end = speakers.change_volume(GARAGE, **change)

        loudest = max(_audible(start), _audible(end))
        assert [
            _audible(volume) for volume in heard if _audible(volume) > loudest
        ] == []

    def test_it_moves_the_speaker_alone_in_a_group(self, speakers, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert speakers.change_volume(LOUNGE, volume=10) == Volume(10, False)

        assert household.read_volume(GARAGE) == Volume(20, False)

    def test_the_answer_is_the_speakers_and_not_the_mirrors(self, speakers, household):
        # Muted elsewhere, and the event that says so has not arrived.
        household.before_call = lambda speaker_id: household.set_muted(GARAGE, True)

        assert speakers.change_volume(GARAGE, volume=30) == Volume(30, True)

    def test_an_unknown_speaker_is_not_found(self, speakers):
        with pytest.raises(SpeakerNotFoundException):
            speakers.change_volume(NOWHERE, volume=10)

    def test_a_speaker_that_does_not_answer_fails(self, speakers, household):
        household.silence_room(GARAGE)
        try:
            with pytest.raises(ExternalServiceException) as raised:
                speakers.change_volume(GARAGE, volume=10)
        finally:
            household.answer_again(GARAGE)

        assert raised.value.operation == f"set the volume of speaker {GARAGE}"
        assert raised.value.cause == DID_NOT_ANSWER


class TestSpeakerServiceGroupVolume:
    def test_for_a_speaker_alone_it_is_the_speakers_volume(self, speakers, household):
        assert speakers.change_group_volume(GARAGE, volume=35) == Volume(35, False)

        assert household.read_volume(GARAGE) == Volume(35, False)

    def test_it_moves_every_speaker_of_the_group(self, speakers, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert speakers.change_group_volume(LOUNGE, volume=60) == Volume(60, False)

        assert household.read_volume(GARAGE) == Volume(40, False)
        assert household.read_volume(LOUNGE) == Volume(80, False)

    def test_a_delta_is_a_step_from_the_group_volume_as_it_is(
        self, speakers, household
    ):
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert speakers.change_group_volume(GARAGE, delta=-15) == Volume(15, False)
        assert speakers.change_group_volume(GARAGE, delta=-100) == Volume(0, False)

    def test_the_mute_is_every_speakers(self, speakers, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        muted = speakers.change_group_volume(GARAGE, delta=0, muted=True)

        assert muted == Volume(30, True)
        assert household.read_volume(GARAGE) == Volume(20, True)
        assert household.read_volume(LOUNGE) == Volume(40, True)
        assert speakers.change_group_volume(LOUNGE, delta=0, muted=False) == Volume(
            30, False
        )

    @pytest.mark.parametrize(
        ("start", "change"),
        [
            (Volume(10, False), {"volume": 80, "muted": True}),
            (Volume(10, False), {"delta": 70, "muted": True}),
            (Volume(80, True), {"volume": 10, "muted": False}),
        ],
    )
    def test_it_never_plays_louder_than_where_it_starts_or_ends(
        self, speakers, household, start, change
    ):
        household.set_group_volume(GARAGE, start.volume)
        household.set_group_muted(GARAGE, start.muted)
        heard: list[Volume] = []
        household.before_call = lambda speaker_id: heard.append(
            household.read_group_volume(GARAGE)
        )

        end = speakers.change_group_volume(GARAGE, **change)

        loudest = max(_audible(start), _audible(end))
        assert [
            _audible(volume) for volume in heard if _audible(volume) > loudest
        ] == []

    def test_an_unknown_speaker_is_not_found(self, speakers):
        with pytest.raises(SpeakerNotFoundException):
            speakers.change_group_volume(NOWHERE, delta=1)

    def test_a_speaker_that_does_not_answer_fails(self, speakers, household):
        household.silence_room(GARAGE)
        try:
            with pytest.raises(ExternalServiceException) as raised:
                speakers.change_group_volume(GARAGE, delta=1)
        finally:
            household.answer_again(GARAGE)

        assert raised.value.operation == f"change the group volume of speaker {GARAGE}"
        assert raised.value.cause == DID_NOT_ANSWER


class TestSpeakerServiceGroups:
    @pytest.fixture
    def three_rooms(self, household):
        """The household with a Kitchen, mirrored."""
        household.add_room(KITCHEN, "Kitchen", model="One", volume=60)
        household.settle()
        return household

    def test_a_speaker_joins_the_group_of_another(self, speakers, household):
        sent = calls_from_here(household)

        speakers.join_group(LOUNGE, GARAGE)

        assert household.read_household().groups == (Group(GARAGE, (GARAGE, LOUNGE)),)
        assert sent == [LOUNGE]

    def test_the_group_is_named_by_any_speaker_of_it(self, speakers, three_rooms):
        three_rooms.join(LOUNGE, GARAGE)
        three_rooms.settle()

        speakers.join_group(KITCHEN, LOUNGE)

        assert three_rooms.read_household().groups == (
            Group(GARAGE, (GARAGE, LOUNGE, KITCHEN)),
        )

    def test_a_speaker_moves_from_one_group_to_another(self, speakers, three_rooms):
        three_rooms.join(LOUNGE, GARAGE)
        three_rooms.settle()

        speakers.join_group(LOUNGE, KITCHEN)

        assert three_rooms.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(KITCHEN, (LOUNGE, KITCHEN)),
        )

    def test_a_coordinator_joins_another_group(self, speakers, three_rooms):
        three_rooms.join(LOUNGE, GARAGE)
        three_rooms.settle()

        speakers.join_group(GARAGE, KITCHEN)

        assert three_rooms.read_household().groups == (
            Group(LOUNGE, (LOUNGE,)),
            Group(KITCHEN, (GARAGE, KITCHEN)),
        )

    @pytest.mark.parametrize(
        ("speaker_id", "member_id"),
        [
            pytest.param(LOUNGE, LOUNGE, id="the speaker itself"),
            pytest.param(LOUNGE, GARAGE, id="the coordinator of its group"),
            pytest.param(GARAGE, LOUNGE, id="a speaker of the group it leads"),
            pytest.param(GARAGE, GARAGE, id="the coordinator itself"),
            pytest.param(KITCHEN, KITCHEN, id="a speaker alone, itself"),
        ],
    )
    def test_a_join_to_the_group_the_speaker_plays_in_sends_nothing(
        self, speakers, three_rooms, speaker_id, member_id
    ):
        three_rooms.join(LOUNGE, GARAGE)
        three_rooms.settle()
        sent = calls_from_here(three_rooms)

        speakers.join_group(speaker_id, member_id)

        assert sent == []

    def test_the_group_a_speaker_plays_in_is_what_the_mirror_holds(
        self, speakers, household
    ):
        # The Lounge joined the Garage elsewhere, and the event that says so
        # has not arrived: the join is sent all the same.
        sent = calls_from_here(household, lambda: household.join(LOUNGE, GARAGE))

        speakers.join_group(LOUNGE, GARAGE)

        assert sent == [LOUNGE]

    @pytest.mark.parametrize(
        ("speaker_id", "member_id", "unknown"),
        [
            pytest.param(NOWHERE, GARAGE, NOWHERE, id="the speaker that joins"),
            pytest.param(LOUNGE, NOWHERE, NOWHERE, id="the speaker of the group"),
            pytest.param(NOWHERE, "Garage", NOWHERE, id="both, the first named"),
        ],
    )
    def test_an_unknown_speaker_is_not_found_and_nothing_is_sent(
        self, speakers, household, speaker_id, member_id, unknown
    ):
        sent = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException) as raised:
            speakers.join_group(speaker_id, member_id)

        assert raised.value.message == f"Speaker {unknown} was not found"
        assert sent == []

    def test_a_speaker_that_does_not_answer_fails_the_join(self, speakers, household):
        household.silence_room(LOUNGE)
        try:
            with pytest.raises(ExternalServiceException) as raised:
                speakers.join_group(LOUNGE, GARAGE)
        finally:
            household.answer_again(LOUNGE)

        assert raised.value.operation == (
            f"join the group of speaker {GARAGE} with speaker {LOUNGE}"
        )
        assert raised.value.cause == DID_NOT_ANSWER

    def test_a_speaker_leaves_its_group(self, speakers, household):
        household.join(LOUNGE, GARAGE)
        household.settle()
        sent = calls_from_here(household)

        speakers.leave_group(LOUNGE)

        assert household.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(LOUNGE, (LOUNGE,)),
        )
        assert sent == [LOUNGE]

    def test_a_coordinator_leaves_the_group_it_leads(self, speakers, three_rooms):
        three_rooms.join(LOUNGE, GARAGE)
        three_rooms.join(KITCHEN, GARAGE)
        three_rooms.settle()

        speakers.leave_group(GARAGE)

        assert three_rooms.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(LOUNGE, (LOUNGE, KITCHEN)),
        )

    def test_a_leave_by_a_speaker_alone_sends_nothing(self, speakers, household):
        sent = calls_from_here(household)

        speakers.leave_group(LOUNGE)

        assert sent == []
        assert household.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(LOUNGE, (LOUNGE,)),
        )

    def test_whether_a_speaker_is_alone_is_what_the_mirror_holds(
        self, speakers, household
    ):
        household.join(LOUNGE, GARAGE)
        household.settle()
        # The Lounge left the group elsewhere, and the event that says so has
        # not arrived: the leave is sent all the same.
        sent = calls_from_here(household, lambda: household.unjoin(LOUNGE))

        speakers.leave_group(LOUNGE)

        assert sent == [LOUNGE]

    def test_a_leave_by_an_unknown_speaker_is_not_found(self, speakers, household):
        sent = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException) as raised:
            speakers.leave_group(NOWHERE)

        assert raised.value.message == f"Speaker {NOWHERE} was not found"
        assert sent == []

    def test_a_speaker_that_does_not_answer_fails_the_leave(self, speakers, household):
        household.join(LOUNGE, GARAGE)
        household.settle()
        household.silence_room(LOUNGE)
        try:
            with pytest.raises(ExternalServiceException) as raised:
                speakers.leave_group(LOUNGE)
        finally:
            household.answer_again(LOUNGE)

        assert raised.value.operation == f"ungroup speaker {LOUNGE}"
        assert raised.value.cause == DID_NOT_ANSWER
