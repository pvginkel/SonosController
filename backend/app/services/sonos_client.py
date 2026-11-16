"""The Sonos client: the backend's only way to the speakers.

``SonosClient`` is what the rest of the backend sees: the household's layout,
a room's and its group's state, the transport and volume commands, the
speakers' event subscriptions and the picture a speaker serves for what it
plays. ``SoCoSonosClient`` implements it on SoCo, and this is the only module
that imports SoCo. pytest overrides the container's provider with an in-memory
fake of the same base class.

A room is addressed by its Sonos id (``RINCON_…``), the id of its one visible
device. Everything a speaker reports is passed on as the speaker gives it;
strings are not normalised here.
"""

import logging
import queue
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any
from xml.etree.ElementTree import ParseError

import requests
import soco.config
import soco.events
from soco import SoCo
from soco.core import PLAY_MODES
from soco.data_structures import DidlObject
from soco.data_structures_entry import from_didl_string
from soco.exceptions import SoCoException, SoCoUPnPException

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.exceptions import ConfigurationError
from app.services.health_service import CheckResult, CheckStatus, HealthService

logger = logging.getLogger(__name__)


class TransportState(StrEnum):
    """A transport's state, in the speaker's words."""

    PLAYING = "PLAYING"
    PAUSED = "PAUSED_PLAYBACK"
    STOPPED = "STOPPED"
    TRANSITIONING = "TRANSITIONING"


class TransportAction(StrEnum):
    """A transport command a speaker can allow, in the speaker's words."""

    PLAY = "Play"
    PAUSE = "Pause"
    STOP = "Stop"
    NEXT = "Next"
    PREVIOUS = "Previous"


class SonosService(StrEnum):
    """A speaker's service that sends events."""

    AV_TRANSPORT = "AVTransport"
    RENDERING_CONTROL = "RenderingControl"
    GROUP_RENDERING_CONTROL = "GroupRenderingControl"
    ZONE_GROUP_TOPOLOGY = "ZoneGroupTopology"


@dataclass(frozen=True)
class Room:
    """A room: its one visible device. Hidden devices are not rooms."""

    id: str
    name: str
    # The visible device's IPv4 address and boot sequence number: a
    # subscription is held by the device at this address since this boot.
    address: str
    boot_seq: int


@dataclass(frozen=True)
class Group:
    """The rooms that play together. A room alone is a group of one."""

    coordinator_id: str
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class Household:
    """The household's layout: rooms and groups, each sorted by id."""

    rooms: tuple[Room, ...]
    groups: tuple[Group, ...]


@dataclass(frozen=True)
class Track:
    """The track a transport has loaded, as its metadata gives it.

    A text the metadata lacks is empty. ``stream_content`` is what a radio
    stream reports as playing; ``picture`` is the address of the picture the
    speaker serves for the track, for ``read_picture()``.
    """

    title: str
    artist: str
    album: str
    stream_content: str
    duration_seconds: int
    uri: str
    picture: str


@dataclass(frozen=True)
class Playback:
    """A transport in full.

    ``transport_title`` is the title of what the transport address names, a
    radio station's for one; it is empty for the queue. ``queue_position`` is
    1-based.
    """

    transport_state: TransportState
    transport_uri: str
    transport_title: str
    actions: frozenset[TransportAction]
    shuffle: bool
    queue_position: int
    queue_length: int
    track: Track | None


@dataclass(frozen=True)
class Volume:
    """A room's or a group's volume (0 to 100) and mute."""

    volume: int
    muted: bool


class Unreported(Enum):
    """Marks a field an event did not carry, where None is a value."""

    UNREPORTED = "unreported"


UNREPORTED = Unreported.UNREPORTED


@dataclass(frozen=True)
class PlaybackChanged:
    """An AVTransport event of a room's own device.

    Partial: a field the event did not carry is None, and ``track`` is
    ``UNREPORTED``. A room that follows a coordinator reports its own
    transport, whose address names the coordinator.
    """

    speaker_id: str
    transport_state: TransportState | None = None
    transport_uri: str | None = None
    transport_title: str | None = None
    actions: frozenset[TransportAction] | None = None
    shuffle: bool | None = None
    queue_position: int | None = None
    queue_length: int | None = None
    track: Track | None | Unreported = UNREPORTED


