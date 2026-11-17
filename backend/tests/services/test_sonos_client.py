"""Tests for the Sonos client, on the in-memory household.

What ``SonosClient`` promises its callers, shown on ``FakeSonosClient``; the
live suite (``tests/live/``) shows the same of the SoCo client on the real
speakers.
"""

import io
from datetime import date

import pytest
from PIL import Image

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.exceptions import CommandRefusedException
from app.services.sonos_client import (
    ADD_MANY_LIMIT,
    QUEUE_ATTEMPTS,
    UNREPORTED,
    FetchRefusedException,
    Group,
    GroupVolumeChanged,
    HouseholdChanged,
    PlaybackChanged,
    PlexItem,
    PlexItemKind,
    QueueChanged,
    SoCoSonosClient,
    SonosService,
    TransportAction,
    TransportState,
    Volume,
    VolumeChanged,
)
from app.utils.plex_queue_entry import PlexQueueEntry, parse_plex_queue_entry
from tests.fakes.plex_client import MACHINE_IDENTIFIER, SECTION_ID, FakePlexClient
from tests.fakes.sonos_client import (
    CONTAINER_LIMIT,
    FakeSonosClient,
    picture_colour,
    queue_track,
)

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
LOUNGE_SURROUNDS = ("RINCON_FAKE0000000211400", "RINCON_FAKE0000000221400")
KITCHEN = "RINCON_FAKE0000000301400"
BEDROOM = "RINCON_FAKE0000000401400"

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


def _item(kind: PlexItemKind, rating_key: str) -> PlexItem:
    """An item of the fake Plex's library, as the Plex service queues it."""
    return PlexItem(
        kind, MACHINE_IDENTIFIER, SECTION_ID, rating_key, f"Item {rating_key}"
    )


def _album(rating_key: str) -> PlexItem:
    return _item(PlexItemKind.ALBUM, rating_key)


def _keys(sonos: FakeSonosClient, speaker_id: str) -> list[str]:
    """The rating keys of the group's queue, as the speaker presents it."""
    return [
        parse_plex_queue_entry(track.uri).rating_key
        for track in sonos.read_queue(speaker_id, 0, 1000).tracks
    ]


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

    @pytest.mark.parametrize("seeds", [2, 5])
    def test_the_soco_client_reports_seeds_that_do_not_answer(
        self, app, client, seeds
    ):
        # It registers its check in place of the fake's.
        SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=[f"192.0.2.{n}" for n in range(1, seeds + 1)],
                sonos_event_advertise_host="sonos-events.invalid",
                plex_url="http://192.0.2.1:32400",
                plex_token="the-suites-plex-token",
            ),
            health_service=app.container.health_service(),
        )

        response = client.get("/health/readyz")

        # Every seed was asked inside readyz's wait for the check: a check
        # that outlives it reads "timed out after 0.8 s".
        assert response.get_json()["checks"]["sonos"] == {
            "status": "error",
            "critical": False,
            "detail": f"none of the {seeds} seed speakers answered",
        }


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

    def test_the_soco_client_fails_in_its_own_words_without_an_address(self, app):
        client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.4"],
                sonos_event_advertise_host="sonos-events.invalid",
                plex_url="http://192.0.2.1:32400",
                plex_token="the-suites-plex-token",
            ),
            health_service=app.container.health_service(),
        )

        with pytest.raises(ExternalServiceException) as raised:
            client.play(GARAGE)

        # SoCo's and requests' own messages name the address that was asked.
        assert raised.value.message.startswith(
            f"Cannot play on speaker {GARAGE} because external service failed: "
            "none of the 1 seed speakers answered (the speaker did not answer ("
        )
        assert "192.0.2" not in raised.value.message

    def test_the_soco_client_fails_a_join_and_a_leave_in_its_own_words(self, app):
        client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.5"],
                sonos_event_advertise_host="sonos-events.invalid",
                plex_url="http://192.0.2.1:32400",
                plex_token="the-suites-plex-token",
            ),
            health_service=app.container.health_service(),
        )

        with pytest.raises(ExternalServiceException) as joining:
            client.join(LOUNGE, GARAGE)
        with pytest.raises(ExternalServiceException) as leaving:
            client.unjoin(LOUNGE)

        assert joining.value.operation == (
            f"join the group of speaker {GARAGE} with speaker {LOUNGE}"
        )
        assert leaving.value.operation == f"ungroup speaker {LOUNGE}"
        for raised in (joining, leaving):
            assert raised.value.cause.startswith(
                "none of the 1 seed speakers answered (the speaker did not answer ("
            )
            assert "192.0.2" not in raised.value.message

    def test_the_soco_client_asks_a_speaker_while_soco_holds_a_topology_subscription(
        self, app
    ):
        client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.3"],
                sonos_event_advertise_host="sonos-events.invalid",
                plex_url="http://192.0.2.1:32400",
                plex_token="the-suites-plex-token",
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

    def test_a_command_the_speaker_does_not_allow_is_refused(self, household):
        with pytest.raises(CommandRefusedException) as raised:
            household.previous(GARAGE)

        assert raised.value.operation == (
            f"go back to the previous track on speaker {GARAGE}"
        )
        assert raised.value.cause == (
            "the speaker answered UPnP error 711 (Illegal seek target)"
        )
        assert household.read_playback(GARAGE).queue_position == 1

    @pytest.mark.parametrize("command", ["play", "pause", "stop", "next", "previous"])
    def test_a_command_to_a_speaker_that_does_not_answer_is_no_refusal(
        self, household, command
    ):
        household.silence_room(GARAGE)
        try:
            with pytest.raises(ExternalServiceException) as raised:
                getattr(household, command)(GARAGE)
        finally:
            household.answer_again(GARAGE)

        assert raised.value.cause == "the speaker did not answer (ConnectTimeout)"

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

        refused = "the speaker answered UPnP error 701 (Transition not available)"
        with pytest.raises(CommandRefusedException) as raised:
            household.pause(GARAGE)
        assert raised.value.cause == refused
        with pytest.raises(CommandRefusedException) as raised:
            household.next(GARAGE)
        assert raised.value.cause == refused

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


