"""The SoCo client against the real speakers: what the fake cannot prove."""

import threading
import time
from collections.abc import Callable, Generator
from ipaddress import IPv4Address
from typing import Any

import pytest

from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckStatus
from app.services.sonos_client import (
    UNREPORTED,
    GroupVolumeChanged,
    HouseholdChanged,
    Playback,
    PlaybackChanged,
    SoCoSonosClient,
    SonosService,
    TransportAction,
    TransportState,
    Volume,
    VolumeChanged,
)

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
        assert layout == HouseholdChanged(garage, sonos.read_household())
        assert listener.lost == 0

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
            sonos.stop(garage)
            stopped = listener.take(
                lambda e: e.transport_state is TransportState.STOPPED
                and e.transport_uri is None
            )
            subscription.unsubscribe()
        # A partial event: the track and its place, and no actions.
        assert stopped.actions is None
        assert stopped.track == sonos.read_playback(garage).track
        assert stopped.queue_position == sonos.read_playback(garage).queue_position