@dataclass(frozen=True)
class VolumeChanged:
    """A RenderingControl event: a room's volume or mute. Partial."""

    speaker_id: str
    volume: int | None = None
    muted: bool | None = None


@dataclass(frozen=True)
class GroupVolumeChanged:
    """A GroupRenderingControl event, of the device that sent it. Partial."""

    speaker_id: str
    volume: int | None = None
    muted: bool | None = None


@dataclass(frozen=True)
class HouseholdChanged:
    """A ZoneGroupTopology event: the household's layout as it is now."""

    speaker_id: str
    household: Household


SonosEvent = PlaybackChanged | VolumeChanged | GroupVolumeChanged | HouseholdChanged


class SonosSubscription(ABC):
    """One subscription to one service of one room's device."""

    @abstractmethod
    def renew(self) -> None:
        """Renew the subscription at the speaker now.

        Raises:
            ExternalServiceException: The speaker refused or did not answer;
                the subscription is then over.
        """

    @abstractmethod
    def unsubscribe(self) -> None:
        """End the subscription. Its handlers are not called after this returns."""

    @property
    @abstractmethod
    def lease_seconds(self) -> int:
        """How long the speaker holds the subscription without a renewal, as
        it granted it."""


class SonosClient(ABC):
    """What the backend asks of the speakers.

    Every method that reaches a speaker raises ``ExternalServiceException``
    when the speaker fails, does not answer, or is not in the household. Its
    message names no address: a ``502`` carries it to the API's clients.
    Playback is a group's: a transport read or command for a room goes to the
    coordinator of the room's group.
    """

    def __init__(self, health_service: HealthService) -> None:
        # Not critical: the app serves favourites without the speakers.
        health_service.register_check("sonos", self.check_readiness, critical=False)

    @abstractmethod
    def check_readiness(self) -> CheckResult:
        """Readiness check: one speaker of the household answers."""

    @abstractmethod
    def read_household(self) -> Household:
        """The household's layout, from any speaker that answers."""

    @abstractmethod
    def read_model(self, speaker_id: str) -> str:
        """The room's model, such as ``Play:3``."""

    @abstractmethod
    def read_playback(self, speaker_id: str) -> Playback:
        """The transport of the room's group."""

    @abstractmethod
    def read_volume(self, speaker_id: str) -> Volume:
        """The room's volume and mute."""

    @abstractmethod
    def read_group_volume(self, speaker_id: str) -> Volume:
        """The volume and mute of the room's group."""

    @abstractmethod
    def play(self, speaker_id: str) -> None:
        """Play or resume."""

    @abstractmethod
    def pause(self, speaker_id: str) -> None:
        """Pause."""

    @abstractmethod
    def stop(self, speaker_id: str) -> None:
        """Stop."""

    @abstractmethod
    def next(self, speaker_id: str) -> None:
        """Skip to the next track."""

    @abstractmethod
    def previous(self, speaker_id: str) -> None:
        """Go back to the previous track."""

    @abstractmethod
    def set_volume(self, speaker_id: str, volume: int) -> None:
        """Set the room's volume, 0 to 100."""

    @abstractmethod
    def change_volume(self, speaker_id: str, delta: int) -> int:
        """Move the room's volume by the speaker's own relative step.

        Returns:
            The new volume; the speaker keeps it within 0 to 100.
        """

    @abstractmethod
    def set_muted(self, speaker_id: str, muted: bool) -> None:
        """Mute or unmute the room."""

    @abstractmethod
    def set_group_volume(self, speaker_id: str, volume: int) -> None:
        """Set the volume of the room's group, 0 to 100."""

    @abstractmethod
    def change_group_volume(self, speaker_id: str, delta: int) -> int:
        """Move the group's volume by the speaker's own relative step.

        Returns:
            The new group volume; the speaker keeps it within 0 to 100.
        """

    @abstractmethod
    def set_group_muted(self, speaker_id: str, muted: bool) -> None:
        """Mute or unmute every room of the room's group."""

    @abstractmethod
    def read_picture(self, speaker_id: str, picture: str) -> bytes:
        """The picture at a ``Track.picture`` of the room's group, as served."""

    @abstractmethod
    def subscribe(
        self,
        speaker_id: str,
        service: SonosService,
        on_event: Callable[[SonosEvent], None],
        on_lost: Callable[[], None],
    ) -> SonosSubscription:
        """Subscribe to one service of the room's own device.

        The speaker sends the service's state in full at once, then what
        changes. The client renews the subscription until ``unsubscribe()``.

        Args:
            on_event: Called with each event, on the client's one delivery
                thread, never on the caller's.
            on_lost: Called on that thread when a renewal failed. The
                subscription is then over.
        """


