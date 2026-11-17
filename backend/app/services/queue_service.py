"""The queue of a speaker's group (``docs/api-design.md`` section 3.4).

The queue lives on the speaker: every call reads or changes the queue of the
group's coordinator through the Sonos client, and nothing of it is kept here.
Whether the speaker exists, and where in its queue the group is, is read from
the household mirror.

An entry is a track of the Plex library when its address carries the machine
identifier of the configured Plex server: it gets its rating key and Plex's
picture, and every other entry neither. Every string is Unicode NFC.

An item of the library is queued by a task, as containers of the Plex
service linked in Sonos (sections 1.3 to 1.5). A group's queueing tasks and
its Clears take their turns in its lane (``QueueingLanes``).
"""

import logging
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from app.core_exceptions import ExternalServiceException
from app.exceptions import (
    InvalidOperationException,
    ItemNotFoundException,
    PlaylistNotPlayableException,
)
from app.schemas.queue_schema import (
    QueueAction,
    QueueItemKind,
    QueueListQuerySchema,
    QueueRequestSchema,
    QueueTaskResultSchema,
)
from app.schemas.task_schema import TaskStartResponse
from app.services.base_task import BaseTask, ProgressHandle
from app.services.household_mirror import HouseholdMirror
from app.services.plex_client import PlexClient
from app.services.queueing_lanes import QueueingLanes
from app.services.sonos_client import (
    ADD_MANY_LIMIT,
    PlexItem,
    PlexItemKind,
    SonosClient,
    Track,
)
from app.utils.artwork_id import plex_artwork_id
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist, PlexTrack
from app.utils.plex_queue_entry import parse_plex_queue_entry

logger = logging.getLogger(__name__)

# The entries one read of the queue asks for while a task looks for the
# track it plays from: a page of the queue listing at most (section 1.6).
_QUEUE_READ_LIMIT = 100


@dataclass(frozen=True)
class QueueEntry:
    position: int
    title: str
    artist: str
    album: str | None
    duration_ms: int | None
    artwork_id: str | None
    track_id: str | None


@dataclass(frozen=True)
class QueueListing:
    """A page of a group's queue: its entries, the queue's size, the page's
    number and the position of the track that is loaded."""

    entries: list[QueueEntry]
    total: int
    page: int
    current_position: int | None


