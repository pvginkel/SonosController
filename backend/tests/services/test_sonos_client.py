"""Tests for the Sonos client, on the in-memory household.

What ``SonosClient`` promises its callers, shown on ``FakeSonosClient``; the
live suite (``tests/live/``) shows the same of the SoCo client on the real
speakers.
"""

import pytest

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckStatus
from app.services.sonos_client import (
    UNREPORTED,
    Group,
    GroupVolumeChanged,
    HouseholdChanged,
    PlaybackChanged,
    SoCoSonosClient,
    SonosService,
    TransportAction,
    TransportState,
    Volume,
    VolumeChanged,
)
from tests.fakes.sonos_client import FakeSonosClient, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
LOUNGE_SURROUNDS = ("RINCON_FAKE0000000211400", "RINCON_FAKE0000000221400")

PLAY = TransportAction.PLAY
PAUSE = TransportAction.PAUSE
STOP = TransportAction.STOP
NEXT = TransportAction.NEXT
PREVIOUS = TransportAction.PREVIOUS


@pytest.fixture
def household(sonos: FakeSonosClient) -> FakeSonosClient:
    """Two rooms: one device with three tracks queued, and a room of three."""
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.add_room(
        LOUNGE, "Lounge", model="Playbase", volume=40, hidden_device_ids=LOUNGE_SURROUNDS
    )
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2), queue_track(3)])
    return sonos


class _HeldTopologySubscription:
    """A ZoneGroupTopology subscription a speaker granted, as SoCo counts one."""

    sid = "uuid:RINCON_FAKE0000000301400_sub0000000001"
    time_left = 86400

    def __init__(self, service) -> None:
        self.service = service


class _Listener:
    """Collects what a subscription delivers."""

    def __init__(self) -> None:
        self.events: list = []
        self.lost = 0

    def on_event(self, event) -> None:
        self.events.append(event)

    def on_lost(self) -> None:
        self.lost += 1


def _subscribe(sonos: FakeSonosClient, speaker_id: str, service: SonosService):
    """Subscribe, and drop the full event a subscription starts with."""
    listener = _Listener()
    subscription = sonos.subscribe(
        speaker_id, service, listener.on_event, listener.on_lost
    )
    sonos.settle()
    listener.events.clear()
    return listener, subscription


class TestSonosClientReadiness:
    def test_the_check_is_registered_and_not_critical(self, household, client):
        response = client.get("/health/readyz")

        assert response.status_code == 200
        assert response.get_json()["checks"]["sonos"] == {
            "status": "ok",
            "critical": False,
        }

    def test_a_household_that_does_not_answer_degrades_and_stays_ready(
        self, household, client
    ):
        household.reachable = False

        response = client.get("/health/readyz")

        body = response.get_json()
        assert response.status_code == 200
        assert body["ready"] is True
        assert body["status"] == "degraded"
        assert body["checks"]["sonos"] == {
            "status": "error",
            "critical": False,
            "detail": "none of the seed speakers answered",
        }

    def test_the_container_provides_the_soco_client(self, app):
        assert app.container.sonos_client.provides is SoCoSonosClient

    def test_the_soco_client_reports_seeds_that_do_not_answer(self, app):
        client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.1", "192.0.2.2"],
                sonos_event_advertise_host="sonos-events.invalid",
            ),
            health_service=app.container.health_service(),
        )

        result = client.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "none of the 2 seed speakers answered"


