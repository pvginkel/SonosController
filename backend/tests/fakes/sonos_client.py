"""The in-memory Sonos household pytest runs against.

``FakeSonosClient`` keeps the state the real speakers keep: rooms with their
hidden devices, groups, each room's transport with its queue or stream, and
room and group volume. It answers ``SonosClient``'s calls from that state and
sends events the way ``docs/api-design.md`` section 1.1 found the speakers
send them: a service's state in full when a subscription starts, then partial
events, several for one change, on a thread of their own. A ``Queue`` event
follows every change to a queue, a change of play mode included.

A room's queue is kept the way ``docs/api-design.md`` sections 1.3 to 1.6
found the speakers keep one. A container of the Plex service becomes the
tracks the fake Plex serves for it; a container the service does not have, or
one beyond what a speaker fetches in time, is refused with UPnP error 800 and
queues nothing, as is a call while ``fail_fetches()`` has the service fail.
The base class makes a refused call again, without the pause between calls. With shuffle on, the queue is presented in play order, and
tracks added then are mixed into what plays after the current one.

A test builds its household with ``add_room()`` and ``load_queue()``. A change
made "elsewhere", as the Sonos app makes one, is the same call a display's
would be (``play()``, ``set_volume()``, ``join()``) or one of the methods
under "What happens to a household". ``settle()`` waits until every event
sent so far is delivered.

The household mirror applies the layouts it is sent on a thread of its own.
``in_step_with()`` has the fake deliver an event only once that thread is
done with the events before it, so that a test sees the events dealt with in
the order they were sent, and ``settle()`` then waits for that thread too.
"""

from __future__ import annotations

import hashlib
import io
import random
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from app.core_exceptions import ExternalServiceException
from app.exceptions import CommandRefusedException
from app.services.health_service import CheckResult, CheckStatus, HealthService
from app.services.sonos_client import (
    FetchRefusedException,
    Group,
    GroupVolumeChanged,
    Household,
    HouseholdChanged,
    Playback,
    PlaybackChanged,
    PlexItem,
    QueueChanged,
    QueuePage,
    Room,
    SonosClient,
    SonosEvent,
    SonosService,
    SonosSubscription,
    Track,
    TransportAction,
    TransportState,
    Volume,
    VolumeChanged,
)
from tests.fakes.plex_client import MACHINE_IDENTIFIER, FakePlexClient, ServedTrack

# What a speaker allows on a stream that cannot pause (section 1.7).
_STREAM_ACTIONS = frozenset({TransportAction.PLAY, TransportAction.STOP})

# The most tracks one call queues: about what a speaker fetches from the Plex
# service in the 10 seconds it waits (section 1.6: 1,243 tracks in 4.9 s).
CONTAINER_LIMIT = 2500

# The side of the picture a speaker serves for a track (section 1.9).
_PICTURE_SIZE = 300


def queue_track(
    number: int,
    album: str = "An Album",
    artist: str = "An Artist",
    machine_identifier: str = MACHINE_IDENTIFIER,
) -> Track:
    """A track as the speaker reports one queued from the Plex service: of
    the fake Plex, or of the Plex server ``machine_identifier`` names."""
    uri = (
        f"x-sonos-http:{machine_identifier}%3a1%3a{1000 + number}%3atrack.mp3"
        "?sid=212&flags=8300&sn=1"
    )
    return Track(
        title=f"Track {number}",
        artist=artist,
        album=album,
        stream_content="",
        duration_seconds=180 + number,
        uri=uri,
        picture=f"/getaa?s=1&u={uri}",
    )


def app_track(number: int) -> Track:
    """A track as the speaker reports one the Sonos app queued from a music
    service that is not Plex."""
    uri = f"x-sonos-spotify:spotify%3atrack%3a{number}?sid=9&flags=8224&sn=7"
    return Track(
        title=f"App Track {number}",
        artist="An App Artist",
        album="An App Album",
        stream_content="",
        duration_seconds=200 + number,
        uri=uri,
        picture=f"/getaa?s=1&u={uri}",
    )


def _entry(item: PlexItem, track: ServedTrack) -> Track:
    """A queue entry of a track the Plex service served for a container."""
    uri = (
        f"x-sonos-http:{item.machine_identifier}%3a{item.section_id}"
        f"%3a{track.rating_key}%3atrack.mp3?sid=212&flags=8300&sn=1"
    )
    return Track(
        title=track.title,
        artist=track.artist,
        album=track.album,
        stream_content="",
        duration_seconds=(track.duration_ms or 0) // 1000,
        uri=uri,
        picture=f"/getaa?s=1&u={uri}",
    )