class TestSonosClientGroups:
    @pytest.fixture
    def four_rooms(self, household):
        """The household with a Kitchen and a Bedroom, each of one device."""
        household.add_room(KITCHEN, "Kitchen", model="One", volume=60)
        household.add_room(BEDROOM, "Bedroom", model="One", volume=10)
        return household

    def test_join_puts_the_room_in_the_group(self, household):
        household.join(LOUNGE, GARAGE)

        assert household.read_household().groups == (
            Group(coordinator_id=GARAGE, member_ids=(GARAGE, LOUNGE)),
        )
        assert household.read_playback(LOUNGE).track == queue_track(1)
        assert household.read_group_volume(LOUNGE) == Volume(30, False)

    def test_join_names_any_room_of_the_group(self, four_rooms):
        four_rooms.join(LOUNGE, GARAGE)

        four_rooms.join(KITCHEN, LOUNGE)

        assert four_rooms.read_household().groups == (
            Group(GARAGE, (GARAGE, LOUNGE, KITCHEN)),
            Group(BEDROOM, (BEDROOM,)),
        )

    def test_a_room_that_joins_reports_its_own_transport_and_the_group_its_volume(
        self, household
    ):
        lounge, _ = _subscribe(household, LOUNGE, SonosService.AV_TRANSPORT)
        garage, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)
        group, _ = _subscribe(household, GARAGE, SonosService.GROUP_RENDERING_CONTROL)

        household.join(LOUNGE, GARAGE)
        household.settle()

        assert lounge.events == [
            PlaybackChanged(LOUNGE, transport_uri=f"x-rincon:{GARAGE}")
        ]
        assert garage.events == []
        assert group.events == [GroupVolumeChanged(GARAGE, volume=30, muted=False)]

    def test_a_join_to_the_group_the_room_plays_in_changes_nothing(self, household):
        household.join(LOUNGE, GARAGE)
        layout = household.read_household()
        listeners = [
            _subscribe(household, speaker_id, service)[0]
            for speaker_id in (GARAGE, LOUNGE)
            for service in SonosService
        ]

        household.join(LOUNGE, GARAGE)
        household.join(GARAGE, LOUNGE)
        household.join(GARAGE, GARAGE)
        household.settle()

        assert household.read_household() == layout
        assert [listener.events for listener in listeners] == [[]] * 10

    def test_a_room_that_moves_to_another_group_leaves_its_own(self, four_rooms):
        four_rooms.join(LOUNGE, GARAGE)
        left, _ = _subscribe(four_rooms, GARAGE, SonosService.GROUP_RENDERING_CONTROL)
        joined, _ = _subscribe(
            four_rooms, KITCHEN, SonosService.GROUP_RENDERING_CONTROL
        )

        four_rooms.join(LOUNGE, KITCHEN)
        four_rooms.settle()

        assert four_rooms.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(KITCHEN, (LOUNGE, KITCHEN)),
            Group(BEDROOM, (BEDROOM,)),
        )
        assert left.events == [GroupVolumeChanged(GARAGE, volume=20, muted=False)]
        assert joined.events == [GroupVolumeChanged(KITCHEN, volume=50, muted=False)]

    def test_unjoin_makes_the_room_play_alone_what_it_holds_itself(self, household):
        household.join(LOUNGE, GARAGE)
        lounge, _ = _subscribe(household, LOUNGE, SonosService.AV_TRANSPORT)
        garage, _ = _subscribe(household, GARAGE, SonosService.AV_TRANSPORT)
        left, _ = _subscribe(household, GARAGE, SonosService.GROUP_RENDERING_CONTROL)
        alone, _ = _subscribe(household, LOUNGE, SonosService.GROUP_RENDERING_CONTROL)

        household.unjoin(LOUNGE)
        household.settle()

        assert household.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(LOUNGE, (LOUNGE,)),
        )
        assert household.read_playback(LOUNGE).queue_length == 0
        assert household.read_playback(GARAGE).track == queue_track(1)
        [event] = lounge.events
        assert event.transport_uri == f"x-rincon-queue:{LOUNGE}#0"
        assert event.track is None
        assert garage.events == []
        assert left.events == [GroupVolumeChanged(GARAGE, volume=20, muted=False)]
        assert alone.events == [GroupVolumeChanged(LOUNGE, volume=40, muted=False)]

    def test_an_unjoin_by_a_room_alone_changes_nothing(self, household):
        household.play(GARAGE)
        layout = household.read_household()
        playback = household.read_playback(GARAGE)
        listeners = [
            _subscribe(household, GARAGE, service)[0] for service in SonosService
        ]

        household.unjoin(GARAGE)
        household.settle()

        assert household.read_household() == layout
        assert household.read_playback(GARAGE) == playback
        assert [listener.events for listener in listeners] == [[]] * 5

    def test_a_group_whose_coordinator_leaves_stays_a_group_led_by_one_of_its_rooms(
        self, four_rooms
    ):
        four_rooms.load_queue(LOUNGE, [queue_track(7)])
        four_rooms.join(LOUNGE, GARAGE)
        four_rooms.join(KITCHEN, GARAGE)
        lounge, _ = _subscribe(four_rooms, LOUNGE, SonosService.AV_TRANSPORT)
        kitchen, _ = _subscribe(four_rooms, KITCHEN, SonosService.AV_TRANSPORT)
        garage, _ = _subscribe(four_rooms, GARAGE, SonosService.AV_TRANSPORT)
        left, _ = _subscribe(four_rooms, LOUNGE, SonosService.GROUP_RENDERING_CONTROL)
        alone, _ = _subscribe(four_rooms, GARAGE, SonosService.GROUP_RENDERING_CONTROL)

        four_rooms.unjoin(GARAGE)
        four_rooms.settle()

        assert four_rooms.read_household().groups == (
            Group(GARAGE, (GARAGE,)),
            Group(LOUNGE, (LOUNGE, KITCHEN)),
            Group(BEDROOM, (BEDROOM,)),
        )
        # The fake's choice: every room keeps its own transport.
        assert four_rooms.read_playback(GARAGE).track == queue_track(1)
        assert four_rooms.read_playback(LOUNGE).track == queue_track(7)
        assert four_rooms.read_playback(KITCHEN).track == queue_track(7)
        [leading] = lounge.events
        assert leading.transport_uri == f"x-rincon-queue:{LOUNGE}#0"
        assert leading.track == queue_track(7)
        assert kitchen.events == [
            PlaybackChanged(KITCHEN, transport_uri=f"x-rincon:{LOUNGE}")
        ]
        [own] = garage.events
        assert own.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert left.events == [GroupVolumeChanged(LOUNGE, volume=50, muted=False)]
        assert alone.events == [GroupVolumeChanged(GARAGE, volume=20, muted=False)]

    def test_a_group_whose_coordinator_joins_another_stays_a_group(self, four_rooms):
        four_rooms.load_queue(LOUNGE, [queue_track(7)])
        four_rooms.load_queue(BEDROOM, [queue_track(9)])
        four_rooms.join(LOUNGE, GARAGE)
        four_rooms.join(KITCHEN, GARAGE)
        lounge, _ = _subscribe(four_rooms, LOUNGE, SonosService.AV_TRANSPORT)
        kitchen, _ = _subscribe(four_rooms, KITCHEN, SonosService.AV_TRANSPORT)
        garage, _ = _subscribe(four_rooms, GARAGE, SonosService.AV_TRANSPORT)
        left, _ = _subscribe(four_rooms, LOUNGE, SonosService.GROUP_RENDERING_CONTROL)
        joined, _ = _subscribe(
            four_rooms, BEDROOM, SonosService.GROUP_RENDERING_CONTROL
        )

        four_rooms.join(GARAGE, BEDROOM)
        four_rooms.settle()

        layout = four_rooms.read_household()
        assert layout.groups == (
            Group(LOUNGE, (LOUNGE, KITCHEN)),
            Group(BEDROOM, (GARAGE, BEDROOM)),
        )
        assert sorted(
            member_id for group in layout.groups for member_id in group.member_ids
        ) == [room.id for room in layout.rooms]
        assert four_rooms.read_playback(GARAGE).track == queue_track(9)
        assert four_rooms.read_playback(LOUNGE).track == queue_track(7)
        assert four_rooms.read_playback(KITCHEN).track == queue_track(7)
        [leading] = lounge.events
        assert leading.transport_uri == f"x-rincon-queue:{LOUNGE}#0"
        assert kitchen.events == [
            PlaybackChanged(KITCHEN, transport_uri=f"x-rincon:{LOUNGE}")
        ]
        assert garage.events == [
            PlaybackChanged(GARAGE, transport_uri=f"x-rincon:{BEDROOM}")
        ]
        assert left.events == [GroupVolumeChanged(LOUNGE, volume=50, muted=False)]
        assert joined.events == [GroupVolumeChanged(BEDROOM, volume=15, muted=False)]

    def test_a_join_and_a_leave_reach_the_room_that_moves_alone(self, household):
        reached = []
        household.before_call = reached.append
        household.silence_room(GARAGE)

        household.join(LOUNGE, GARAGE)
        household.unjoin(LOUNGE)

        assert reached == [LOUNGE, LOUNGE]

    @pytest.mark.parametrize(
        ("command", "operation"),
        [
            (
                lambda household: household.join(LOUNGE, GARAGE),
                f"join the group of speaker {GARAGE} with speaker {LOUNGE}",
            ),
            (lambda household: household.unjoin(LOUNGE), f"ungroup speaker {LOUNGE}"),
        ],
        ids=["join", "unjoin"],
    )
    def test_a_join_and_a_leave_fail_as_the_rooms_other_commands_do(
        self, household, command, operation
    ):
        household.reachable = False
        with pytest.raises(ExternalServiceException) as unreachable:
            command(household)
        household.reachable = True
        household.silence_room(LOUNGE)
        with pytest.raises(ExternalServiceException) as silent:
            command(household)
        household.answer_again(LOUNGE)
        household.remove_room(LOUNGE)
        with pytest.raises(ExternalServiceException) as gone:
            command(household)

        assert unreachable.value.operation == operation
        assert unreachable.value.cause == "none of the seed speakers answered"
        assert silent.value.operation == operation
        assert silent.value.cause == "the speaker did not answer (ConnectTimeout)"
        assert gone.value.operation == operation
        assert gone.value.cause == f"speaker {LOUNGE} is not in the household"
        assert household.read_household().groups == (Group(GARAGE, (GARAGE,)),)

    @pytest.mark.parametrize(
        "member_id", ["RINCON_FAKE0000000991400", LOUNGE_SURROUNDS[0]]
    )
    def test_a_join_to_the_group_of_no_room_fails(self, household, member_id):
        layout = household.read_household()

        with pytest.raises(ExternalServiceException) as raised:
            household.join(GARAGE, member_id)

        assert raised.value.operation == (
            f"join the group of speaker {member_id} with speaker {GARAGE}"
        )
        assert raised.value.cause == f"speaker {member_id} is not in the household"
        assert household.read_household() == layout