# The SoCo attribute of each service.
_SOCO_SERVICES = {
    SonosService.AV_TRANSPORT: "avTransport",
    SonosService.RENDERING_CONTROL: "renderingControl",
    SonosService.GROUP_RENDERING_CONTROL: "groupRenderingControl",
    SonosService.ZONE_GROUP_TOPOLOGY: "zoneGroupTopology",
}

_REQUEST_TIMEOUT_SECONDS = 5.0
# The lease asked for a subscription; SoCo renews it at 85%. A subscription
# whose callback stopped answering (a killed process, a stopped environment)
# holds up the speaker's notifications to every subscriber, seconds to tens of
# seconds on and off, until its lease runs out: 24 hours when none is asked.
# Once a delivery to it has failed, the speaker refuses its UNSUBSCRIBE with
# 412, so only the lease ends it.
_SUBSCRIPTION_LEASE_SECONDS = 300
_SEED_TIMEOUT_SECONDS = 2.0
_READINESS_TIMEOUT_SECONDS = 1.0

_INSTANCE = [("InstanceID", 0)]
_DURATION = re.compile(r"(\d+):(\d\d):(\d\d)")


class _HouseholdError(Exception):
    """No seed answers, or the household has no such room."""


_SPEAKER_ERRORS = (
    SoCoException,
    requests.RequestException,
    OSError,
    ParseError,
    _HouseholdError,
)


@dataclass(frozen=True)
class _Lost:
    """A subscription whose renewal failed, queued for the delivery thread."""

    sid: str
    error: Exception


@dataclass(frozen=True)
class _Subscribed:
    device: Any
    speaker_id: str
    service: SonosService
    on_event: Callable[[SonosEvent], None]
    on_lost: Callable[[], None]


class _SoCoSubscription(SonosSubscription):
    def __init__(
        self, client: "SoCoSonosClient", subscription: Any, described: str
    ) -> None:
        self._client = client
        self._subscription = subscription
        self._described = described

    def renew(self) -> None:
        with _speaker_call(f"renew the subscription to {self._described}"):
            self._subscription.renew()

    def unsubscribe(self) -> None:
        self._client._forget(self._subscription.sid)
        with _speaker_call(f"end the subscription to {self._described}"):
            self._subscription.unsubscribe()

    @property
    def lease_seconds(self) -> int:
        return int(self._subscription.timeout)