class TestSonosClientHousehold:
    def test_rooms_are_the_visible_devices(self, household):
        layout = household.read_household()

        assert [(room.id, room.name) for room in layout.rooms] == [
            (GARAGE, "Garage"),
            (LOUNGE, "Lounge"),
        ]
        assert all(room.address and room.boot_seq == 1 for room in layout.rooms)

    def test_a_room_alone_is_a_group_of_one(self, household):
        assert household.read_household().groups == (
            Group(coordinator_id=GARAGE, member_ids=(GARAGE,)),
            Group(coordinator_id=LOUNGE, member_ids=(LOUNGE,)),
        )

    def test_a_group_lists_its_rooms_under_its_coordinator(self, household):
        household.join(LOUNGE, GARAGE)

        assert household.read_household().groups == (
            Group(coordinator_id=GARAGE, member_ids=(GARAGE, LOUNGE)),
        )

    def test_a_hidden_device_is_not_a_room(self, household):
        with pytest.raises(ExternalServiceException, match="is not in the household"):
            household.read_volume(LOUNGE_SURROUNDS[0])

    def test_a_household_that_does_not_answer_fails(self, household):
        household.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            household.read_household()

        assert raised.value.operation == "read the household's layout"
        assert raised.value.cause == "none of the seed speakers answered"

    def test_the_soco_client_asks_a_speaker_while_soco_holds_a_topology_subscription(
        self, app
    ):
        client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.3"],
                sonos_event_advertise_host="sonos-events.invalid",
            ),
            health_service=app.container.health_service(),
        )
        # The client once its seed has answered and a topology subscription is
        # held: SoCo then answers its own polls from what the last event left.
        [seed] = client._candidates()
        seed._household_id = "Sonos_silent"
        held = _HeldTopologySubscription(seed.zoneGroupTopology)
        seed.zone_group_state.add_subscription(held)
        client._entry = seed

        try:
            with pytest.raises(ExternalServiceException) as raised:
                client.read_household()
        finally:
            seed.zone_group_state.remove_subscription(held)
            seed._household_id = None

        assert raised.value.operation == "read the household's layout"
        assert "none of the 1 seed speakers answered" in raised.value.cause

    def test_read_model(self, household):
        assert household.read_model(GARAGE) == "Play:3"
        assert household.read_model(LOUNGE) == "Playbase"

    def test_an_unknown_speaker_fails(self, household):
        with pytest.raises(ExternalServiceException, match="is not in the household"):
            household.read_model("RINCON_FAKE0000000991400")


class TestSonosClientPlayback:
    def test_read_playback_of_a_queue(self, household):
        playback = household.read_playback(GARAGE)

        assert playback.transport_state is TransportState.STOPPED
        assert playback.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert playback.transport_title == ""
        assert playback.actions == {PLAY, PAUSE, STOP, NEXT}
        assert playback.shuffle is False
        assert (playback.queue_position, playback.queue_length) == (1, 3)
        assert playback.track == queue_track(1)

    def test_read_playback_with_nothing_loaded(self, household):
        playback = household.read_playback(LOUNGE)

        assert playback.track is None
        assert playback.actions == frozenset()
        assert (playback.queue_position, playback.queue_length) == (0, 0)

    def test_play_pause_and_stop_move_the_transport(self, household):
        household.play(GARAGE)
        assert household.read_playback(GARAGE).transport_state is TransportState.PLAYING

        household.pause(GARAGE)
        assert household.read_playback(GARAGE).transport_state is TransportState.PAUSED

        household.stop(GARAGE)
        assert household.read_playback(GARAGE).transport_state is TransportState.STOPPED

    def test_next_and_previous_move_along_the_queue(self, household):
        household.next(GARAGE)

        playback = household.read_playback(GARAGE)
        assert playback.queue_position == 2
        assert playback.track == queue_track(2)
        assert playback.actions == {PLAY, PAUSE, STOP, NEXT, PREVIOUS}

        household.next(GARAGE)
        assert household.read_playback(GARAGE).actions == {PLAY, PAUSE, STOP, PREVIOUS}

        household.previous(GARAGE)
        assert household.read_playback(GARAGE).queue_position == 2

    def test_a_command_the_speaker_does_not_allow_fails(self, household):
        with pytest.raises(ExternalServiceException, match="UPnP Error 701"):
            household.previous(GARAGE)

        assert household.read_playback(GARAGE).queue_position == 1

    def test_a_command_goes_to_the_coordinator_of_the_group(self, household):
        household.join(LOUNGE, GARAGE)

        household.play(LOUNGE)
        household.next(LOUNGE)

        for speaker_id in (GARAGE, LOUNGE):
            playback = household.read_playback(speaker_id)
            assert playback.transport_state is TransportState.PLAYING
            assert playback.track == queue_track(2)

    def test_a_stream_allows_play_and_stop_only(self, household):
        household.start_stream(GARAGE, "Groove Salad", "An Artist - A Song")

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert playback.transport_uri.startswith("x-rincon-mp3radio://")
        assert playback.transport_title == "Groove Salad"
        assert playback.actions == {PLAY, STOP}
        assert (playback.queue_position, playback.queue_length) == (1, 1)
        assert playback.track.title == "groove-salad"
        assert playback.track.stream_content == "An Artist - A Song"
        assert (playback.track.artist, playback.track.album) == ("", "")
        assert playback.track.duration_seconds == 0
        assert playback.track.picture == ""

        with pytest.raises(ExternalServiceException, match="UPnP Error 701"):
            household.pause(GARAGE)
        with pytest.raises(ExternalServiceException, match="UPnP Error 701"):
            household.next(GARAGE)

        household.stop(GARAGE)
        assert household.read_playback(GARAGE).transport_state is TransportState.STOPPED

    def test_a_command_fails_when_the_household_does_not_answer(self, household):
        household.reachable = False

        for command in (
            household.play,
            household.pause,
            household.stop,
            household.next,
            household.previous,
            household.read_playback,
        ):
            with pytest.raises(ExternalServiceException):
                command(GARAGE)