def picture_colour(picture: str) -> tuple[int, int, int]:
    """The one colour of the picture a speaker serves at an address, unless a
    test had it serve something else there."""
    digest = hashlib.sha256(picture.encode()).digest()
    return digest[0], digest[1], digest[2]


@dataclass
class _Room:
    id: str
    name: str
    model: str
    address: str
    boot_seq: int
    volume: int
    muted: bool
    hidden_device_ids: tuple[str, ...]
    coordinator_id: str
    # The room's own transport: what plays when it coordinates its group.
    state: TransportState = TransportState.STOPPED
    shuffle: bool = False
    # The queue as the speaker presents it, and each entry's place in the
    # queue's normal order: with shuffle off, 0, 1, 2 and so on.
    queue: list[Track] = field(default_factory=list)
    normal: list[int] = field(default_factory=list)
    position: int = 0
    # A stream replaces the queue as the transport, and the queue stays.
    stream: Track | None = None
    stream_uri: str = ""
    stream_title: str = ""


class _FakeSubscription(SonosSubscription):
    def __init__(
        self,
        client: FakeSonosClient,
        speaker_id: str,
        address: str,
        service: SonosService,
        on_event: Callable[[SonosEvent], None],
        on_lost: Callable[[], None],
    ) -> None:
        self._client = client
        self.speaker_id = speaker_id
        # The device's address when it took the subscription.
        self.address = address
        self.service = service
        self.on_event = on_event
        self.on_lost = on_lost
        # False once the speaker no longer holds it: after a restart, a failed
        # renewal or unsubscribe().
        self.held = True
        self.unsubscribed = False

    def renew(self) -> None:
        self._client._require_reachable(f"renew the subscription to {self.service}")
        if not self.held:
            raise ExternalServiceException(
                f"renew the subscription to {self.service} events of speaker "
                f"{self.speaker_id}",
                "the speaker answered 412",
            )

    def unsubscribe(self) -> None:
        self.held = False
        self.unsubscribed = True
        self._client._handled()
        self._client._require_answer(
            self.speaker_id,
            f"end the subscription to {self.service} events of speaker "
            f"{self.speaker_id}",
        )

    @property
    def lease_seconds(self) -> int:
        # What a speaker grants the real client.
        return 300