class QueueService:
    """What a client reads of a group's queue, and has the speaker do with
    it."""

    def __init__(
        self,
        household_mirror: HouseholdMirror,
        sonos_client: SonosClient,
        plex_client: PlexClient,
        queueing_lanes: QueueingLanes,
        playlist_max_tracks: int,
    ) -> None:
        self._mirror = household_mirror
        self._sonos = sonos_client
        self._plex = plex_client
        self._lanes = queueing_lanes
        self._playlist_max_tracks = playlist_max_tracks

    def list_queue(self, speaker_id: str, query: QueueListQuerySchema) -> QueueListing:
        """A page of the queue of the room's group, as the speaker gives it.

        ``current_position`` is the mirror's ``queue_position()``. With
        ``locate`` the page is the one that holds it, while there is one. The
        pictures of the page's Plex tracks are one call to Plex; while Plex
        fails to give them, the entries keep their track ids and have no
        artwork, which is logged. While Plex has never answered, which server
        it is is not known: the page's entries then have no track id and no
        artwork, which is logged.

        Raises:
            SpeakerNotFoundException: The household has no such room.
            ExternalServiceException: The speaker failed.
        """
        current_position = self._mirror.queue_position(speaker_id)
        page = query.page
        if query.locate == "current" and current_position is not None:
            page = (current_position - 1) // query.per_page + 1
        offset = (page - 1) * query.per_page
        queue = self._sonos.read_queue(speaker_id, offset, query.per_page)
        track_ids = self._track_ids(speaker_id, queue.tracks)
        pictures = self._pictures(speaker_id, track_ids)
        entries = [
            _entry(offset + index, track, track_id, pictures)
            for index, (track, track_id) in enumerate(
                zip(queue.tracks, track_ids, strict=True), start=1
            )
        ]
        return QueueListing(entries, queue.total, page, current_position)

    def clear(self, speaker_id: str) -> None:
        """Empty the queue of the room's group.

        Every queueing task asked before on the group is ended, as a Play
        ends them, and the queue is emptied in a turn of its own on the
        group's lane: once the call under way has landed, and before every
        task asked after it. It returns once the queue is empty.

        Raises:
            SpeakerNotFoundException: The household has no such room.
            ExternalServiceException: The speaker failed.
        """
        group_id = self._mirror.speaker(speaker_id).group.coordinator_id
        with self._lanes.clear_turn(group_id):
            self._sonos.clear_queue(speaker_id)

    def queue(self, speaker_id: str, request: QueueRequestSchema) -> TaskStartResponse:
        """Start the task that plays, shuffles or adds an item of the library
        on the room's group.

        The room is read from the mirror and the item from Plex, as it is
        now; nothing is asked of a speaker before the task runs. The task
        takes its turn on the group after every task asked before it, and a
        Play or a Shuffle ends those. The task's events go to every
        connection (``docs/api-design.md`` section 4).

        An album played from a track is checked to hold the track.

        Raises:
            SpeakerNotFoundException: The household has no such room.
            ItemNotFoundException: Plex has no such item, or the album has
                no such track.
            PlaylistNotPlayableException: The playlist holds no tracks, or
                more than ``PLAYLIST_MAX_TRACKS``.
            ExternalServiceException: Plex failed.
        """
        group_id = self._mirror.speaker(speaker_id).group.coordinator_id
        title = self._title(request.kind, request.item_id)
        if request.start_track_id is not None:
            self._check_album_track(request.item_id, request.start_track_id)
        task = QueueingTask(
            self._sonos,
            self._plex,
            self._lanes,
            group_id,
            speaker_id,
            PlexItemKind(request.kind),
            request.item_id,
            title,
            request.action,
            newest_first=request.order == "newest_first",
            start_track_id=request.start_track_id,
        )
        return self._lanes.start(group_id, task, replaces=request.action != "add")

    def _title(self, kind: QueueItemKind, item_id: str) -> str:
        """The item's title, once Plex has the item and it can be queued."""
        item: PlexArtist | PlexAlbum | PlexTrack | PlexPlaylist | None
        match kind:
            case "artist":
                item = self._plex.read_artist(item_id)
            case "album":
                item = self._plex.read_album(item_id)
            case "track":
                item = self._plex.read_track(item_id)
            case _:
                item = self._plex.read_playlist(item_id)
        if item is None:
            raise ItemNotFoundException(kind.capitalize(), item_id)
        if isinstance(item, PlexPlaylist):
            if item.track_count == 0:
                raise PlaylistNotPlayableException(item_id, "it holds no tracks")
            if item.track_count > self._playlist_max_tracks:
                raise PlaylistNotPlayableException(
                    item_id,
                    f"it holds {item.track_count} tracks, more than the "
                    f"{self._playlist_max_tracks} a playlist may hold to be queued",
                )
        return item.title

    def _check_album_track(self, album_id: str, track_id: str) -> None:
        """Plex has the track on the album."""
        tracks = self._plex.read_album_tracks(album_id)
        if tracks is None:
            raise ItemNotFoundException("Album", album_id)
        if all(track.rating_key != track_id for track in tracks):
            raise ItemNotFoundException("Track", f"{track_id} of album {album_id}")

    def _track_ids(self, speaker_id: str, tracks: Sequence[Track]) -> list[str | None]:
        """The rating key of each entry queued from the configured Plex
        server; None for every other entry.

        Plex is asked which server it is only for a page that holds an entry
        of the Plex service.
        """
        entries = [parse_plex_queue_entry(track.uri) for track in tracks]
        if all(entry is None for entry in entries):
            return [None] * len(entries)
        try:
            server = self._plex.machine_identifier()
        except ExternalServiceException as error:
            logger.warning(
                "The queue of speaker %s is served without the track ids and "
                "the artwork of its Plex tracks: %s",
                speaker_id,
                error,
            )
            return [None] * len(entries)
        return [
            entry.rating_key
            if entry is not None and entry.machine_identifier == server
            else None
            for entry in entries
        ]

    def _pictures(
        self, speaker_id: str, track_ids: Sequence[str | None]
    ) -> dict[str, str]:
        """The pictures of the page's Plex tracks, by rating key; none while
        Plex fails to give them, which is logged."""
        try:
            return self._plex.read_track_pictures(
                track_id for track_id in track_ids if track_id is not None
            )
        except ExternalServiceException as error:
            logger.warning(
                "The queue of speaker %s is served without the artwork of its "
                "Plex tracks: %s",
                speaker_id,
                error,
            )
            return {}


