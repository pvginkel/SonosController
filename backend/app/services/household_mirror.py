"""The household mirror: the speakers' state in memory, fed by their events.

``docs/api-design.md`` section 2: "The backend mirrors, and does not own, the
speakers' state." One instance subscribes to every room's events, reads the
room in full when it does, and merges each event into what it holds. The API
reads rooms and playback from it without asking a speaker, and it sends
section 4's ``speakers_changed``, ``volume_changed`` and ``playback_changed``
when what it holds changes. Nothing of it is stored.

Its subscriptions live in this process, which is why the app runs as one
replica.

Playback and the group volume are a group's: the mirror takes them from the
events of the group's coordinator and serves them for every member. Every
string it serves is Unicode NFC.
"""

import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from app.core_exceptions import ExternalServiceException
from app.exceptions import SpeakerNotFoundException
from app.schemas.stream_event_schema import (
    PlaybackChangedEvent,
    SpeakerPayload,
    SpeakersChangedEvent,
    StreamEvent,
    VolumeChangedEvent,
)
from app.services.event_sender import EventSender
from app.services.sonos_client import (
    UNREPORTED,
    Group,
    GroupVolumeChanged,
    Household,
    HouseholdChanged,
    Playback,
    PlaybackChanged,
    Room,
    SonosClient,
    SonosEvent,
    SonosService,
    SonosSubscription,
    TransportAction,
    TransportState,
    Volume,
    VolumeChanged,
)
from app.utils.lifecycle_coordinator import LifecycleCoordinatorProtocol, LifecycleEvent


class PlaybackStatus(StrEnum):
    """What a transport is doing."""

    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    BUFFERING = "buffering"


class PlaybackSource(StrEnum):
    """Where what plays comes from, by the transport's address."""

    QUEUE = "queue"
    RADIO = "radio"
    LINE_IN = "line_in"
    TV = "tv"
    OTHER = "other"


class PlaybackAction(StrEnum):
    """A playback command of the API."""

    PLAY = "play"
    PAUSE = "pause"
    NEXT = "next"
    PREVIOUS = "previous"


@dataclass(frozen=True)
class SpeakerGroup:
    """The rooms a room plays together with, itself included."""

    coordinator_id: str
    member_ids: tuple[str, ...]
    volume: int
    muted: bool


@dataclass(frozen=True)
class Speaker:
    """A room, with its own volume and its group."""

    id: str
    name: str
    model: str
    volume: int
    muted: bool
    group: SpeakerGroup


@dataclass(frozen=True)
class PlayingTrack:
    """The track a group has loaded.

    ``album`` and ``duration_ms`` are None where the source gives none, as a
    radio stream does; ``track_id`` is the Plex rating key of a track queued
    from Plex, else None.
    """

    title: str
    artist: str
    album: str | None
    duration_ms: int | None
    track_id: str | None


@dataclass(frozen=True)
class SpeakerPlayback:
    """What a room's group plays, and what can be done with it.

    ``actions`` are in the order of ``PlaybackAction``. ``queue_position``,
    1-based, and ``queue_length`` are None when the source is not the queue;
    ``queue_position`` is None for an empty queue too. ``track`` is None when
    nothing is loaded.
    """

    state: PlaybackStatus
    source: PlaybackSource
    actions: tuple[PlaybackAction, ...]
    shuffle: bool
    queue_position: int | None
    queue_length: int | None
    track: PlayingTrack | None


_STATES = {
    TransportState.PLAYING: PlaybackStatus.PLAYING,
    TransportState.PAUSED: PlaybackStatus.PAUSED,
    TransportState.STOPPED: PlaybackStatus.STOPPED,
    TransportState.TRANSITIONING: PlaybackStatus.BUFFERING,
}

# The transport addresses of each source, by how they start. The radio ones
# are those SoCo's music_source_from_uri() names as radio.
_SOURCES = (
    ("x-rincon-queue:", PlaybackSource.QUEUE),
    ("x-rincon-mp3radio:", PlaybackSource.RADIO),
    ("x-sonosapi-stream:", PlaybackSource.RADIO),
    ("x-sonosapi-radio:", PlaybackSource.RADIO),
    ("x-sonosapi-hls:", PlaybackSource.RADIO),
    ("x-sonos-http:sonos", PlaybackSource.RADIO),
    ("aac:", PlaybackSource.RADIO),
    ("hls-radio:", PlaybackSource.RADIO),
    ("x-rincon-stream:", PlaybackSource.LINE_IN),
    ("x-sonos-htastream:", PlaybackSource.TV),
)