class SoCoSonosClient(SonosClient):
    """The speakers, through SoCo.

    SoCo's configuration and its event listener are the process's: one
    instance of this class serves a process.
    """

    def __init__(self, settings: AppSettings, health_service: HealthService) -> None:
        super().__init__(health_service)
        self._seed_hosts = settings.sonos_seed_hosts
        self._event_port = settings.sonos_event_port
        # The device that answered last: asked first, before the seeds.
        self._entry: Any = None
        self._lock = threading.Lock()
        self._subscriptions: dict[str, _Subscribed] = {}
        self._events: queue.Queue[Any] = queue.Queue()
        self._delivery: threading.Thread | None = None
        soco.config.REQUEST_TIMEOUT = _REQUEST_TIMEOUT_SECONDS
        soco.config.EVENT_ADVERTISE_IP = settings.sonos_event_advertise_host

    def check_readiness(self) -> CheckResult:
        for device in self._candidates():
            try:
                _probe(device, _READINESS_TIMEOUT_SECONDS)
            except _SPEAKER_ERRORS:
                continue
            return CheckResult(CheckStatus.OK)
        return CheckResult(
            CheckStatus.ERROR,
            f"none of the {len(self._seed_hosts)} seed speakers answered",
        )

    def read_household(self) -> Household:
        with _speaker_call("read the household's layout"):
            return _household(self._any_device())

    def read_model(self, speaker_id: str) -> str:
        with _speaker_call(f"read the model of speaker {speaker_id}"):
            info = self._room(speaker_id).get_speaker_info(
                timeout=_REQUEST_TIMEOUT_SECONDS
            )
            return str(info["model_name"]).removeprefix("Sonos ")

    def read_playback(self, speaker_id: str) -> Playback:
        with _speaker_call(f"read the playback of speaker {speaker_id}"):
            transport = self._coordinator(speaker_id).avTransport
            state = transport.GetTransportInfo(_INSTANCE)
            media = transport.GetMediaInfo(_INSTANCE)
            position = transport.GetPositionInfo(
                [("InstanceID", 0), ("Channel", "Master")]
            )
            actions = transport.GetCurrentTransportActions(_INSTANCE)
            mode = transport.GetTransportSettings(_INSTANCE)
            return Playback(
                transport_state=TransportState(state["CurrentTransportState"]),
                transport_uri=media["CurrentURI"],
                transport_title=_title(_didl(media["CurrentURIMetaData"])),
                actions=_actions(actions["Actions"]),
                shuffle=_shuffle(mode["PlayMode"]),
                queue_position=int(position["Track"]),
                queue_length=int(media["NrTracks"]),
                track=_track(
                    _didl(position["TrackMetaData"]),
                    position["TrackURI"],
                    position["TrackDuration"],
                ),
            )

    def read_volume(self, speaker_id: str) -> Volume:
        with _speaker_call(f"read the volume of speaker {speaker_id}"):
            room = self._room(speaker_id)
            return Volume(volume=int(room.volume), muted=bool(room.mute))

    def read_group_volume(self, speaker_id: str) -> Volume:
        with _speaker_call(f"read the group volume of speaker {speaker_id}"):
            group = self._group(speaker_id)
            return Volume(volume=int(group.volume), muted=bool(group.mute))

    def play(self, speaker_id: str) -> None:
        with _speaker_call(f"play on speaker {speaker_id}"):
            self._coordinator(speaker_id).play()

    def pause(self, speaker_id: str) -> None:
        with _speaker_call(f"pause speaker {speaker_id}"):
            self._coordinator(speaker_id).pause()

    def stop(self, speaker_id: str) -> None:
        with _speaker_call(f"stop speaker {speaker_id}"):
            self._coordinator(speaker_id).stop()

    def next(self, speaker_id: str) -> None:
        with _speaker_call(f"skip to the next track on speaker {speaker_id}"):
            self._coordinator(speaker_id).next()

    def previous(self, speaker_id: str) -> None:
        with _speaker_call(f"go back to the previous track on speaker {speaker_id}"):
            self._coordinator(speaker_id).previous()

    def set_volume(self, speaker_id: str, volume: int) -> None:
        with _speaker_call(f"set the volume of speaker {speaker_id}"):
            self._room(speaker_id).volume = volume

    def change_volume(self, speaker_id: str, delta: int) -> int:
        with _speaker_call(f"change the volume of speaker {speaker_id}"):
            return int(self._room(speaker_id).set_relative_volume(delta))

    def set_muted(self, speaker_id: str, muted: bool) -> None:
        with _speaker_call(f"mute or unmute speaker {speaker_id}"):
            self._room(speaker_id).mute = muted

    def set_group_volume(self, speaker_id: str, volume: int) -> None:
        with _speaker_call(f"set the group volume of speaker {speaker_id}"):
            self._group(speaker_id).volume = volume

    def change_group_volume(self, speaker_id: str, delta: int) -> int:
        with _speaker_call(f"change the group volume of speaker {speaker_id}"):
            return int(self._group(speaker_id).set_relative_volume(delta))

    def set_group_muted(self, speaker_id: str, muted: bool) -> None:
        with _speaker_call(f"mute or unmute the group of speaker {speaker_id}"):
            self._group(speaker_id).mute = muted

    def read_picture(self, speaker_id: str, picture: str) -> bytes:
        with _speaker_call(f"read the picture speaker {speaker_id} serves"):
            coordinator = self._coordinator(speaker_id)
            # A speaker names its own picture by path (/getaa?…).
            url = (
                f"http://{coordinator.ip_address}:1400{picture}"
                if picture.startswith("/")
                else picture
            )
            response = requests.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.content

    def subscribe(
        self,
        speaker_id: str,
        service: SonosService,
        on_event: Callable[[SonosEvent], None],
        on_lost: Callable[[], None],
    ) -> SonosSubscription:
        described = f"{service} events of speaker {speaker_id}"
        with _speaker_call(f"subscribe to {described}"):
            device = self._room(speaker_id)
            self._listen(device)
            # Held across SUBSCRIBE: the speaker's first event is queued before
            # subscribe() returns, and the delivery thread must find the
            # subscription when it takes that event.
            with self._lock:
                subscription = getattr(device, _SOCO_SERVICES[service]).subscribe(
                    requested_timeout=_SUBSCRIPTION_LEASE_SECONDS,
                    auto_renew=True,
                    event_queue=self._events,
                )
                sid = subscription.sid
                self._subscriptions[sid] = _Subscribed(
                    device, speaker_id, service, on_event, on_lost
                )
            subscription.auto_renew_fail = lambda error: self._events.put(
                _Lost(sid, error)
            )
            return _SoCoSubscription(self, subscription, described)

    def _forget(self, sid: str) -> None:
        with self._lock:
            self._subscriptions.pop(sid, None)

    def _listen(self, device: Any) -> None:
        """Start SoCo's event listener and this client's delivery thread."""
        listener = soco.events.event_listener
        with self._lock:
            if not listener.is_running:
                # SoCo stops its listener with the last subscription and keeps
                # the stopped server's socket bound until it starts the next
                # one, which then finds the port in use.
                stopped = listener._listener_thread
                if stopped is not None:
                    stopped.server.server_close()
                listener.requested_port_number = self._event_port
                listener.start(device)
            if not listener.is_running:
                raise ConfigurationError(
                    "Cannot listen for speaker events: no local address reaches "
                    f"the speaker at {device.ip_address}"
                )
            # SoCo takes the next free port when the requested one is in use,
            # and advertises the port it bound: the speakers would call a port
            # nothing forwards.
            bound = listener.address[1]
            if bound != self._event_port:
                listener.stop()
                raise ConfigurationError(
                    f"Cannot listen for speaker events on port {self._event_port}: "
                    f"it is in use (SoCo bound {bound}). Stop the process that "
                    "holds it."
                )
            if self._delivery is None:
                self._delivery = threading.Thread(
                    target=self._deliver, name="sonos-events", daemon=True
                )
                self._delivery.start()

    def _deliver(self) -> None:
        while True:
            item = self._events.get()
            with self._lock:
                subscribed = self._subscriptions.get(item.sid)
                if subscribed is not None and isinstance(item, _Lost):
                    del self._subscriptions[item.sid]
            if subscribed is None:
                continue
            # One handler's failure must not end delivery to the others.
            try:
                if isinstance(item, _Lost):
                    logger.warning(
                        "The subscription to %s events of speaker %s was lost: %s",
                        subscribed.service,
                        subscribed.speaker_id,
                        item.error,
                    )
                    subscribed.on_lost()
                    continue
                event = _event(subscribed, item.variables)
                if event is not None:
                    subscribed.on_event(event)
            except Exception:
                logger.exception(
                    "Cannot deliver a %s event of speaker %s",
                    subscribed.service,
                    subscribed.speaker_id,
                )

    def _candidates(self) -> list[Any]:
        # SoCo keeps one instance per address.
        seeds = [SoCo(host) for host in self._seed_hosts]
        if self._entry is None:
            return seeds
        return [self._entry, *(seed for seed in seeds if seed is not self._entry)]

    def _any_device(self) -> Any:
        """A device that has just given SoCo the household's layout: any that
        answers."""
        error: Exception | None = None
        for device in self._candidates():
            try:
                _read_layout(device)
            except _SPEAKER_ERRORS as exc:
                error = exc
                continue
            self._entry = device
            return device
        raise _HouseholdError(
            f"none of the {len(self._seed_hosts)} seed speakers answered"
            + (f" ({_cause(error)})" if error is not None else "")
        ) from error

    def _room(self, speaker_id: str) -> Any:
        for zone in self._any_device().visible_zones:
            if zone.uid == speaker_id:
                return zone
        raise _HouseholdError(f"speaker {speaker_id} is not in the household")

    def _group(self, speaker_id: str) -> Any:
        return self._room(speaker_id).group

    def _coordinator(self, speaker_id: str) -> Any:
        return self._group(speaker_id).coordinator