class QueueingTask(BaseTask):
    """Play, shuffle or add an item of the Plex library on a room's group
    (``docs/api-design.md`` section 3.4).

    Every item is one container of the Plex service, but an artist played or
    added: its albums, in release order, the first alone and the rest
    ``ADD_MANY_LIMIT`` a call. Plex is read before the queue changes. A
    failure ends the task where it is, the queue as far as it got.

    A Play or a Shuffle plays from the queue's first entry, and an album
    played from a track from that track's entry, looked for in the queue as
    the speaker gives it.

    The task runs in its turn in the group's lane. A task a later Play,
    Shuffle or Clear ends stops before its next call to the speaker and
    returns.
    """

    def __init__(
        self,
        sonos_client: SonosClient,
        plex_client: PlexClient,
        queueing_lanes: QueueingLanes,
        group_id: str,
        speaker_id: str,
        kind: PlexItemKind,
        rating_key: str,
        title: str,
        action: QueueAction,
        newest_first: bool,
        start_track_id: str | None,
    ) -> None:
        super().__init__()
        self._sonos = sonos_client
        self._plex = plex_client
        self._lanes = queueing_lanes
        self._group_id = group_id
        self._speaker_id = speaker_id
        self._kind = kind
        self._rating_key = rating_key
        self._title = title
        self._action = action
        self._newest_first = newest_first
        self._start_track_id = start_track_id

    def execute(
        self, progress_handle: ProgressHandle, **kwargs: Any
    ) -> QueueTaskResultSchema:
        """Queue the item, in the task's turn on the group.

        A task that was ended makes no call from then on, and returns as one
        that is done.

        Raises:
            ItemNotFoundException: Plex no longer has the artist.
            InvalidOperationException: Plex has no albums of the artist, or
                the album the speaker queued lacks the track to play from.
            ExternalServiceException: Plex or the speaker failed.
        """
        with self._lanes.turn(self._group_id, self):
            if not self.is_cancelled:
                self._queue()
        return QueueTaskResultSchema()

    def _queue(self) -> None:
        calls = self._calls(self._containers())
        for done, call in enumerate(calls):
            if self.is_cancelled:
                logger.info(
                    "Queueing %s %s on speaker %s was ended after %d of its %d calls",
                    self._kind.value,
                    self._rating_key,
                    self._speaker_id,
                    done,
                    len(calls),
                )
                return
            call()

    def _calls(self, containers: list[PlexItem]) -> list[Callable[[], None]]:
        """The calls to the speaker that queue the containers, in order."""
        first, *rest = containers
        sonos = self._sonos
        speaker_id = self._speaker_id
        replaces = self._action != "add"
        calls: list[Callable[[], None]] = []
        if replaces:
            calls += [
                partial(sonos.clear_queue, speaker_id),
                # A speaker refuses shuffle while a stream is its source
                # (UPnP error 712), and clearing the queue leaves a stream
                # playing.
                partial(sonos.select_queue, speaker_id),
                # The mode first: the speaker draws what it queues with
                # shuffle on into the play order (docs/api-design.md
                # section 1.5).
                partial(sonos.set_shuffle, speaker_id, self._action == "shuffle"),
            ]
        calls.append(partial(sonos.add_to_queue, speaker_id, first))
        if replaces and self._start_track_id is not None:
            calls.append(partial(self._play_from_track, first.machine_identifier))
        elif replaces:
            calls.append(partial(sonos.play_from, speaker_id, 1))
        calls += [
            partial(
                sonos.add_many_to_queue,
                speaker_id,
                rest[start : start + ADD_MANY_LIMIT],
            )
            for start in range(0, len(rest), ADD_MANY_LIMIT)
        ]
        return calls

    def _containers(self) -> list[PlexItem]:
        server = self._plex.machine_identifier()
        section = self._plex.section_id()
        if self._kind is not PlexItemKind.ARTIST or self._action == "shuffle":
            return [
                PlexItem(self._kind, server, section, self._rating_key, self._title)
            ]
        albums = self._plex.read_artist_albums(self._rating_key, self._newest_first)
        if albums is None:
            raise ItemNotFoundException("Artist", self._rating_key)
        if not albums:
            raise InvalidOperationException(
                f"queue artist {self._rating_key} on speaker {self._speaker_id}",
                "Plex has no albums of the artist",
            )
        return [
            PlexItem(PlexItemKind.ALBUM, server, section, album.rating_key, album.title)
            for album in albums
        ]

    def _play_from_track(self, server: str) -> None:
        """Play from the entry of the track to start at, the first of the
        queue that is that track of the server."""
        offset = 0
        while True:
            page = self._sonos.read_queue(self._speaker_id, offset, _QUEUE_READ_LIMIT)
            for position, track in enumerate(page.tracks, start=offset + 1):
                entry = parse_plex_queue_entry(track.uri)
                if (
                    entry is not None
                    and entry.machine_identifier == server
                    and entry.rating_key == self._start_track_id
                ):
                    self._sonos.play_from(self._speaker_id, position)
                    return
            offset += len(page.tracks)
            if not page.tracks or offset >= page.total:
                raise InvalidOperationException(
                    f"play album {self._rating_key} from track "
                    f"{self._start_track_id} on speaker {self._speaker_id}",
                    "the speaker queued the album without the track",
                )


def _entry(
    position: int, track: Track, track_id: str | None, pictures: dict[str, str]
) -> QueueEntry:
    picture = pictures.get(track_id) if track_id is not None else None
    return QueueEntry(
        position=position,
        title=_nfc(track.title),
        artist=_nfc(track.artist),
        album=_nfc(track.album) or None,
        duration_ms=track.duration_seconds * 1000 or None,
        artwork_id=plex_artwork_id(picture) if picture is not None else None,
        track_id=track_id,
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