# The speaker's commands that make each action of the API available. The API
# has no stop: its pause stops what the speaker cannot pause, a radio stream
# (docs/api-design.md section 1.7).
_ACTIONS = (
    (PlaybackAction.PLAY, {TransportAction.PLAY}),
    (PlaybackAction.PAUSE, {TransportAction.PAUSE, TransportAction.STOP}),
    (PlaybackAction.NEXT, {TransportAction.NEXT}),
    (PlaybackAction.PREVIOUS, {TransportAction.PREVIOUS}),
)

# A queue entry from the Plex service names its track:
# x-sonos-http:<machine>%3a<section>%3a<rating key>%3atrack.mp3?…
# (docs/api-design.md section 1.3).
_PLEX_TRACK = re.compile(
    r"x-sonos-http:[^%?]+%3a[^%?]+%3a(\d+)%3atrack\.", re.IGNORECASE
)

_PLAYBACK_FIELDS = (
    "transport_state",
    "transport_uri",
    "transport_title",
    "actions",
    "shuffle",
    "queue_position",
    "queue_length",
)

_GroupEvent = VolumeChanged | GroupVolumeChanged | PlaybackChanged


@dataclass
class _Feed:
    """A room's subscriptions.

    ``early`` holds the room's events that arrived before it was read in
    full, and is None once they are merged into what was read.
    """

    subscriptions: list[SonosSubscription] = field(default_factory=list)
    early: list[_GroupEvent] | None = field(default_factory=list)


@dataclass
class _RoomState:
    """What the mirror holds of a room.

    ``group_volume`` and ``playback`` are those of the room's group as its
    own device reports them: current while the room coordinates its group,
    and not served while it follows another room.
    """

    model: str
    volume: Volume
    group_volume: Volume
    playback: Playback