def _probe(device: Any, timeout: float) -> None:
    """One cheap call; raises when the device does not answer in time.

    The arguments are given: for a call without them SoCo first fetches the
    service's description, with a timeout of 10 seconds that nothing sets.
    """
    device.deviceProperties.GetZoneInfo([], timeout=timeout)


def _read_layout(device: Any) -> None:
    """Ask the device for the household's layout and hand it to SoCo.

    SoCo's own poll asks no speaker while it holds a ZoneGroupTopology
    subscription, nor within 5 seconds of its last poll: it answers from the
    layout it took last. The arguments are given as in ``_probe()``.
    """
    response = device.zoneGroupTopology.GetZoneGroupState(
        [], timeout=_SEED_TIMEOUT_SECONDS
    )
    device.zone_group_state.process_payload(
        payload=response["ZoneGroupState"],
        source="poll",
        source_ip=device.ip_address,
    )


@contextmanager
def _speaker_call(operation: str) -> Iterator[None]:
    """Turn a speaker's failure into an ExternalServiceException (BE-3)."""
    try:
        yield
    except _SPEAKER_ERRORS as exc:
        raise ExternalServiceException(operation, _cause(exc)) from exc


def _cause(exc: Exception) -> str:
    """What went wrong, in words of this module's own: SoCo's and requests'
    messages name the speaker's address."""
    if isinstance(exc, _HouseholdError):
        return str(exc)
    if isinstance(exc, SoCoUPnPException):
        description = f" ({exc.error_description})" if exc.error_description else ""
        return f"the speaker answered UPnP error {exc.error_code}{description}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"the speaker answered {exc.response.status_code}"
    if isinstance(exc, (requests.RequestException, OSError)):
        return f"the speaker did not answer ({type(exc).__name__})"
    return f"the speaker gave an answer the backend cannot use ({type(exc).__name__})"


