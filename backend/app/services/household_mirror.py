"""The household mirror: the speakers' state in memory, fed by their events.

``docs/api-design.md`` section 2: "The backend mirrors, and does not own, the
speakers' state." One instance subscribes to every room's events, reads the
room in full when it does, and merges each event into what it holds. The API
reads rooms and playback from it without asking a speaker, and it sends
section 4's ``speakers_changed``, ``volume_changed`` and ``playback_changed``
when what it holds changes. Nothing of it is stored.

Its subscriptions live in this process, which is why the app runs as one
replica. They heal themselves: a room is subscribed to and read again after
a failed renewal, after its device restarted and after it took another
address. A layout the speakers send is applied as it arrives, and the mirror
reads the layout itself every ``CHECK_SECONDS``, which finds what no event
reported: a device that restarted without a word, a room that did not answer
before, a household that did not answer when the app started.

Playback and the group volume are a group's: the mirror takes them from the
events of the group's coordinator and serves them for every member. Every
string it serves is Unicode NFC.
"""

import logging
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

logger = logging.getLogger(__name__)

# The time between two reads of the household's layout.
CHECK_SECONDS = 10.0


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
    full, and is None once they are merged into what was read. ``stale`` is
    set when a renewal failed, or when the room could not be subscribed to
    again: the next layout that is applied replaces the feed. ``ended`` is
    set once the mirror no longer uses the feed: its handlers do nothing
    more.
    """

    subscriptions: list[SonosSubscription] = field(default_factory=list)
    early: list[_GroupEvent] | None = field(default_factory=list)
    stale: bool = False
    ended: bool = False


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


@dataclass(frozen=True)
class _Served:
    """What the mirror serves of a room: what its events are about."""

    name: str
    group: Group
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
        check_seconds: float = CHECK_SECONDS,
    ) -> None:
        self._client = sonos_client
        self._events = event_sender
        self._check_seconds = check_seconds
        # Held while a layout is applied, across its calls to the speakers:
        # layouts are applied one after the other.
        self._layout_lock = threading.Lock()
        # Guards everything below. Never held across a call to a speaker, and
        # taken after _layout_lock where both are held.
        self._lock = threading.Lock()
        self._stopped = False
        self._household = Household(rooms=(), groups=())
        # Every room of _household: its group, what is held of it, and the
        # feed that keeps it current.
        self._groups: dict[str, Group] = {}
        self._states: dict[str, _RoomState] = {}
        self._feeds: dict[str, _Feed] = {}
        # The healing thread reads the layout when it is woken with _due set,
        # and every _check_seconds.
        self._healer: threading.Thread | None = None
        self._wake = threading.Condition(self._lock)
        self._due = False
        # Why the last layout was not applied in full; None when it was.
        self._failure: str | None = None
        lifecycle_coordinator.register_lifecycle_notification(self._on_lifecycle_event)

    def start(self) -> None:
        """Mirror the household, and keep it mirrored until ``stop()``.

        Every room is subscribed to and read in full. A household that does
        not answer and a room that fails are logged, not raised: the mirror
        holds nothing new then, and the layout is read again every
        ``check_seconds`` until every room is mirrored.
        """
        self._heal()
        self._healer = threading.Thread(
            target=self._keep_healing, name="household-mirror", daemon=True
        )
        self._healer.start()

    def stop(self) -> None:
        """End every subscription. The mirror is empty afterwards.

        Raises:
            ExternalServiceException: A speaker failed to end a subscription;
                the others are ended all the same.
        """
        with self._layout_lock, self._lock:
            self._stopped = True
            feeds = list(self._feeds.values())
            for feed in feeds:
                feed.ended = True
            self._feeds = {}
            self._states = {}
            self._groups = {}
            self._household = Household(rooms=(), groups=())
            self._failure = None
            self._wake.notify_all()
        if self._healer is not None:
            self._healer.join()
        failure = self._end(feeds)
        if failure is not None:
            raise failure

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

    def _keep_healing(self) -> None:
        """The healing thread: read the layout when woken, and every
        ``check_seconds``."""
        while True:
            with self._lock:
                if not self._due:
                    self._wake.wait(self._check_seconds)
                if self._stopped:
                    return
                self._due = False
            self._heal()

    def _heal(self, household: Household | None = None) -> None:
        """Apply a layout: the one given, or the one a speaker gives now.

        What fails stays as it was and is logged: the healing thread's next
        read of the layout tries it again.
        """
        try:
            if household is None:
                household = self._client.read_household()
            self._apply_layout(household)
        except ExternalServiceException as error:
            self._report(str(error))
        else:
            self._report(None)

    def _report(self, failure: str | None) -> None:
        """Log that the household is not mirrored in full, or is again: once
        for each failure, not at every read of the layout."""
        with self._lock:
            before, self._failure = self._failure, failure
        if failure is not None and failure != before:
            logger.warning(
                "The household is not mirrored in full; the layout is read "
                "again every %s s: %s",
                self._check_seconds,
                failure,
            )
        elif failure is None and before is not None:
            logger.info("The household is mirrored in full again")

    def _apply_layout(self, household: Household) -> None:
        """Make the mirror's rooms and groups those of the layout.

        A room the mirror does not hold is subscribed to and read in full.
        So is a room whose feed is stale, or whose device has another address
        or boot sequence number than the mirror holds: its subscriptions are
        over, or their renewal fails, and what it reported since is unknown.
        A room that now coordinates a group and followed another room before
        is read again: while it followed, its own device's events were not
        the group's. A room the layout no longer has is dropped.

        Raises:
            ExternalServiceException: A room failed. A room that is new, or
                that now coordinates, leaves the layout unapplied. A held
                room that could not be subscribed to again keeps what is
                held of it, and its feed is stale.
        """
        events: list[StreamEvent] = []
        # The feeds this layout ends. Their subscriptions are ended outside
        # the locks: a client may wait there for a handler that waits here.
        ended: list[_Feed] = []
        failure: ExternalServiceException | None = None
        try:
            with self._layout_lock:
                if self._stopped:
                    return
                groups = {
                    member_id: group
                    for group in household.groups
                    for member_id in group.member_ids
                }
                with self._lock:
                    held = {room.id: room for room in self._household.rooms}
                    stale = {
                        room_id for room_id, feed in self._feeds.items() if feed.stale
                    }
                feeds: dict[str, _Feed] = {}
                read: dict[str, _RoomState] = {}
                # The held rooms that could not be subscribed to again.
                unanswered: list[str] = []
                try:
                    for room in household.rooms:
                        known = held.get(room.id)
                        if (
                            known is not None
                            and room.id not in stale
                            and (room.address, room.boot_seq)
                            == (known.address, known.boot_seq)
                        ):
                            continue
                        feed = _Feed()
                        try:
                            read[room.id] = self._subscribe_and_read(room, feed)
                        except ExternalServiceException as error:
                            ended.append(feed)
                            if known is None:
                                raise
                            unanswered.append(room.id)
                            failure = failure or error
                        else:
                            feeds[room.id] = feed
                    read_again = {
                        room.id: (
                            self._client.read_playback(room.id),
                            self._client.read_group_volume(room.id),
                        )
                        for room in household.rooms
                        if room.id in held
                        and room.id not in read
                        and self._groups[room.id].coordinator_id != room.id
                        and groups[room.id].coordinator_id == room.id
                    }
                except ExternalServiceException:
                    ended.extend(feeds.values())
                    with self._lock:
                        for feed in ended:
                            feed.ended = True
                    raise

                with self._lock:
                    before = self._served()
                    for room_id in self._states.keys() - groups.keys():
                        del self._states[room_id]
                        ended.append(self._feeds.pop(room_id))
                    self._household = household
                    self._groups = groups
                    for room_id in unanswered:
                        self._feeds[room_id].stale = True
                    for room_id, state in read.items():
                        if room_id in held:
                            ended.append(self._feeds[room_id])
                        self._states[room_id] = state
                        self._feeds[room_id] = feed = feeds[room_id]
                        early, feed.early = feed.early or [], None
                        for event in early:
                            self._merge(room_id, event)
                    for room_id, (playback, group_volume) in read_again.items():
                        self._states[room_id].playback = playback
                        self._states[room_id].group_volume = group_volume
                    for feed in ended:
                        feed.ended = True
                    events = _layout_events(before, self._served())
            for stream_event in events:
                self._events.send(stream_event)
        finally:
            unended = self._end(ended)
            if unended is not None:
                logger.warning(
                    "A subscription the mirror no longer uses was not ended; "
                    "the speaker ends it when it is not renewed: %s",
                    unended,
                )
        if failure is not None:
            raise failure

    def _served(self) -> dict[str, _Served]:
        served = {}
        for room in self._household.rooms:
            group = self._groups[room.id]
            coordinator = self._states[group.coordinator_id]
            served[room.id] = _Served(
                name=room.name,
                group=group,
                volume=self._states[room.id].volume,
                group_volume=coordinator.group_volume,
                playback=coordinator.playback,
            )
        return served

    def _subscribe_and_read(self, room: Room, feed: _Feed) -> _RoomState:
        """Subscribe the feed to the room's four services, then read the room
        in full.

        In that order: what changes after the read arrives as an event, and
        the events that arrive before the read is held wait in the feed.
        """
        for service in SonosService:
            feed.subscriptions.append(
                self._client.subscribe(
                    room.id, service, self._handler(room.id, feed), self._lost(feed)
                )
            )
        return _RoomState(
            model=self._client.read_model(room.id),
            volume=self._client.read_volume(room.id),
            group_volume=self._client.read_group_volume(room.id),
            playback=self._client.read_playback(room.id),
        )

    def _end(self, feeds: list[_Feed]) -> ExternalServiceException | None:
        """End the feeds' subscriptions, every one, also when one fails.

        Returns:
            The first failure, None when every subscription was ended.
        """
        failure: ExternalServiceException | None = None
        for feed in feeds:
            for subscription in feed.subscriptions:
                try:
                    subscription.unsubscribe()
                except ExternalServiceException as error:
                    failure = failure or error
        return failure

    def _lost(self, feed: _Feed) -> Callable[[], None]:
        """What a subscription of the feed calls when its renewal failed."""

        def on_lost() -> None:
            with self._lock:
                if feed.ended:
                    return
                feed.stale = True
                self._due = True
                self._wake.notify()

        return on_lost

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
                ended = feed.ended
            if not ended:
                self._heal(event.household)
            return
        with self._lock:
            if feed.ended:
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


def _layout_events(
    before: dict[str, _Served], after: dict[str, _Served]
) -> list[StreamEvent]:
    """The events of a layout that was applied.

    A room whose group changed has another group volume, and one whose
    coordinator changed plays something else. A room that was read again has
    the events of what the read changed.
    """
    events: list[StreamEvent] = []
    if {room_id: (room.name, room.group) for room_id, room in before.items()} != {
        room_id: (room.name, room.group) for room_id, room in after.items()
    }:
        events.append(SpeakersChangedEvent())
    for room_id in sorted(before.keys() & after.keys()):
        was, now = before[room_id], after[room_id]
        if (was.group, was.volume, was.group_volume) != (
            now.group,
            now.volume,
            now.group_volume,
        ):
            events.append(VolumeChangedEvent(payload=SpeakerPayload(speaker_id=room_id)))
        if (
            was.group.coordinator_id != now.group.coordinator_id
            or was.playback != now.playback
        ):
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