class HouseholdMirror:
    """The household in memory."""

    def __init__(
        self,
        sonos_client: SonosClient,
        event_sender: EventSender,
        lifecycle_coordinator: LifecycleCoordinatorProtocol,
    ) -> None:
        self._client = sonos_client
        self._events = event_sender
        # Held while a layout is applied, across its calls to the speakers:
        # layouts are applied one after the other.
        self._layout_lock = threading.Lock()
        # Guards everything below. Never held across a call to a speaker, and
        # taken after _layout_lock where both are held.
        self._lock = threading.Lock()
        self._stopped = False
        self._household = Household(rooms=(), groups=())
        # Every room of _household: its group, and what is held of it.
        self._groups: dict[str, Group] = {}
        self._states: dict[str, _RoomState] = {}
        # The rooms of _states, and those being read in full.
        self._feeds: dict[str, _Feed] = {}
        lifecycle_coordinator.register_lifecycle_notification(self._on_lifecycle_event)

    def start(self) -> None:
        """Mirror the household: subscribe to every room and read it in full.

        Raises:
            ExternalServiceException: No speaker answers, or a room failed.
                Nothing new is mirrored then.
        """
        self._apply_layout(self._client.read_household())

    def stop(self) -> None:
        """End every subscription. The mirror is empty afterwards.

        Raises:
            ExternalServiceException: A speaker failed to end a subscription;
                the others are ended all the same.
        """
        with self._layout_lock, self._lock:
            self._stopped = True
            feeds = list(self._feeds.values())
            self._feeds = {}
            self._states = {}
            self._groups = {}
            self._household = Household(rooms=(), groups=())
        self._unsubscribe(feeds)

    def speakers(self) -> list[Speaker]:
        """Every room of the household, by name."""
        with self._lock:
            speakers = [self._speaker(room) for room in self._household.rooms]
        return sorted(speakers, key=lambda speaker: (speaker.name.casefold(), speaker.id))

    def speaker(self, speaker_id: str) -> Speaker:
        """One room.

        Raises:
            SpeakerNotFoundException: The household has no such room.
        """
        with self._lock:
            return self._speaker(self._room(speaker_id))

    def playback(self, speaker_id: str) -> SpeakerPlayback:
        """What the room's group plays.

        Raises:
            SpeakerNotFoundException: The household has no such room.
        """
        with self._lock:
            group = self._groups[self._room(speaker_id).id]
            return _playback(self._states[group.coordinator_id].playback)

    def _room(self, speaker_id: str) -> Room:
        for room in self._household.rooms:
            if room.id == speaker_id:
                return room
        raise SpeakerNotFoundException(speaker_id)

    def _speaker(self, room: Room) -> Speaker:
        state = self._states[room.id]
        group = self._groups[room.id]
        group_volume = self._states[group.coordinator_id].group_volume
        return Speaker(
            id=room.id,
            name=_nfc(room.name),
            model=_nfc(state.model),
            volume=state.volume.volume,
            muted=state.volume.muted,
            group=SpeakerGroup(
                coordinator_id=group.coordinator_id,
                member_ids=group.member_ids,
                volume=group_volume.volume,
                muted=group_volume.muted,
            ),
        )

    def _apply_layout(self, household: Household) -> None:
        """Make the mirror's rooms and groups those of the layout.

        A room the mirror does not hold is subscribed to and read in full. A
        room that now coordinates a group and followed another room before is
        read again: while it followed, its own device's events were not the
        group's. A room the layout no longer has is dropped.
        """
        events: list[StreamEvent] = []
        # The feeds this layout ends. Their subscriptions are ended outside
        # the locks: a client may wait there for a handler that waits here.
        ended: list[_Feed] = []
        try:
            with self._layout_lock:
                if self._stopped:
                    return
                groups = {
                    member_id: group
                    for group in household.groups
                    for member_id in group.member_ids
                }
                appeared = [
                    room for room in household.rooms if room.id not in self._states
                ]
                promoted = [
                    room.id
                    for room in household.rooms
                    if room.id in self._states
                    and self._groups[room.id].coordinator_id != room.id
                    and groups[room.id].coordinator_id == room.id
                ]
                try:
                    read = {
                        room.id: self._subscribe_and_read(room) for room in appeared
                    }
                    read_again = {
                        room_id: (
                            self._client.read_playback(room_id),
                            self._client.read_group_volume(room_id),
                        )
                        for room_id in promoted
                    }
                except ExternalServiceException:
                    with self._lock:
                        ended = [
                            self._feeds.pop(room.id)
                            for room in appeared
                            if room.id in self._feeds
                        ]
                    raise

                with self._lock:
                    before = self._layout()
                    for room_id in self._states.keys() - groups.keys():
                        del self._states[room_id]
                        ended.append(self._feeds.pop(room_id))
                    self._household = household
                    self._groups = groups
                    for room_id, state in read.items():
                        self._states[room_id] = state
                        feed = self._feeds[room_id]
                        early, feed.early = feed.early or [], None
                        for event in early:
                            self._merge(room_id, event)
                    for room_id, (playback, group_volume) in read_again.items():
                        self._states[room_id].playback = playback
                        self._states[room_id].group_volume = group_volume
                    events = _layout_events(before, self._layout())
            for stream_event in events:
                self._events.send(stream_event)
        finally:
            self._unsubscribe(ended)

    def _layout(self) -> dict[str, tuple[str, Group]]:
        """Each room's name and group: what ``speakers_changed`` is about."""
        return {
            room.id: (room.name, self._groups[room.id]) for room in self._household.rooms
        }

    def _subscribe_and_read(self, room: Room) -> _RoomState:
        """Subscribe to the room's four services, then read it in full.

        In that order: what changes after the read arrives as an event, and
        the events that arrive before the read is held wait in the feed.
        """
        feed = _Feed()
        with self._lock:
            self._feeds[room.id] = feed
        for service in SonosService:
            feed.subscriptions.append(
                self._client.subscribe(
                    room.id, service, self._handler(room.id, feed), _subscription_lost
                )
            )
        return _RoomState(
            model=self._client.read_model(room.id),
            volume=self._client.read_volume(room.id),
            group_volume=self._client.read_group_volume(room.id),
            playback=self._client.read_playback(room.id),
        )

    def _unsubscribe(self, feeds: list[_Feed]) -> None:
        """End the feeds' subscriptions, every one, also when one fails."""
        failure: ExternalServiceException | None = None
        for feed in feeds:
            for subscription in feed.subscriptions:
                try:
                    subscription.unsubscribe()
                except ExternalServiceException as error:
                    failure = failure or error
        if failure is not None:
            raise failure

    def _handler(self, speaker_id: str, feed: _Feed) -> Callable[[SonosEvent], None]:
        """The handler of one subscription of the feed."""
        first = True

        def on_event(event: SonosEvent) -> None:
            nonlocal first
            if first:
                # A subscription starts with the service's state as the device
                # holds it, which can be one the room has left. The read that
                # follows the subscription is current, so it stands.
                first = False
                return
            self._on_event(speaker_id, feed, event)

        return on_event

    def _on_event(self, speaker_id: str, feed: _Feed, event: SonosEvent) -> None:
        if isinstance(event, HouseholdChanged):
            with self._lock:
                current = self._feeds.get(speaker_id) is feed
            if current:
                self._apply_layout(event.household)
            return
        with self._lock:
            if self._feeds.get(speaker_id) is not feed:
                return
            if feed.early is not None:
                feed.early.append(event)
                return
            events = self._merge(speaker_id, event)
        for stream_event in events:
            self._events.send(stream_event)

    def _merge(self, speaker_id: str, event: _GroupEvent) -> list[StreamEvent]:
        """Merge an event into what is held of the room.

        Returns:
            The events to send: none when the event changed nothing.
        """
        state = self._states[speaker_id]
        if isinstance(event, VolumeChanged):
            volume = _merged_volume(state.volume, event)
            if volume == state.volume:
                return []
            state.volume = volume
            return [VolumeChangedEvent(payload=SpeakerPayload(speaker_id=speaker_id))]

        group = self._groups[speaker_id]
        if group.coordinator_id != speaker_id:
            # A group's volume and playback are its coordinator's to report.
            return []
        if isinstance(event, GroupVolumeChanged):
            group_volume = _merged_volume(state.group_volume, event)
            if group_volume == state.group_volume:
                return []
            state.group_volume = group_volume
            return [
                VolumeChangedEvent(payload=SpeakerPayload(speaker_id=member_id))
                for member_id in group.member_ids
            ]
        playback = _merged_playback(state.playback, event)
        if playback == state.playback:
            return []
        state.playback = playback
        return [
            PlaybackChangedEvent(payload=SpeakerPayload(speaker_id=member_id))
            for member_id in group.member_ids
        ]

    def _on_lifecycle_event(self, event: LifecycleEvent) -> None:
        if event == LifecycleEvent.STARTUP:
            self.start()
        elif event == LifecycleEvent.SHUTDOWN:
            self.stop()