def _household(device: Any) -> Household:
    visible = device.visible_zones
    rooms = [
        Room(
            id=zone.uid,
            name=zone.player_name,
            address=zone.ip_address,
            boot_seq=int(zone.boot_seqnum),
        )
        for zone in visible
    ]
    groups = [
        Group(
            coordinator_id=group.coordinator.uid,
            member_ids=tuple(
                sorted(member.uid for member in group.members if member in visible)
            ),
        )
        for group in device.all_groups
        if group.coordinator in visible
    ]
    return Household(
        rooms=tuple(sorted(rooms, key=lambda room: room.id)),
        groups=tuple(sorted(groups, key=lambda group: group.coordinator_id)),
    )


def _didl(metadata: str) -> DidlObject | None:
    """The object of a metadata document; None where the speaker gives none."""
    if not metadata.startswith("<DIDL-Lite"):
        return None
    didl: DidlObject = from_didl_string(metadata)[0]
    return didl


def _title(metadata: DidlObject | None) -> str:
    return "" if metadata is None else str(metadata.to_dict().get("title") or "")


def _track(metadata: DidlObject | None, uri: str, duration: str) -> Track | None:
    if metadata is None:
        return None
    fields = metadata.to_dict()
    return Track(
        title=str(fields.get("title") or ""),
        artist=str(fields.get("creator") or ""),
        album=str(fields.get("album") or ""),
        stream_content=str(fields.get("stream_content") or ""),
        duration_seconds=_seconds(duration),
        uri=uri,
        picture=str(fields.get("album_art_uri") or ""),
    )