class TestSonosClientVolume:
    def test_read_volume(self, household):
        assert household.read_volume(GARAGE) == Volume(volume=20, muted=False)

    def test_set_volume(self, household):
        household.set_volume(GARAGE, 7)

        assert household.read_volume(GARAGE).volume == 7

    def test_change_volume_answers_the_new_volume(self, household):
        assert household.change_volume(GARAGE, -5) == 15
        assert household.read_volume(GARAGE).volume == 15

    @pytest.mark.parametrize(("delta", "volume"), [(-100, 0), (100, 100)])
    def test_change_volume_stays_within_0_and_100(self, household, delta, volume):
        assert household.change_volume(GARAGE, delta) == volume

    def test_set_muted(self, household):
        household.set_muted(GARAGE, True)
        assert household.read_volume(GARAGE) == Volume(volume=20, muted=True)

        household.set_muted(GARAGE, False)
        assert household.read_volume(GARAGE).muted is False

    def test_the_group_volume_of_a_room_alone_is_the_rooms(self, household):
        assert household.read_group_volume(GARAGE) == Volume(volume=20, muted=False)

        household.set_group_volume(GARAGE, 12)

        assert household.read_volume(GARAGE).volume == 12
        assert household.read_group_volume(GARAGE).volume == 12

    def test_the_group_volume_moves_every_room_in_proportion(self, household):
        household.join(LOUNGE, GARAGE)
        assert household.read_group_volume(LOUNGE).volume == 30

        household.set_group_volume(LOUNGE, 15)

        assert household.read_volume(GARAGE).volume == 10
        assert household.read_volume(LOUNGE).volume == 20
        assert household.read_group_volume(GARAGE).volume == 15

    def test_change_group_volume_answers_the_new_group_volume(self, household):
        assert household.change_group_volume(GARAGE, 5) == 25
        assert household.change_group_volume(GARAGE, 100) == 100

    def test_set_group_muted_mutes_every_room(self, household):
        household.join(LOUNGE, GARAGE)

        household.set_group_muted(GARAGE, True)

        assert household.read_volume(LOUNGE).muted is True
        assert household.read_group_volume(LOUNGE).muted is True

        household.set_muted(LOUNGE, False)
        assert household.read_group_volume(GARAGE).muted is False

    def test_volume_fails_when_the_household_does_not_answer(self, household):
        household.reachable = False

        for call in (
            lambda: household.read_volume(GARAGE),
            lambda: household.read_group_volume(GARAGE),
            lambda: household.set_volume(GARAGE, 5),
            lambda: household.change_volume(GARAGE, 1),
            lambda: household.set_muted(GARAGE, True),
            lambda: household.set_group_volume(GARAGE, 5),
            lambda: household.change_group_volume(GARAGE, 1),
            lambda: household.set_group_muted(GARAGE, True),
        ):
            with pytest.raises(ExternalServiceException):
                call()