def _subscription_lost() -> None:
    """A subscription's renewal failed: its events have ended."""


def _layout_events(
    before: dict[str, tuple[str, Group]], after: dict[str, tuple[str, Group]]
) -> list[StreamEvent]:
    """The events of a change of layout.

    A room whose group changed has another group volume, and one whose
    coordinator changed plays something else.
    """
    if before == after:
        return []
    events: list[StreamEvent] = [SpeakersChangedEvent()]
    for room_id in sorted(before.keys() & after.keys()):
        group_before, group_after = before[room_id][1], after[room_id][1]
        if group_before != group_after:
            events.append(VolumeChangedEvent(payload=SpeakerPayload(speaker_id=room_id)))
        if group_before.coordinator_id != group_after.coordinator_id:
            events.append(
                PlaybackChangedEvent(payload=SpeakerPayload(speaker_id=room_id))
            )
    return events


def _merged_volume(held: Volume, event: VolumeChanged | GroupVolumeChanged) -> Volume:
    return Volume(
        volume=held.volume if event.volume is None else event.volume,
        muted=held.muted if event.muted is None else event.muted,
    )


def _merged_playback(held: Playback, event: PlaybackChanged) -> Playback:
    """What is held with the fields the event carries: events are partial."""
    reported: dict[str, Any] = {
        name: getattr(event, name)
        for name in _PLAYBACK_FIELDS
        if getattr(event, name) is not None
    }
    if event.track is not UNREPORTED:
        reported["track"] = event.track
    return replace(held, **reported)


def _playback(playback: Playback) -> SpeakerPlayback:
    source = _source(playback.transport_uri)
    queued = source is PlaybackSource.QUEUE
    return SpeakerPlayback(
        state=_STATES[playback.transport_state],
        source=source,
        actions=tuple(
            action for action, commands in _ACTIONS if commands & playback.actions
        ),
        shuffle=playback.shuffle,
        queue_position=(playback.queue_position or None) if queued else None,
        queue_length=playback.queue_length if queued else None,
        track=_track(playback, source),
    )


def _source(transport_uri: str) -> PlaybackSource:
    for start, source in _SOURCES:
        if transport_uri.startswith(start):
            return source
    return PlaybackSource.OTHER


def _track(playback: Playback, source: PlaybackSource) -> PlayingTrack | None:
    track = playback.track
    if track is None:
        return None
    title, artist = track.title, track.artist
    if source is PlaybackSource.RADIO:
        # A stream's own title is its file name. What it reports as playing
        # is the title, the station the artist (section 1.7); until it
        # reports something, the station is all there is to show.
        title = track.stream_content or playback.transport_title
        artist = playback.transport_title
    plex_track = _PLEX_TRACK.match(track.uri)
    return PlayingTrack(
        title=_nfc(title),
        artist=_nfc(artist),
        album=_nfc(track.album) or None,
        duration_ms=track.duration_seconds * 1000 or None,
        track_id=plex_track.group(1) if plex_track else None,
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