def _seconds(duration: str) -> int:
    """A speaker's H:MM:SS; 0 where it gives none (``NOT_IMPLEMENTED``)."""
    match = _DURATION.fullmatch(duration)
    if match is None:
        return 0
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _actions(actions: str) -> frozenset[TransportAction]:
    """The commands among a speaker's ``Set, Stop, Pause, Play, X_DLNA_SeekTime``."""
    known = {action.value: action for action in TransportAction}
    names = (name.strip() for name in actions.split(","))
    return frozenset(known[name] for name in names if name in known)


def _shuffle(play_mode: str) -> bool:
    return bool(PLAY_MODES[play_mode][0])


def _event(subscribed: _Subscribed, variables: dict[str, Any]) -> SonosEvent | None:
    """The event a notification's variables make; None when they carry nothing
    this client reports."""
    speaker_id = subscribed.speaker_id
    if subscribed.service is SonosService.AV_TRANSPORT:
        return _playback_changed(speaker_id, variables)
    if subscribed.service is SonosService.RENDERING_CONTROL:
        volume = variables.get("volume")
        mute = variables.get("mute")
        if volume is None and mute is None:
            return None
        return VolumeChanged(
            speaker_id,
            volume=None if volume is None else int(volume["Master"]),
            muted=None if mute is None else mute["Master"] == "1",
        )
    if subscribed.service is SonosService.GROUP_RENDERING_CONTROL:
        volume = variables.get("group_volume")
        mute = variables.get("group_mute")
        if volume is None and mute is None:
            return None
        return GroupVolumeChanged(
            speaker_id,
            volume=None if volume is None else int(volume),
            muted=None if mute is None else mute == "1",
        )
    if "zone_group_state" not in variables:
        return None
    # SoCo has taken the event's layout into the household's state already.
    return HouseholdChanged(speaker_id, _household(subscribed.device))


def _playback_changed(
    speaker_id: str, variables: dict[str, Any]
) -> PlaybackChanged | None:
    state = variables.get("transport_state")
    actions = variables.get("current_transport_actions")
    play_mode = variables.get("current_play_mode")
    position = variables.get("current_track")
    length = variables.get("number_of_tracks")
    transport_metadata = variables.get("av_transport_uri_meta_data")
    track: Track | None | Unreported = UNREPORTED
    if "current_track_meta_data" in variables:
        track = _track(
            _event_didl(variables["current_track_meta_data"]),
            variables.get("current_track_uri", ""),
            variables.get("current_track_duration", ""),
        )
    event = PlaybackChanged(
        speaker_id,
        transport_state=None if state is None else TransportState(state),
        transport_uri=variables.get("av_transport_uri"),
        transport_title=(
            None
            if transport_metadata is None
            else _title(_event_didl(transport_metadata))
        ),
        actions=None if actions is None else _actions(actions),
        shuffle=None if play_mode is None else _shuffle(play_mode),
        queue_position=None if position is None else int(position),
        queue_length=None if length is None else int(length),
        track=track,
    )
    return None if event == PlaybackChanged(speaker_id) else event


def _event_didl(value: Any) -> DidlObject | None:
    """An event's metadata: SoCo parsed it, or left the empty text."""
    if isinstance(value, DidlObject):
        return value
    if value == "":
        return None
    raise ValueError(f"The speaker sent metadata SoCo cannot read: {value!r}")