class TestSonosClientPicture:
    def test_read_picture_of_the_current_track(self, household):
        picture = household.read_playback(GARAGE).track.picture

        assert household.read_picture(GARAGE, picture) == household.picture_of(picture)

    def test_a_picture_is_served_after_its_track_stops_being_current(self, household):
        picture = household.read_playback(GARAGE).track.picture
        household.next(GARAGE)
        assert household.read_playback(GARAGE).track.picture != picture

        assert household.read_picture(GARAGE, picture) == household.picture_of(picture)

    @pytest.mark.parametrize("address", ["/getaa?s=1", "/nosuchpath", ""])
    def test_an_address_that_names_no_picture_fails(self, household, address):
        with pytest.raises(ExternalServiceException, match="404"):
            household.read_picture(GARAGE, address)

    def test_a_picture_fails_when_the_household_does_not_answer(self, household):
        picture = household.read_playback(GARAGE).track.picture
        household.reachable = False

        with pytest.raises(ExternalServiceException):
            household.read_picture(GARAGE, picture)


class TestSonosClientSubscriptions:
    def test_a_subscription_starts_with_the_state_in_full(self, household):
        listener = _Listener()
        for service in SonosService:
            household.subscribe(GARAGE, service, listener.on_event, listener.on_lost)
        household.settle()

        assert listener.events == [
            PlaybackChanged(
                GARAGE,
                transport_state=TransportState.STOPPED,
                transport_uri=f"x-rincon-queue:{GARAGE}#0",
                transport_title="",
                actions=frozenset({PLAY, PAUSE, STOP, NEXT}),
                shuffle=False,
                queue_position=1,
                queue_length=3,
                track=queue_track(1),
            ),
            VolumeChanged(GARAGE, volume=20, muted=False),
            GroupVolumeChanged(GARAGE, volume=20, muted=False),
            HouseholdChanged(GARAGE, household.read_household()),
        ]

    def test_events_are_delivered_off_the_callers_thread(self, household):
        import threading

        threads = []
        household.subscribe(
            GARAGE,
            SonosService.RENDERING_CONTROL,
            lambda event: threads.append(threading.current_thread()),
            lambda: None,
        )
        household.settle()

        assert threads and threading.current_thread() not in threads

    def test_play_arrives_as_a_burst_of_partial_events(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)

        household.play(GARAGE)
        household.settle()

        assert [event.transport_state for event in listener.events] == [
            TransportState.TRANSITIONING,
            TransportState.PLAYING,
            None,
        ]
        # Most events leave the actions and the transport's address out, and
        # one carries the actions alone (docs/api-design.md section 1.1).
        for event in listener.events[:2]:
            assert event.actions is None
            assert event.transport_uri is None
            assert event.transport_title is None
            assert event.track == queue_track(1)
        assert listener.events[2] == PlaybackChanged(
            GARAGE, actions=frozenset({PLAY, PAUSE, STOP, NEXT})
        )
        assert listener.events[2].track is UNREPORTED

    def test_a_track_change_names_the_new_track_and_then_the_actions(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)

        household.next(GARAGE)
        household.settle()

        assert [event.transport_state for event in listener.events] == [
            TransportState.STOPPED,
            None,
        ]
        assert listener.events[0].track == queue_track(2)
        assert listener.events[0].queue_position == 2
        assert listener.events[1].actions == {PLAY, PAUSE, STOP, NEXT, PREVIOUS}

    def test_a_new_transport_arrives_in_full(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)

        household.start_stream(GARAGE, "Groove Salad")
        household.report_stream_content(GARAGE, "An Artist - A Song")
        household.settle()

        started = listener.events[0]
        assert started.transport_uri.startswith("x-rincon-mp3radio://")
        assert started.transport_title == "Groove Salad"
        assert started.actions == {PLAY, STOP}
        assert started.track.stream_content == ""
        assert listener.events[1].track.stream_content == "An Artist - A Song"
        assert listener.events[1].transport_uri is None

    def test_shuffle_is_reported(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)

        household.set_shuffle(GARAGE, True)
        household.settle()

        assert listener.events[0].shuffle is True
        assert household.read_playback(GARAGE).shuffle is True

    def test_a_groups_transport_events_come_from_its_coordinator(self, household):
        household.join(LOUNGE, GARAGE)
        garage, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)
        lounge, _ = _subscribe(household, LOUNGE, SonosService.AV_TRANSPORT)

        household.play(LOUNGE)
        household.settle()

        assert {event.speaker_id for event in garage.events} == {GARAGE}
        assert lounge.events == []

    def test_a_volume_change_is_partial_and_moves_the_group_volume(self, household):
        room, _ = _subscribe(household, GARAGE, SonosService.RENDERING_CONTROL)
        group, _ = _subscribe(household, GARAGE, SonosService.GROUP_RENDERING_CONTROL)

        household.set_volume(GARAGE, 9)
        household.set_muted(GARAGE, True)
        household.settle()

        assert room.events == [
            VolumeChanged(GARAGE, volume=9),
            VolumeChanged(GARAGE, muted=True),
        ]
        assert group.events == [
            GroupVolumeChanged(GARAGE, volume=9, muted=False),
            GroupVolumeChanged(GARAGE, volume=9, muted=True),
        ]

    def test_a_group_volume_change_reaches_every_room(self, household):
        household.join(LOUNGE, GARAGE)
        garage, _ = _subscribe(household, GARAGE, SonosService.RENDERING_CONTROL)
        lounge, _ = _subscribe(household, LOUNGE, SonosService.RENDERING_CONTROL)
        group, _ = _subscribe(household, GARAGE, SonosService.GROUP_RENDERING_CONTROL)

        household.change_group_volume(LOUNGE, -15)
        household.settle()

        assert garage.events == [VolumeChanged(GARAGE, volume=10)]
        assert lounge.events == [VolumeChanged(LOUNGE, volume=20)]
        assert group.events == [GroupVolumeChanged(GARAGE, volume=15, muted=False)]

    def test_a_change_of_the_layout_reaches_every_topology_subscription(
        self, household
    ):
        garage, _ = _subscribe(household, GARAGE, SonosService.ZONE_GROUP_TOPOLOGY)
        lounge, _ = _subscribe(household, LOUNGE, SonosService.ZONE_GROUP_TOPOLOGY)

        household.rename_room(LOUNGE, "Living room")
        household.settle()

        layout = household.read_household()
        assert [room.name for room in layout.rooms] == ["Garage", "Living room"]
        assert garage.events == [HouseholdChanged(GARAGE, layout)]
        assert lounge.events == [HouseholdChanged(LOUNGE, layout)]

    def test_joining_leaving_and_removing_a_room_change_the_layout(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.ZONE_GROUP_TOPOLOGY)

        household.join(LOUNGE, GARAGE)
        household.unjoin(LOUNGE)
        household.remove_room(LOUNGE)
        household.settle()

        groups = [event.household.groups for event in listener.events]
        assert groups == [
            (Group(GARAGE, (GARAGE, LOUNGE)),),
            (Group(GARAGE, (GARAGE,)), Group(LOUNGE, (LOUNGE,))),
            (Group(GARAGE, (GARAGE,)),),
        ]

    def test_unsubscribe_ends_delivery(self, household):
        listener, subscription = _subscribe(
            household, GARAGE, SonosService.RENDERING_CONTROL
        )

        subscription.unsubscribe()
        household.set_volume(GARAGE, 9)
        household.settle()

        assert listener.events == []
        assert household.subscription_count(GARAGE) == 0

    def test_renew_keeps_a_subscription(self, household):
        listener, subscription = _subscribe(
            household, GARAGE, SonosService.RENDERING_CONTROL
        )

        subscription.renew()
        household.set_volume(GARAGE, 9)
        household.settle()

        assert listener.events == [VolumeChanged(GARAGE, volume=9)]

    def test_a_failed_renewal_loses_the_subscription(self, household):
        listener, subscription = _subscribe(
            household, GARAGE, SonosService.RENDERING_CONTROL
        )
        other, _ = _subscribe(household, LOUNGE, SonosService.RENDERING_CONTROL)

        household.fail_renewals(GARAGE)
        household.set_volume(GARAGE, 9)
        household.settle()

        assert (listener.lost, listener.events) == (1, [])
        assert other.lost == 0
        assert household.subscription_count(GARAGE) == 0
        with pytest.raises(ExternalServiceException, match="412"):
            subscription.renew()

    def test_a_restarted_room_forgets_its_subscriptions_silently(self, household):
        listener, subscription = _subscribe(
            household, GARAGE, SonosService.RENDERING_CONTROL
        )
        topology, _ = _subscribe(household, LOUNGE, SonosService.ZONE_GROUP_TOPOLOGY)

        household.restart_room(GARAGE)
        household.set_volume(GARAGE, 9)
        household.settle()

        assert (listener.lost, listener.events) == (0, [])
        boot_seqs = {
            room.id: room.boot_seq for room in topology.events[0].household.rooms
        }
        assert boot_seqs == {GARAGE: 2, LOUNGE: 1}
        with pytest.raises(ExternalServiceException, match="412"):
            subscription.renew()

    def test_a_room_that_moves_shows_its_new_address(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.ZONE_GROUP_TOPOLOGY)
        before = household.read_household().rooms[0].address

        address = household.move_room(GARAGE)
        household.settle()

        assert address != before
        assert listener.events[0].household.rooms[0].address == address
        assert household.subscription_count(GARAGE) == 1

    def test_subscribe_fails_when_the_household_does_not_answer(self, household):
        household.reachable = False

        with pytest.raises(ExternalServiceException):
            household.subscribe(
                GARAGE, SonosService.AV_TRANSPORT, lambda event: None, lambda: None
            )

    def test_a_handler_that_fails_fails_the_test_that_caused_it(self, household):
        def handler(event):
            raise RuntimeError("the handler failed")

        household.subscribe(
            GARAGE, SonosService.RENDERING_CONTROL, handler, lambda: None
        )

        with pytest.raises(RuntimeError, match="the handler failed"):
            household.settle()

    def test_a_subscription_can_start_with_a_group_volume_the_room_has_left(
        self, household
    ):
        household.lag_group_volume(GARAGE, 55)
        events = []
        household.subscribe(
            GARAGE, SonosService.GROUP_RENDERING_CONTROL, events.append, lambda: None
        )
        household.settle()

        assert events == [GroupVolumeChanged(GARAGE, volume=55, muted=False)]
        assert household.read_group_volume(GARAGE) == Volume(20, False)

        household.set_volume(GARAGE, 21)
        household.settle()
        later = []
        household.subscribe(
            GARAGE, SonosService.GROUP_RENDERING_CONTROL, later.append, lambda: None
        )
        household.settle()
        assert later == [GroupVolumeChanged(GARAGE, volume=21, muted=False)]

    def test_a_transport_that_waits_is_reported(self, household):
        events = []
        household.subscribe(
            GARAGE, SonosService.AV_TRANSPORT, events.append, lambda: None
        )
        household.settle()

        household.start_buffering(GARAGE)
        household.settle()

        assert events[1].transport_state is TransportState.TRANSITIONING
        assert household.read_playback(GARAGE).transport_state is (
            TransportState.TRANSITIONING
        )

    def test_a_source_that_is_not_radio_has_its_own_address(self, household):
        household.start_stream(GARAGE, "Line In", scheme="x-rincon-stream")

        assert household.read_playback(GARAGE).transport_uri.startswith(
            "x-rincon-stream:"
        )

    def test_a_room_that_is_silent_fails_every_call_to_it(self, household):
        subscription = household.subscribe(
            GARAGE, SonosService.RENDERING_CONTROL, lambda event: None, lambda: None
        )
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException, match="timed out"):
            household.read_volume(GARAGE)
        with pytest.raises(ExternalServiceException, match="timed out"):
            household.subscribe(
                GARAGE, SonosService.AV_TRANSPORT, lambda event: None, lambda: None
            )
        with pytest.raises(ExternalServiceException, match="timed out"):
            subscription.unsubscribe()
        assert household.subscription_count(GARAGE) == 0
        # The household still lists it, and the other rooms answer.
        assert len(household.read_household().rooms) == 2
        assert household.read_volume(LOUNGE) == Volume(40, False)

    def test_before_call_runs_between_two_calls_and_not_for_its_own(self, household):
        seen = []

        def turn_the_lounge_up(speaker_id):
            seen.append(speaker_id)
            household.set_volume(LOUNGE, 41)

        household.before_call = turn_the_lounge_up

        assert household.read_volume(LOUNGE) == Volume(41, False)
        assert seen == [LOUNGE]