class TestSonosClientQueue:
    @pytest.fixture
    def library(self, plex: FakePlexClient) -> FakePlexClient:
        """An artist of two albums, a second artist's album and a playlist.

        Album 10 (2001) holds 101 to 103, and album 20 (2010) 201 and 202,
        both of artist 1; album 30, of artist 3, holds 301 to 310. Playlist 40
        holds 202, 101 and 301.
        """
        plex.add_artist("1", "Artist One")
        plex.add_album(
            "10", ["101", "102", "103"], artist="1", released=date(2001, 5, 1)
        )
        plex.add_album("20", [], artist="1", title="Later", released=date(2010, 1, 1))
        # On two discs, added out of order.
        plex.add_track("20", "202", disc=2, number=1)
        plex.add_track("20", "201", disc=1, number=1)
        plex.add_artist("3", "Artist Three")
        plex.add_album("30", [str(key) for key in range(301, 311)], artist="3")
        plex.add_playlist("40", "A Mix", 3, track_keys=["202", "101", "301"])
        return plex

    def test_read_queue_answers_a_page_and_the_queues_size(self, household):
        page = household.read_queue(GARAGE, 1, 1)

        assert page.tracks == (queue_track(2),)
        assert page.total == 3
        assert household.read_queue(GARAGE, 0, 100).tracks == (
            queue_track(1),
            queue_track(2),
            queue_track(3),
        )
        assert household.read_queue(GARAGE, 3, 100).tracks == ()
        assert household.read_queue(LOUNGE, 0, 100).total == 0

    def test_an_album_becomes_its_tracks_in_disc_and_track_order(
        self, household, library
    ):
        household.add_to_queue(LOUNGE, _album("20"))

        [first, second] = household.read_queue(LOUNGE, 0, 100).tracks
        assert parse_plex_queue_entry(first.uri) == PlexQueueEntry(
            MACHINE_IDENTIFIER, SECTION_ID, "201"
        )
        assert parse_plex_queue_entry(second.uri).rating_key == "202"
        # Every entry is complete, as the speaker's are.
        assert (first.title, first.artist, first.album) == (
            "Track 201",
            "Artist One",
            "Later",
        )
        assert first.duration_seconds == 180
        assert first.picture == f"/getaa?s=1&u={first.uri}"

    def test_an_artist_becomes_their_albums_newest_first(self, household, library):
        household.add_to_queue(LOUNGE, _item(PlexItemKind.ARTIST, "1"))

        assert _keys(household, LOUNGE) == ["201", "202", "101", "102", "103"]

    def test_a_playlist_becomes_its_tracks_in_its_own_order(self, household, library):
        household.add_to_queue(LOUNGE, _item(PlexItemKind.PLAYLIST, "40"))

        assert _keys(household, LOUNGE) == ["202", "101", "301"]
        assert [track.album for track in household.read_queue(LOUNGE, 0, 3).tracks] == [
            "Later",
            "Album 10",
            "Album 30",
        ]

    def test_a_track_becomes_itself(self, household, library):
        household.add_to_queue(LOUNGE, _item(PlexItemKind.TRACK, "102"))

        assert _keys(household, LOUNGE) == ["102"]

    def test_add_appends_and_changes_nothing_else(self, household, library):
        household.next(GARAGE)
        household.play(GARAGE)
        before = household.read_playback(GARAGE)

        household.add_to_queue(GARAGE, _album("10"))

        after = household.read_playback(GARAGE)
        assert household.read_queue(GARAGE, 0, 100).tracks[:3] == (
            queue_track(1),
            queue_track(2),
            queue_track(3),
        )
        assert _keys(household, GARAGE)[3:] == ["101", "102", "103"]
        assert after.queue_length == 6
        assert (after.transport_state, after.queue_position, after.track) == (
            before.transport_state,
            before.queue_position,
            before.track,
        )

    def test_adding_to_an_empty_queue_makes_its_first_track_current(
        self, household, library
    ):
        household.add_to_queue(LOUNGE, _album("10"))

        playback = household.read_playback(LOUNGE)
        assert playback.transport_state is TransportState.STOPPED
        assert (playback.queue_position, playback.queue_length) == (1, 3)
        assert parse_plex_queue_entry(playback.track.uri).rating_key == "101"

    def test_add_many_queues_each_container_in_the_order_given(
        self, household, library
    ):
        household.add_many_to_queue(LOUNGE, [_album("30"), _album("10"), _album("20")])

        assert _keys(household, LOUNGE) == [
            *(str(key) for key in range(301, 311)),
            "101",
            "102",
            "103",
            "201",
            "202",
        ]

    @pytest.mark.parametrize("count", [0, ADD_MANY_LIMIT + 1])
    def test_add_many_takes_1_to_16_items(self, household, library, count):
        with pytest.raises(ValueError, match=f"takes 1 to 16 items, not {count}"):
            household.add_many_to_queue(LOUNGE, [_album("10")] * count)

        assert household.read_queue(LOUNGE, 0, 100).total == 0

    @pytest.mark.parametrize(
        "item",
        [
            _album("99"),
            # An artist's key named as an album.
            _album("1"),
            PlexItem(PlexItemKind.ALBUM, "anotherserver", SECTION_ID, "10", "Album 10"),
        ],
    )
    def test_a_container_the_service_does_not_have_is_refused_and_queues_nothing(
        self, household, library, item
    ):
        with pytest.raises(ExternalServiceException) as raised:
            household.add_to_queue(GARAGE, item)
        with pytest.raises(ExternalServiceException):
            household.add_many_to_queue(GARAGE, [_album("10"), item])

        assert raised.value.operation == (
            f"add Plex album {item.rating_key} to the queue of speaker {GARAGE}"
        )
        assert raised.value.cause == "the speaker answered UPnP error 800"
        assert household.read_queue(GARAGE, 0, 100).total == 3

    def test_a_call_beyond_what_the_speaker_fetches_is_refused_and_queues_nothing(
        self, household, plex
    ):
        half = CONTAINER_LIMIT // 2 + 1
        plex.add_album("50", [str(100_000 + n) for n in range(half)])
        plex.add_album("60", [str(200_000 + n) for n in range(half)])
        plex.add_album("70", [str(300_000 + n) for n in range(CONTAINER_LIMIT + 1)])

        with pytest.raises(ExternalServiceException) as raised:
            household.add_to_queue(LOUNGE, _album("70"))
        with pytest.raises(ExternalServiceException) as many:
            household.add_many_to_queue(LOUNGE, [_album("50"), _album("60")])

        assert raised.value.cause == "the speaker answered UPnP error 800"
        assert (
            many.value.operation == f"add 2 Plex items to the queue of speaker {LOUNGE}"
        )
        assert many.value.cause == "the speaker answered UPnP error 800"
        assert household.read_queue(LOUNGE, 0, 100).total == 0
        household.add_to_queue(LOUNGE, _album("50"))
        assert household.read_queue(LOUNGE, 0, 100).total == half

    def test_a_call_refused_for_a_fetch_that_failed_is_made_again(
        self, household, library
    ):
        reached: list[str] = []
        household.before_call = reached.append
        household.fail_fetches(1)

        household.add_many_to_queue(LOUNGE, [_album("30"), _album("10")])

        assert reached == [LOUNGE, LOUNGE]
        # Queued once: a refused call queues nothing.
        assert _keys(household, LOUNGE) == [
            *(str(key) for key in range(301, 311)),
            "101",
            "102",
            "103",
        ]

    def test_a_refusal_that_lasts_fails_in_the_clients_words_and_queues_nothing(
        self, household, library
    ):
        reached: list[str] = []
        household.before_call = reached.append
        household.fail_fetches(QUEUE_ATTEMPTS)

        with pytest.raises(FetchRefusedException) as raised:
            household.add_to_queue(GARAGE, _album("10"))

        assert raised.value.message == (
            f"Cannot add Plex album 10 to the queue of speaker {GARAGE} because "
            "external service failed: the speaker answered UPnP error 800"
        )
        assert reached == [GARAGE] * QUEUE_ATTEMPTS
        assert household.read_queue(GARAGE, 0, 100).total == 3

    def test_a_queueing_call_that_fails_otherwise_is_made_once(
        self, household, library
    ):
        reached: list[str] = []
        household.before_call = reached.append
        household.plex_linked = False

        with pytest.raises(ExternalServiceException) as raised:
            household.add_many_to_queue(GARAGE, [_album("10")])

        assert not isinstance(raised.value, FetchRefusedException)
        assert reached == [GARAGE]

    def test_a_household_without_the_plex_service_queues_nothing(
        self, household, library
    ):
        household.plex_linked = False

        with pytest.raises(ExternalServiceException) as raised:
            household.add_to_queue(GARAGE, _album("10"))

        assert raised.value.cause == "the speaker lists no Plex service"
        assert household.read_queue(GARAGE, 0, 100).total == 3

    def test_clear_empties_the_queue_and_stops(self, household):
        household.play(GARAGE)

        household.clear_queue(GARAGE)

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.STOPPED
        assert playback.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert (playback.queue_position, playback.queue_length) == (0, 0)
        assert playback.track is None
        assert playback.actions == frozenset()
        assert household.read_queue(GARAGE, 0, 100).total == 0

    def test_clear_leaves_a_stream_playing(self, household):
        household.start_stream(GARAGE, "Radio One")

        household.clear_queue(GARAGE)

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert playback.transport_title == "Radio One"
        assert household.read_queue(GARAGE, 0, 100).total == 0

    def test_shuffle_presents_the_queue_in_play_order_from_the_current_track(
        self, household
    ):
        tracks = [queue_track(n) for n in range(1, 11)]
        household.load_queue(GARAGE, tracks)
        household.next(GARAGE)
        household.next(GARAGE)

        household.set_shuffle(GARAGE, True)

        shuffled = household.read_queue(GARAGE, 0, 100).tracks
        playback = household.read_playback(GARAGE)
        assert playback.shuffle is True
        assert (playback.queue_position, playback.track) == (1, queue_track(3))
        assert shuffled[0] == queue_track(3)
        assert sorted(shuffled, key=tracks.index) == tracks
        assert list(shuffled) != [queue_track(3), *tracks[:2], *tracks[3:]]

        # Next counts along the play order.
        household.next(GARAGE)
        assert household.read_playback(GARAGE).track == shuffled[1]

        household.set_shuffle(GARAGE, False)

        playback = household.read_playback(GARAGE)
        assert household.read_queue(GARAGE, 0, 100).tracks == tuple(tracks)
        assert playback.shuffle is False
        assert playback.track == shuffled[1]
        assert playback.queue_position == tracks.index(shuffled[1]) + 1

    def test_what_is_queued_with_shuffle_on_is_drawn_into_the_play_order(
        self, household, library
    ):
        household.set_shuffle(LOUNGE, True)
        household.add_to_queue(LOUNGE, _album("30"))

        drawn = _keys(household, LOUNGE)
        assert sorted(drawn) == [str(key) for key in range(301, 311)]
        assert drawn != sorted(drawn)
        playback = household.read_playback(LOUNGE)
        assert parse_plex_queue_entry(playback.track.uri).rating_key == drawn[0]

        # Added later, it lands after the current track, mixed into the rest.
        household.add_to_queue(LOUNGE, _album("10"))

        mixed = _keys(household, LOUNGE)
        assert mixed[0] == drawn[0]
        assert [key for key in mixed if key.startswith("3")] == drawn
        assert sorted(mixed[1:]) == sorted([*drawn[1:], "101", "102", "103"])
        household.set_shuffle(LOUNGE, False)
        assert _keys(household, LOUNGE) == [
            *(str(key) for key in range(301, 311)),
            "101",
            "102",
            "103",
        ]

    def test_shuffle_of_an_empty_queue_sets_the_mode(self, household):
        household.set_shuffle(LOUNGE, True)

        assert household.read_playback(LOUNGE).shuffle is True
        assert household.read_queue(LOUNGE, 0, 100).total == 0

    def test_play_from_the_first_position_plays_the_first_track(self, household):
        household.next(GARAGE)
        household.next(GARAGE)

        household.play_from(GARAGE, 1)

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert (playback.queue_position, playback.track) == (1, queue_track(1))

    def test_play_from_a_position_plays_the_track_there(self, household):
        household.play_from(GARAGE, 2)

        playback = household.read_playback(GARAGE)
        assert playback.transport_state is TransportState.PLAYING
        assert (playback.queue_position, playback.track) == (2, queue_track(2))

    def test_play_from_takes_the_queue_from_a_stream(self, household):
        household.start_stream(GARAGE, "Radio One")

        household.play_from(GARAGE, 1)

        playback = household.read_playback(GARAGE)
        assert playback.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert playback.transport_state is TransportState.PLAYING
        assert playback.track == queue_track(1)

    def test_select_queue_takes_the_queue_from_a_stream_and_stops(self, household):
        household.start_stream(GARAGE, "Radio One")

        household.select_queue(GARAGE)

        playback = household.read_playback(GARAGE)
        assert playback.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert playback.transport_state is TransportState.STOPPED
        assert (playback.queue_position, playback.track) == (1, queue_track(1))
        assert household.read_queue(GARAGE, 0, 100).total == 3

    def test_select_queue_of_an_emptied_queue_takes_it_from_a_stream(self, household):
        household.start_stream(GARAGE, "Radio One")
        household.clear_queue(GARAGE)

        household.select_queue(GARAGE)

        playback = household.read_playback(GARAGE)
        assert playback.transport_uri == f"x-rincon-queue:{GARAGE}#0"
        assert playback.transport_state is TransportState.STOPPED
        assert (playback.queue_position, playback.queue_length) == (0, 0)

    def test_select_queue_leaves_the_queue_that_is_the_source_as_it_is(
        self, household
    ):
        household.next(GARAGE)
        household.play(GARAGE)
        found = household.read_playback(GARAGE)

        household.select_queue(GARAGE)

        assert household.read_playback(GARAGE) == found

    def test_shuffle_is_refused_while_a_stream_is_the_source(self, household):
        household.start_stream(GARAGE, "Radio One")

        with pytest.raises(ExternalServiceException) as raised:
            household.set_shuffle(GARAGE, True)

        # As the Garage answered with a radio stream as its source.
        assert raised.value.operation == f"set the play mode of speaker {GARAGE}"
        assert raised.value.cause == (
            "the speaker answered UPnP error 712 (Play mode not supported)"
        )
        assert household.read_playback(GARAGE).shuffle is False
        # Normal, which it already is, is taken.
        household.set_shuffle(GARAGE, False)
        household.select_queue(GARAGE)
        household.set_shuffle(GARAGE, True)
        assert household.read_playback(GARAGE).shuffle is True

    def test_play_from_the_start_of_an_empty_queue_fails(self, household):
        with pytest.raises(ExternalServiceException) as raised:
            household.play_from(LOUNGE, 1)

        # As the Garage answered: the seek to the first track fails.
        assert (
            raised.value.operation == f"play from queue position 1 on speaker {LOUNGE}"
        )
        assert raised.value.cause == (
            "the speaker answered UPnP error 711 (Illegal seek target)"
        )
        assert household.read_playback(LOUNGE).transport_state is (
            TransportState.STOPPED
        )

    def test_play_from_past_the_end_of_the_queue_fails(self, household):
        found = household.read_playback(GARAGE)

        with pytest.raises(ExternalServiceException) as raised:
            household.play_from(GARAGE, 4)

        # As the live suite's test_an_album_plays_from_one_of_its_tracks has
        # the Garage answer a seek past its last track.
        assert raised.value.cause == (
            "the speaker answered UPnP error 711 (Illegal seek target)"
        )
        assert household.read_playback(GARAGE) == found

    def test_a_queue_call_goes_to_the_coordinator_of_the_group(
        self, household, library
    ):
        household.join(LOUNGE, GARAGE)

        household.add_to_queue(LOUNGE, _album("10"))
        household.set_shuffle(LOUNGE, True)

        assert household.read_queue(LOUNGE, 0, 100) == household.read_queue(
            GARAGE, 0, 100
        )
        assert household.read_queue(GARAGE, 0, 100).total == 6
        assert household.read_playback(GARAGE).shuffle is True
        household.clear_queue(LOUNGE)
        assert household.read_queue(GARAGE, 0, 100).total == 0

    @pytest.mark.parametrize(
        ("call", "operation"),
        [
            (lambda s, room: s.read_queue(room, 0, 10), "read the queue of"),
            (lambda s, room: s.clear_queue(room), "clear the queue of"),
            (lambda s, room: s.set_shuffle(room, True), "set the play mode of"),
            (
                lambda s, room: s.add_to_queue(room, _album("10")),
                "add Plex album 10 to the queue of",
            ),
            (
                lambda s, room: s.add_many_to_queue(room, [_album("10")]),
                "add 1 Plex items to the queue of",
            ),
            (lambda s, room: s.play_from(room, 1), "play from queue position 1 on"),
        ],
    )
    def test_a_queue_call_fails_when_the_coordinator_does_not_answer(
        self, household, library, call, operation
    ):
        household.join(LOUNGE, GARAGE)
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException) as raised:
            call(household, LOUNGE)

        assert raised.value.operation == f"{operation} speaker {LOUNGE}"
        assert raised.value.cause == "the speaker did not answer (ConnectTimeout)"

        household.answer_again(GARAGE)
        household.reachable = False
        with pytest.raises(ExternalServiceException, match="none of the seed"):
            call(household, LOUNGE)

    def test_a_queue_change_arrives_as_a_playback_event(self, household, library):
        listener, _ = _subscribe(household, LOUNGE, SonosService.AV_TRANSPORT)

        household.add_to_queue(LOUNGE, _album("10"))
        household.play_from(LOUNGE, 1)
        household.clear_queue(LOUNGE)
        household.settle()

        events = listener.events
        lengths = [e.queue_length for e in events if e.queue_length is not None]
        states = [e.transport_state for e in events if e.transport_state is not None]
        tracks = [e.track for e in events if e.track is not UNREPORTED]
        assert lengths[0] == 3
        assert TransportState.PLAYING in states
        assert (lengths[-1], states[-1], tracks[-1]) == (
            0,
            TransportState.STOPPED,
            None,
        )

    def test_a_queue_change_arrives_as_a_queue_event(self, household, library):
        listener, _ = _subscribe(household, GARAGE, SonosService.QUEUE)

        household.add_to_queue(GARAGE, _album("10"))
        household.add_many_to_queue(GARAGE, [_album("30"), _album("10")])
        household.set_shuffle(GARAGE, True)
        household.set_shuffle(GARAGE, False)
        household.clear_queue(GARAGE)
        household.load_queue(GARAGE, [queue_track(1)])
        household.settle()

        # One for every change, a change of play mode included
        # (docs/api-design.md section 1.1).
        assert listener.events == [QueueChanged(GARAGE)] * 6

    def test_what_leaves_the_queue_as_it_is_sends_no_queue_event(self, household):
        listener, _ = _subscribe(household, GARAGE, SonosService.QUEUE)

        household.play(GARAGE)
        household.next(GARAGE)
        household.previous(GARAGE)
        household.stop(GARAGE)
        household.set_shuffle(GARAGE, False)
        household.set_volume(GARAGE, 30)
        household.start_stream(GARAGE, "Groove Salad")
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert listener.events == []

    def test_a_groups_queue_events_come_from_its_coordinator(self, household):
        household.join(LOUNGE, GARAGE)
        garage, _ = _subscribe(household, GARAGE, SonosService.QUEUE)
        lounge, _ = _subscribe(household, LOUNGE, SonosService.QUEUE)

        household.clear_queue(LOUNGE)
        household.settle()

        assert garage.events == [QueueChanged(GARAGE)]
        assert lounge.events == []

    def test_the_soco_client_fails_queue_calls_in_its_own_words(self, app):
        client = SoCoSonosClient(
            settings=AppSettings(
                sonos_seed_hosts=["192.0.2.6"],
                sonos_event_advertise_host="sonos-events.invalid",
                plex_url="http://192.0.2.1:32400",
                plex_token="the-suites-plex-token",
            ),
            health_service=app.container.health_service(),
        )

        with pytest.raises(ExternalServiceException) as reading:
            client.read_queue(GARAGE, 0, 100)
        with pytest.raises(ExternalServiceException) as adding:
            client.add_many_to_queue(GARAGE, [_album("10"), _album("20")])
        with pytest.raises(ExternalServiceException) as selecting:
            client.select_queue(GARAGE)

        assert reading.value.operation == f"read the queue of speaker {GARAGE}"
        assert adding.value.operation == (
            f"add 2 Plex items to the queue of speaker {GARAGE}"
        )
        assert selecting.value.operation == (
            f"make the queue the source of speaker {GARAGE}"
        )
        for raised in (reading, adding, selecting):
            assert raised.value.cause.startswith(
                "none of the 1 seed speakers answered (the speaker did not answer ("
            )
            assert "192.0.2" not in raised.value.message
        # More than one call takes is refused before any speaker is asked.
        with pytest.raises(ValueError, match="takes 1 to 16 items, not 17"):
            client.add_many_to_queue(GARAGE, [_album("10")] * 17)