class FakeSonosClient(SonosClient):
    """A household in memory."""

    # A refused queueing call is made again at once.
    refetch_pause_seconds = 0.0

    def __init__(self, health_service: HealthService, plex: FakePlexClient) -> None:
        super().__init__(health_service)
        # The Plex the household's Plex service serves.
        self._plex = plex
        # False: the speakers list no Plex service.
        self.plex_linked = True
        # The speakers' shuffle, the same in every test.
        self._random = random.Random(0)
        self._rooms: dict[str, _Room] = {}
        self._subscriptions: list[_FakeSubscription] = []
        self._next_address = 1
        self._lagging_group_volume: dict[str, int] = {}
        self._silent: set[str] = set()
        self._served: dict[str, bytes] = {}
        # Called with the id of the room a call names before the call reaches
        # a device, and not for the calls it makes itself: a test makes
        # something happen in the household between two calls of the code it
        # tests.
        self.before_call: Callable[[str], None] | None = None
        # False: no speaker answers, as when the household is switched off.
        self.reachable = True
        # The queueing calls still to be refused for a fetch that failed.
        self._failing_fetches = 0
        self._condition = threading.Condition()
        self._pending: deque[Callable[[], None]] = deque()
        # True from when a delivery is taken until what in_step_with() named
        # is done with it; _handling only while its handler runs.
        self._delivering = False
        self._handling = False
        self._in_step_with: list[Callable[[float], bool]] = []
        self._closed = False
        self._errors: list[BaseException] = []
        self._delivery = threading.Thread(
            target=self._deliver, name="fake-sonos-events", daemon=True
        )
        self._delivery.start()

    # ------------------------------------------------------------------
    # Building a household
    # ------------------------------------------------------------------

    def add_room(
        self,
        speaker_id: str,
        name: str,
        *,
        model: str = "Play:1",
        volume: int = 20,
        hidden_device_ids: tuple[str, ...] = (),
    ) -> None:
        """Add a room that plays alone, with nothing loaded.

        ``hidden_device_ids`` are its surrounds or the second half of a pair:
        part of the room, and no rooms themselves.
        """
        self._rooms[speaker_id] = _Room(
            id=speaker_id,
            name=name,
            model=model,
            # TEST-NET-1 (RFC 5737): addresses no network holds.
            address=f"192.0.2.{self._next_address}",
            boot_seq=1,
            volume=volume,
            muted=False,
            hidden_device_ids=hidden_device_ids,
            coordinator_id=speaker_id,
        )
        self._next_address += 1
        self._send_household()

    def load_queue(self, speaker_id: str, tracks: list[Track]) -> None:
        """Replace the queue of the room's group, as presented; the first
        track is current."""
        room = self._coordinator(speaker_id)
        room.queue = list(tracks)
        room.normal = list(range(len(tracks)))
        room.position = 1 if tracks else 0
        room.stream = None
        room.state = TransportState.STOPPED
        self._send_queue(room)
        self._send_playback(room, full=True)

    def start_stream(
        self,
        speaker_id: str,
        station: str,
        stream_content: str = "",
        scheme: str = "x-rincon-mp3radio",
    ) -> None:
        """Play a radio stream on the room's group, as the Sonos app starts one.

        The queue stays as it was (section 1.7). With another ``scheme`` the
        transport's address is that of another source: ``x-rincon-stream``
        a line-in, ``x-sonos-htastream`` a TV.
        """
        room = self._coordinator(speaker_id)
        slug = station.lower().replace(" ", "-")
        room.stream_uri = f"{scheme}://stream.invalid/{slug}"
        room.stream_title = station
        room.stream = Track(
            title=slug,
            artist="",
            album="",
            stream_content=stream_content,
            duration_seconds=0,
            uri=f"x-rincon-mp3radio://http://stream.invalid/{slug}",
            picture="",
        )
        room.state = TransportState.PLAYING
        self._send_playback(room, full=True)

    def report_stream_content(self, speaker_id: str, stream_content: str) -> None:
        """The stream reports what it plays now."""
        room = self._coordinator(speaker_id)
        assert room.stream is not None, "the room plays no stream"
        room.stream = Track(
            title=room.stream.title,
            artist="",
            album="",
            stream_content=stream_content,
            duration_seconds=0,
            uri=room.stream.uri,
            picture="",
        )
        self._send_playback(room)

    def start_buffering(self, speaker_id: str) -> None:
        """The room's group waits for what it is to play: ``TRANSITIONING``."""
        room = self._coordinator(speaker_id)
        room.state = TransportState.TRANSITIONING
        self._send_playback(room)

    # ------------------------------------------------------------------
    # What happens to a household
    # ------------------------------------------------------------------

    def rename_room(self, speaker_id: str, name: str) -> None:
        self._rooms[speaker_id].name = name
        self._send_household()

    def remove_room(self, speaker_id: str) -> None:
        """The room leaves the household: switched off and forgotten."""
        del self._rooms[speaker_id]
        for room in self._rooms.values():
            if room.coordinator_id == speaker_id:
                room.coordinator_id = room.id
        for subscription in self._held(speaker_id):
            subscription.held = False
        self._send_household()

    def restart_room(self, speaker_id: str) -> None:
        """The room's device restarts.

        It forgets its subscriptions without a word: nothing arrives from it
        any more, and no renewal has failed yet. The other rooms report its
        new boot sequence number.
        """
        self._rooms[speaker_id].boot_seq += 1
        for subscription in self._held(speaker_id):
            subscription.held = False
        self._send_household()

    def move_room(self, speaker_id: str) -> str:
        """The room's device takes another address. Returns the new address.

        The device keeps its subscriptions; their renewal, which goes to the
        old address, is what fails later (``fail_renewals()``).
        """
        room = self._rooms[speaker_id]
        room.address = f"192.0.2.{self._next_address}"
        self._next_address += 1
        self._send_household()
        return room.address

    def lag_group_volume(self, speaker_id: str, volume: int) -> None:
        """The room's device has not caught up with its group's volume.

        Until the group volume next changes, a new GroupRenderingControl
        subscription to the device starts with ``volume``, as the Garage's
        started with a volume the room had left a minute before. A read
        answers the group's volume as it is.
        """
        self._lagging_group_volume[speaker_id] = volume

    def silence_room(self, speaker_id: str) -> None:
        """The room's device stops answering.

        Every call to it fails, the end of a subscription included: the
        calls to the room itself, and the calls to a group it coordinates,
        whichever room of the group they name. The household still lists the
        room.
        """
        self._silent.add(speaker_id)

    def answer_again(self, speaker_id: str) -> None:
        """The room's device answers again, after ``silence_room()``."""
        self._silent.remove(speaker_id)

    def fail_fetches(self, calls: int) -> None:
        """The Plex service fails the speakers' fetches for a while.

        The next ``calls`` queueing calls, to any room, are refused with UPnP
        error 800 and queue nothing, as the Garage refused one of 16 albums
        that it queued when asked again (SCTL-17).
        """
        self._failing_fetches = calls

    def fail_renewals(self, speaker_id: str) -> None:
        """Every renewal of the room's subscriptions fails: each is lost."""
        for subscription in self._held(speaker_id):
            subscription.held = False
            self._enqueue(subscription.on_lost)

    # ------------------------------------------------------------------
    # For a test
    # ------------------------------------------------------------------

    def in_step_with(self, wait: Callable[[float], bool]) -> None:
        """Deliver an event only once what took the events before it to a
        thread of its own is done with them.

        ``wait(timeout)`` returns once that thread has nothing left to do:
        False when ``timeout`` seconds passed first, which fails the test
        at its next ``settle()``.
        """
        self._in_step_with.append(wait)

    def settle(self, timeout: float = 5.0) -> None:
        """Wait until every event sent so far has been delivered, and until
        what ``in_step_with()`` named is done with them.

        Not for a thread ``in_step_with()`` named while it deals with an
        event: the delivery waits for that thread.

        Raises what a handler raised, so that a failing handler fails the test
        that caused it.
        """
        if threading.current_thread() is self._delivery:
            return
        with self._condition:
            idle = self._condition.wait_for(
                lambda: not self._pending and not self._delivering, timeout
            )
            assert idle, "the fake's events were not delivered in time"
            if self._errors:
                error = self._errors.pop(0)
                self._errors.clear()
                raise error

    def _handled(self) -> None:
        """Wait until no handler is running: a subscription that is ended
        runs none afterwards."""
        if threading.current_thread() is self._delivery:
            return
        with self._condition:
            idle = self._condition.wait_for(lambda: not self._handling, 5.0)
            assert idle, "a handler of the fake's events did not return in time"

    def close(self) -> None:
        """End the delivery thread."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._delivery.join()

    def subscription_count(
        self, speaker_id: str, service: SonosService | None = None
    ) -> int:
        """How many subscriptions the room's device holds now."""
        return len(
            [
                subscription
                for subscription in self._held(speaker_id)
                if service is None or subscription.service is service
            ]
        )

    def subscribed_addresses(self, speaker_id: str) -> set[str]:
        """The addresses the room's device had when it took the subscriptions
        it holds now."""
        return {subscription.address for subscription in self._held(speaker_id)}

    def serve_picture(self, picture: str, content: bytes) -> None:
        """Have the speakers serve these bytes at a ``Track.picture``."""
        self._served[picture] = content

    def picture_of(self, picture: str) -> bytes:
        """The bytes ``read_picture()`` answers for a ``Track.picture``: what
        ``serve_picture()`` put there, else a JPEG of ``picture_colour()``."""
        if picture in self._served:
            return self._served[picture]
        jpeg = io.BytesIO()
        Image.new(
            "RGB", (_PICTURE_SIZE, _PICTURE_SIZE), picture_colour(picture)
        ).save(jpeg, "JPEG")
        return jpeg.getvalue()

    # ------------------------------------------------------------------
    # SonosClient
    # ------------------------------------------------------------------

    def check_readiness(self) -> CheckResult:
        if not self.reachable or not self._rooms:
            return CheckResult(CheckStatus.ERROR, "none of the seed speakers answered")
        return CheckResult(CheckStatus.OK)

    def read_household(self) -> Household:
        self._require_reachable("read the household's layout")
        return self._household()

    def read_model(self, speaker_id: str) -> str:
        return self._room(speaker_id, "read the model of").model

    def read_playback(self, speaker_id: str) -> Playback:
        room = self._coordinator(speaker_id, "read the playback of")
        return Playback(
            transport_state=room.state,
            transport_uri=self._transport_uri(room),
            transport_title=room.stream_title if room.stream else "",
            actions=self._actions(room),
            shuffle=room.shuffle,
            queue_position=self._position(room),
            queue_length=self._length(room),
            track=self._track(room),
        )

    def read_volume(self, speaker_id: str) -> Volume:
        room = self._room(speaker_id, "read the volume of")
        return Volume(room.volume, room.muted)

    def read_group_volume(self, speaker_id: str) -> Volume:
        coordinator = self._coordinator(speaker_id, "read the group volume of")
        return self._group_volume(coordinator)

    def play(self, speaker_id: str) -> None:
        room = self._command(speaker_id, "play on", TransportAction.PLAY)
        room.state = TransportState.PLAYING
        self._send_playback(room, via=TransportState.TRANSITIONING)

    def pause(self, speaker_id: str) -> None:
        room = self._command(speaker_id, "pause", TransportAction.PAUSE)
        room.state = TransportState.PAUSED
        self._send_playback(room)

    def stop(self, speaker_id: str) -> None:
        room = self._command(speaker_id, "stop", TransportAction.STOP)
        room.state = TransportState.STOPPED
        self._send_playback(room)

    def next(self, speaker_id: str) -> None:
        room = self._command(
            speaker_id, "skip to the next track on", TransportAction.NEXT
        )
        room.position += 1
        self._send_track_change(room)

    def previous(self, speaker_id: str) -> None:
        room = self._command(
            speaker_id, "go back to the previous track on", TransportAction.PREVIOUS
        )
        room.position -= 1
        self._send_track_change(room)

    def set_volume(self, speaker_id: str, volume: int) -> None:
        room = self._room(speaker_id, "set the volume of")
        self._set_room_volume(room, volume)
        self._send_group_volume(self._rooms[room.coordinator_id])

    def change_volume(self, speaker_id: str, delta: int) -> int:
        room = self._room(speaker_id, "change the volume of")
        self._set_room_volume(room, room.volume + delta)
        self._send_group_volume(self._rooms[room.coordinator_id])
        return room.volume

    def set_muted(self, speaker_id: str, muted: bool) -> None:
        room = self._room(speaker_id, "mute or unmute")
        room.muted = muted
        self._send(room.id, VolumeChanged(room.id, muted=muted))
        self._send_group_volume(self._rooms[room.coordinator_id])

    def set_group_volume(self, speaker_id: str, volume: int) -> None:
        coordinator = self._coordinator(speaker_id, "set the group volume of")
        self._set_group_volume(coordinator, volume)

    def change_group_volume(self, speaker_id: str, delta: int) -> int:
        coordinator = self._coordinator(speaker_id, "change the group volume of")
        self._set_group_volume(
            coordinator, self._group_volume(coordinator).volume + delta
        )
        return self._group_volume(coordinator).volume

    def set_group_muted(self, speaker_id: str, muted: bool) -> None:
        coordinator = self._coordinator(speaker_id, "mute or unmute the group of")
        for member in self._members(coordinator):
            member.muted = muted
            self._send(member.id, VolumeChanged(member.id, muted=muted))
        self._send_group_volume(coordinator)

    def join(self, speaker_id: str, member_id: str) -> None:
        operation = f"join the group of speaker {member_id} with"
        room = self._room(speaker_id, operation)
        # The command goes to the room that joins alone: the group's rooms
        # are named in it and not reached.
        if member_id not in self._rooms:
            raise ExternalServiceException(
                f"{operation} speaker {speaker_id}",
                f"speaker {member_id} is not in the household",
            )
        coordinator = self._rooms[self._rooms[member_id].coordinator_id]
        if room.coordinator_id == coordinator.id:
            # What a speaker does with a join to the group it plays in was
            # never tried on the real speakers. Here it changes nothing.
            return
        self._move(room, coordinator)

    def unjoin(self, speaker_id: str) -> None:
        room = self._room(speaker_id, "ungroup")
        if self._members(room) == [room]:
            # What a speaker does with a leave by a room alone was never
            # tried on the real speakers. Here it changes nothing.
            return
        self._move(room, room)

    def read_queue(self, speaker_id: str, offset: int, limit: int) -> QueuePage:
        room = self._coordinator(speaker_id, "read the queue of")
        return QueuePage(
            tracks=tuple(room.queue[offset : offset + limit]), total=len(room.queue)
        )

    def clear_queue(self, speaker_id: str) -> None:
        room = self._coordinator(speaker_id, "clear the queue of")
        room.queue = []
        room.normal = []
        room.position = 0
        if not room.stream:
            room.state = TransportState.STOPPED
        self._send_queue(room)
        self._send_playback(room)

    def select_queue(self, speaker_id: str) -> None:
        room = self._coordinator(speaker_id, "make the queue the source of")
        if room.stream:
            room.stream = None
            room.state = TransportState.STOPPED
            self._send_playback(room, full=True)

    def set_shuffle(self, speaker_id: str, shuffle: bool) -> None:
        operation = "set the play mode of"
        room = self._coordinator(speaker_id, operation)
        if shuffle and room.stream:
            # As the Garage answered with a radio stream as its source.
            raise ExternalServiceException(
                f"{operation} speaker {speaker_id}",
                "the speaker answered UPnP error 712 (Play mode not supported)",
            )
        changed = shuffle != room.shuffle
        # A queue that holds tracks is at one of them.
        if changed and room.queue:
            current = room.position - 1
            if shuffle:
                # The current track stays first, and the rest follow drawn.
                rest = [index for index in range(len(room.queue)) if index != current]
                self._random.shuffle(rest)
                order = [current, *rest]
            else:
                order = sorted(range(len(room.queue)), key=room.normal.__getitem__)
            room.queue = [room.queue[index] for index in order]
            room.normal = [room.normal[index] for index in order]
            room.position = order.index(current) + 1
        room.shuffle = shuffle
        if changed:
            self._send_queue(room)
        self._send_playback(room)

    def _send_add(self, speaker_id: str, item: PlexItem) -> None:
        self._add(
            speaker_id,
            f"add Plex {item.kind} {item.rating_key} to the queue of",
            [item],
        )

    def _send_add_many(self, speaker_id: str, items: Sequence[PlexItem]) -> None:
        self._add(speaker_id, f"add {len(items)} Plex items to the queue of", items)

    def play_from_start(self, speaker_id: str) -> None:
        operation = "play from the first track on"
        room = self._coordinator(speaker_id, operation)
        # The speaker takes the queue as its transport before it seeks.
        transport_changed = room.stream is not None
        room.stream = None
        if not room.queue:
            if transport_changed:
                room.state = TransportState.STOPPED
                self._send_playback(room, full=True)
            raise ExternalServiceException(
                f"{operation} speaker {speaker_id}",
                "the speaker answered UPnP error 711 (Illegal seek target)",
            )
        room.position = 1
        room.state = TransportState.PLAYING
        self._send_playback(
            room, full=transport_changed, via=TransportState.TRANSITIONING
        )

    def read_picture(self, speaker_id: str, picture: str) -> bytes:
        self._coordinator(speaker_id, "read the picture served by")
        # A speaker serves /getaa for the address it is given, whatever plays.
        address = urlsplit(picture)
        if address.path != "/getaa" or "u" not in parse_qs(address.query):
            raise ExternalServiceException(
                f"read the picture speaker {speaker_id} serves",
                "the speaker answered 404",
            )
        return self.picture_of(picture)

    def subscribe(
        self,
        speaker_id: str,
        service: SonosService,
        on_event: Callable[[SonosEvent], None],
        on_lost: Callable[[], None],
    ) -> SonosSubscription:
        room = self._room(speaker_id, f"subscribe to {service} events of")
        subscription = _FakeSubscription(
            self, speaker_id, room.address, service, on_event, on_lost
        )
        self._subscriptions.append(subscription)
        self._enqueue(lambda: self._deliver_to(subscription, self._full(room, service)))
        return subscription

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _require_reachable(self, operation: str) -> None:
        if not self.reachable:
            raise ExternalServiceException(
                operation, "none of the seed speakers answered"
            )

    def _require_answer(self, speaker_id: str, operation: str) -> None:
        if speaker_id in self._silent:
            raise ExternalServiceException(
                operation, "the speaker did not answer (ConnectTimeout)"
            )

    def _room(self, speaker_id: str, operation: str = "reach") -> _Room:
        """The room, for a call that goes to its own device."""
        room = self._listed(speaker_id, operation)
        self._require_answer(room.id, f"{operation} speaker {speaker_id}")
        return room

    def _coordinator(self, speaker_id: str, operation: str = "reach") -> _Room:
        """The coordinator of the room's group, for a call that goes to the
        group: SoCo sends it to the coordinator's device, and none to the
        room's own."""
        coordinator_id = self._listed(speaker_id, operation).coordinator_id
        self._require_answer(coordinator_id, f"{operation} speaker {speaker_id}")
        return self._rooms[coordinator_id]

    def _listed(self, speaker_id: str, operation: str) -> _Room:
        """The room a call names, as the household lists it when the call
        reaches a device."""
        before_call = self.before_call
        if before_call is not None:
            self.before_call = None
            try:
                before_call(speaker_id)
            finally:
                self.before_call = before_call
        described = f"{operation} speaker {speaker_id}"
        self._require_reachable(described)
        if speaker_id not in self._rooms:
            raise ExternalServiceException(
                described, f"speaker {speaker_id} is not in the household"
            )
        return self._rooms[speaker_id]

    def _command(
        self, speaker_id: str, operation: str, action: TransportAction
    ) -> _Room:
        room = self._coordinator(speaker_id, operation)
        if action not in self._actions(room):
            # The Garage answers a previous at its queue's first track with
            # 711, and a next on a radio stream with 701 (docs/api-design.md
            # section 1.7).
            seek = action in (TransportAction.NEXT, TransportAction.PREVIOUS)
            refusal = (
                "711 (Illegal seek target)"
                if seek and room.queue and not room.stream
                else "701 (Transition not available)"
            )
            raise CommandRefusedException(
                f"{operation} speaker {speaker_id}",
                f"the speaker answered UPnP error {refusal}",
            )
        return room

    def _add(self, speaker_id: str, operation: str, items: Sequence[PlexItem]) -> None:
        """Append the tracks the Plex service serves for the items' containers."""
        room = self._coordinator(speaker_id, operation)
        described = f"{operation} speaker {speaker_id}"
        if not self.plex_linked:
            raise ExternalServiceException(
                described, "the speaker lists no Plex service"
            )
        if self._failing_fetches:
            self._failing_fetches -= 1
            raise FetchRefusedException(
                described, "the speaker answered UPnP error 800"
            )
        tracks: list[Track] = []
        for item in items:
            served = (
                self._plex.served_tracks(item.kind, item.rating_key)
                if item.machine_identifier == MACHINE_IDENTIFIER
                else None
            )
            if served is None:
                # As the Garage answered a container of no item of the library.
                raise FetchRefusedException(
                    described, "the speaker answered UPnP error 800"
                )
            tracks.extend(_entry(item, track) for track in served)
        if len(tracks) > CONTAINER_LIMIT:
            raise FetchRefusedException(
                described, "the speaker answered UPnP error 800"
            )
        normal = range(len(room.queue), len(room.queue) + len(tracks))
        for track, index in zip(tracks, normal, strict=True):
            # In play order, what is added while shuffle is on lands anywhere
            # after the current track.
            place = (
                self._random.randint(room.position, len(room.queue))
                if room.shuffle
                else len(room.queue)
            )
            room.queue.insert(place, track)
            room.normal.insert(place, index)
        if tracks and not room.position:
            room.position = 1
        self._send_queue(room)
        self._send_playback(room)

    def _members(self, coordinator: _Room) -> list[_Room]:
        return [
            room
            for room in self._rooms.values()
            if room.coordinator_id == coordinator.id
        ]

    def _move(self, room: _Room, coordinator: _Room) -> None:
        """Put the room in the group ``coordinator`` leads, which is the room
        itself when it is to play alone, and send what the devices report."""
        left = [
            member
            for member in self._members(self._rooms[room.coordinator_id])
            if member is not room
        ]
        handed_over = bool(left) and room.coordinator_id == room.id
        if handed_over:
            # The rooms it led stay a group, led by the first of them by id.
            # What they play then, and what the room that led them plays, was
            # never seen on the real speakers. Here every room keeps its own
            # transport: the group it left plays its new coordinator's own
            # queue, and a room that leaves to play alone plays what it led
            # its group in.
            leader = min(left, key=lambda member: member.id)
            for member in left:
                member.coordinator_id = leader.id
        room.coordinator_id = coordinator.id
        self._send_household()
        if left:
            self._send_group_volume(self._rooms[left[0].coordinator_id])
        self._send_group_volume(coordinator)
        if handed_over:
            self._send_playback(leader, full=True)
            for member in left:
                if member is not leader:
                    self._send_follows(member)
        if coordinator is room:
            # Its transport is its own queue again.
            self._send_playback(room, full=True)
        else:
            self._send_follows(room)

    def _household(self) -> Household:
        rooms = sorted(self._rooms.values(), key=lambda room: room.id)
        return Household(
            rooms=tuple(
                Room(room.id, room.name, room.address, room.boot_seq) for room in rooms
            ),
            groups=tuple(
                Group(
                    coordinator_id=room.id,
                    member_ids=tuple(
                        sorted(member.id for member in self._members(room))
                    ),
                )
                for room in rooms
                if room.coordinator_id == room.id
            ),
        )

    def _transport_uri(self, room: _Room) -> str:
        return room.stream_uri if room.stream else f"x-rincon-queue:{room.id}#0"

    def _track(self, room: _Room) -> Track | None:
        if room.stream:
            return room.stream
        return room.queue[room.position - 1] if room.position else None

    def _position(self, room: _Room) -> int:
        return 1 if room.stream else room.position

    def _length(self, room: _Room) -> int:
        return 1 if room.stream else len(room.queue)

    def _actions(self, room: _Room) -> frozenset[TransportAction]:
        if room.stream:
            return _STREAM_ACTIONS
        if not room.queue:
            return frozenset()
        actions = {TransportAction.PLAY, TransportAction.PAUSE, TransportAction.STOP}
        if room.position < len(room.queue):
            actions.add(TransportAction.NEXT)
        if room.position > 1:
            actions.add(TransportAction.PREVIOUS)
        return frozenset(actions)

    def _group_volume(self, coordinator: _Room) -> Volume:
        members = self._members(coordinator)
        return Volume(
            volume=round(sum(member.volume for member in members) / len(members)),
            muted=all(member.muted for member in members),
        )

    def _set_room_volume(self, room: _Room, volume: int) -> None:
        room.volume = max(0, min(100, volume))
        self._send(room.id, VolumeChanged(room.id, volume=room.volume))

    def _set_group_volume(self, coordinator: _Room, volume: int) -> None:
        """Move every member in proportion, as the group volume does."""
        volume = max(0, min(100, volume))
        before = self._group_volume(coordinator).volume
        for member in self._members(coordinator):
            scaled = round(member.volume * volume / before) if before else volume
            self._set_room_volume(member, scaled)
        self._send_group_volume(coordinator)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _held(self, speaker_id: str) -> list[_FakeSubscription]:
        return [
            subscription
            for subscription in self._subscriptions
            if subscription.speaker_id == speaker_id and subscription.held
        ]

    def _full(self, room: _Room, service: SonosService) -> SonosEvent:
        """A service's state in full: what a new subscription receives."""
        if service is SonosService.AV_TRANSPORT:
            return self._playback_event(room, full=True)
        if service is SonosService.RENDERING_CONTROL:
            return VolumeChanged(room.id, volume=room.volume, muted=room.muted)
        if service is SonosService.GROUP_RENDERING_CONTROL:
            group = self._group_volume(self._rooms[room.coordinator_id])
            volume = self._lagging_group_volume.get(room.id, group.volume)
            return GroupVolumeChanged(room.id, volume=volume, muted=group.muted)
        if service is SonosService.QUEUE:
            return QueueChanged(room.id)
        return HouseholdChanged(room.id, self._household())

    def _playback_event(
        self, room: _Room, *, full: bool = False, state: TransportState | None = None
    ) -> PlaybackChanged:
        """The transport as an event names it.

        Only a full event carries the transport's address and the actions:
        "Some AVTransport events carry only the allowed actions, and most
        leave them out" (section 1.1).
        """
        return PlaybackChanged(
            room.id,
            transport_state=state or room.state,
            transport_uri=self._transport_uri(room) if full else None,
            transport_title=(room.stream_title if room.stream else "")
            if full
            else None,
            actions=self._actions(room) if full else None,
            shuffle=room.shuffle,
            queue_position=self._position(room),
            queue_length=self._length(room),
            track=self._track(room),
        )

    def _send_playback(
        self, room: _Room, *, full: bool = False, via: TransportState | None = None
    ) -> None:
        """Send a change of the room's own transport, to its own device."""
        if via is not None:
            self._send(room.id, self._playback_event(room, state=via))
        self._send(room.id, self._playback_event(room, full=full))
        if not full:
            self._send(room.id, PlaybackChanged(room.id, actions=self._actions(room)))

    def _send_follows(self, room: _Room) -> None:
        """Send the transport of a room that follows a coordinator: a room
        joins by setting its own transport to the coordinator (section 1.2),
        and its own device reports that."""
        self._send(
            room.id,
            PlaybackChanged(room.id, transport_uri=f"x-rincon:{room.coordinator_id}"),
        )

    def _send_queue(self, room: _Room) -> None:
        """Send a change of the room's own queue, to its own device."""
        self._send(room.id, QueueChanged(room.id))

    def _send_track_change(self, room: _Room) -> None:
        playing = room.state is TransportState.PLAYING
        self._send_playback(room, via=TransportState.TRANSITIONING if playing else None)

    def _send_group_volume(self, coordinator: _Room) -> None:
        group = self._group_volume(coordinator)
        self._lagging_group_volume.pop(coordinator.id, None)
        self._send(
            coordinator.id,
            GroupVolumeChanged(coordinator.id, volume=group.volume, muted=group.muted),
            SonosService.GROUP_RENDERING_CONTROL,
        )

    def _send_household(self) -> None:
        household = self._household()
        for subscription in self._subscriptions:
            if (
                subscription.held
                and subscription.service is SonosService.ZONE_GROUP_TOPOLOGY
            ):
                event = HouseholdChanged(subscription.speaker_id, household)
                self._enqueue(lambda s=subscription, e=event: self._deliver_to(s, e))

    def _send(
        self, speaker_id: str, event: SonosEvent, service: SonosService | None = None
    ) -> None:
        """Queue an event of a room's device for the subscriptions it holds."""
        if service is None:
            if isinstance(event, PlaybackChanged):
                service = SonosService.AV_TRANSPORT
            elif isinstance(event, QueueChanged):
                service = SonosService.QUEUE
            else:
                service = SonosService.RENDERING_CONTROL
        for subscription in self._held(speaker_id):
            if subscription.service is service:
                self._enqueue(lambda s=subscription: self._deliver_to(s, event))

    def _deliver_to(self, subscription: _FakeSubscription, event: SonosEvent) -> None:
        if not subscription.unsubscribed:
            subscription.on_event(event)

    def _enqueue(self, delivery: Callable[[], None]) -> None:
        with self._condition:
            self._pending.append(delivery)
            self._condition.notify_all()

    def _deliver(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if self._closed:
                    return
                delivery = self._pending.popleft()
                self._delivering = True
                self._handling = True
            try:
                try:
                    delivery()
                finally:
                    with self._condition:
                        self._handling = False
                        self._condition.notify_all()
                for wait in self._in_step_with:
                    assert wait(5.0), "what took the fake's events was not done in time"
            except BaseException as error:
                self._errors.append(error)
            finally:
                with self._condition:
                    self._delivering = False
                    self._condition.notify_all()