class TestSonosClientPicture:
    def test_read_picture_of_the_current_track(self, household):
        picture = household.read_playback(GARAGE).track.picture

        assert household.read_picture(GARAGE, picture) == household.picture_of(picture)

    def test_a_picture_is_served_after_its_track_stops_being_current(self, household):
        picture = household.read_playback(GARAGE).track.picture
        household.next(GARAGE)
        assert household.read_playback(GARAGE).track.picture != picture

        assert household.read_picture(GARAGE, picture) == household.picture_of(picture)

    def test_a_picture_is_a_jpeg_of_its_own_colour(self, household):
        first, second = queue_track(1).picture, queue_track(2).picture

        image = Image.open(io.BytesIO(household.read_picture(GARAGE, first)))

        assert (image.format, image.size) == ("JPEG", (300, 300))
        assert picture_colour(first) != picture_colour(second)
        pixel = image.convert("RGB").getpixel((150, 150))
        assert all(
            abs(got - want) <= 8
            for got, want in zip(pixel, picture_colour(first), strict=True)
        )

    def test_a_test_has_a_speaker_serve_what_it_gives(self, household):
        picture = household.read_playback(GARAGE).track.picture

        household.serve_picture(picture, b"not a picture")

        assert household.read_picture(GARAGE, picture) == b"not a picture"

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
            QueueChanged(GARAGE),
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
        # The device keeps the subscription it took at its old address.
        assert household.subscribed_addresses(GARAGE) == {before}

        _subscribe(household, GARAGE, SonosService.RENDERING_CONTROL)
        assert household.subscribed_addresses(GARAGE) == {before, address}

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

    def test_an_event_is_delivered_once_what_is_in_step_is_done_with_the_one_before(
        self, household
    ):
        listener, _ = _subscribe(household, GARAGE, SonosService.RENDERING_CONTROL)
        # What was delivered at each wait for what took the events.
        delivered = []
        household.in_step_with(
            lambda timeout: delivered.append(len(listener.events)) or True
        )

        household.set_volume(GARAGE, 21)
        household.set_volume(GARAGE, 22)
        household.settle()

        assert [event.volume for event in listener.events] == [21, 22]
        assert delivered == [1, 2]

    def test_what_is_in_step_and_is_not_done_in_time_fails_the_test(self, household):
        _subscribe(household, GARAGE, SonosService.RENDERING_CONTROL)
        household.in_step_with(lambda timeout: False)

        household.set_volume(GARAGE, 21)

        with pytest.raises(AssertionError, match="was not done in time"):
            household.settle()

    def test_unsubscribe_does_not_wait_for_what_is_in_step(self, household):
        import threading

        listener, subscription = _subscribe(
            household, GARAGE, SonosService.RENDERING_CONTROL
        )
        waiting, go_on = threading.Event(), threading.Event()

        def wait(timeout):
            waiting.set()
            return go_on.wait(timeout)

        household.in_step_with(wait)
        household.set_volume(GARAGE, 21)
        try:
            assert waiting.wait(5.0)
            subscription.unsubscribe()
        finally:
            go_on.set()
        household.settle()

        assert household.subscription_count(GARAGE) == 0

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

        with pytest.raises(ExternalServiceException, match="did not answer"):
            household.read_volume(GARAGE)
        with pytest.raises(ExternalServiceException, match="did not answer"):
            household.subscribe(
                GARAGE, SonosService.AV_TRANSPORT, lambda event: None, lambda: None
            )
        with pytest.raises(ExternalServiceException, match="did not answer"):
            subscription.unsubscribe()
        assert household.subscription_count(GARAGE) == 0
        # The household still lists it, and the other rooms answer.
        assert len(household.read_household().rooms) == 2
        assert household.read_volume(LOUNGE) == Volume(40, False)

    def test_a_silent_coordinator_fails_every_call_to_its_group(self, household):
        household.join(LOUNGE, GARAGE)
        household.silence_room(GARAGE)

        # A call to a group goes to its coordinator, whichever room it names.
        for speaker_id in (GARAGE, LOUNGE):
            with pytest.raises(ExternalServiceException, match="did not answer"):
                household.read_playback(speaker_id)
            with pytest.raises(ExternalServiceException, match="did not answer"):
                household.read_group_volume(speaker_id)
            with pytest.raises(ExternalServiceException, match="did not answer"):
                household.play(speaker_id)
            with pytest.raises(ExternalServiceException, match="did not answer"):
                household.set_group_volume(speaker_id, 10)
        # The calls to the room that follows it reach that room.
        assert household.read_volume(LOUNGE) == Volume(40, False)
        assert household.read_model(LOUNGE) == "Playbase"

    def test_a_silent_room_that_follows_fails_no_call_to_its_group(self, household):
        household.join(LOUNGE, GARAGE)
        household.silence_room(LOUNGE)

        assert household.read_group_volume(LOUNGE) == Volume(30, False)
        assert household.read_playback(LOUNGE) == household.read_playback(GARAGE)
        household.play(LOUNGE)
        assert household.read_playback(GARAGE).transport_state is TransportState.PLAYING
        with pytest.raises(ExternalServiceException, match="did not answer"):
            household.read_volume(LOUNGE)

    def test_a_silent_room_can_answer_again(self, household):
        household.silence_room(GARAGE)
        household.answer_again(GARAGE)

        assert household.read_volume(GARAGE) == Volume(20, False)

    def test_before_call_runs_between_two_calls_and_not_for_its_own(self, household):
        seen = []

        def turn_the_lounge_up(speaker_id):
            seen.append(speaker_id)
            household.set_volume(LOUNGE, 41)

        household.before_call = turn_the_lounge_up

        assert household.read_volume(LOUNGE) == Volume(41, False)
        assert seen == [LOUNGE]
