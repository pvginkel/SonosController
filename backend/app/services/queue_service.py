"""The queue of a speaker's group (``docs/api-design.md`` section 3.4).

The queue lives on the speaker: every call reads or changes the queue of the
group's coordinator through the Sonos client, and nothing of it is kept here.
Whether the speaker exists, and where in its queue the group is, is read from
the household mirror.

An entry is a track of the Plex library when its address carries the machine
identifier of the configured Plex server: it gets its rating key and Plex's
picture, and every other entry neither. Every string is Unicode NFC.

An item of the library is queued by a task, as containers of the Plex
service linked in Sonos (sections 1.3 to 1.5).
"""

import logging
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
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
from app.services.sonos_client import (
    ADD_MANY_LIMIT,
    PlexItem,
    PlexItemKind,
    SonosClient,
    Track,
)
from app.services.task_service import TaskService
from app.utils.artwork_id import plex_artwork_id
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist, PlexTrack
from app.utils.plex_queue_entry import parse_plex_queue_entry

logger = logging.getLogger(__name__)


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
        task_service: TaskService,
        playlist_max_tracks: int,
    ) -> None:
        self._mirror = household_mirror
        self._sonos = sonos_client
        self._plex = plex_client
        self._tasks = task_service
        self._playlist_max_tracks = playlist_max_tracks

    def list_queue(self, speaker_id: str, query: QueueListQuerySchema) -> QueueListing:
        """A page of the queue of the room's group, as the speaker gives it.

        ``current_position`` is the mirror's ``queue_position()``. With
        ``locate`` the page is the one that holds it, while there is one. The
        pictures of the page's Plex tracks are one call to Plex. While Plex
        has never answered, which server it is is not known: the page's
        entries then have no track id and no artwork, which is logged.

        Raises:
            SpeakerNotFoundException: The household has no such room.
            ExternalServiceException: The speaker failed, or Plex failed to
                give the pictures of the page's tracks.
        """
        current_position = self._mirror.queue_position(speaker_id)
        page = query.page
        if query.locate == "current" and current_position is not None:
            page = (current_position - 1) // query.per_page + 1
        offset = (page - 1) * query.per_page
        queue = self._sonos.read_queue(speaker_id, offset, query.per_page)
        track_ids = self._track_ids(speaker_id, queue.tracks)
        pictures = self._plex.read_track_pictures(
            track_id for track_id in track_ids if track_id is not None
        )
        entries = [
            _entry(offset + index, track, track_id, pictures)
            for index, (track, track_id) in enumerate(
                zip(queue.tracks, track_ids, strict=True), start=1
            )
        ]
        return QueueListing(entries, queue.total, page, current_position)

    def clear(self, speaker_id: str) -> None:
        """Empty the queue of the room's group.

        Raises:
            SpeakerNotFoundException: The household has no such room.
            ExternalServiceException: The speaker failed.
        """
        self._mirror.speaker(speaker_id)
        self._sonos.clear_queue(speaker_id)

    def queue(self, speaker_id: str, request: QueueRequestSchema) -> TaskStartResponse:
        """Start the task that plays, shuffles or adds an item of the library
        on the room's group.

        The room is read from the mirror and the item from Plex, as it is
        now; nothing is asked of a speaker before the task runs. The task's
        events go to every connection (``docs/api-design.md`` section 4).

        Raises:
            SpeakerNotFoundException: The household has no such room.
            ItemNotFoundException: Plex has no such item.
            PlaylistNotPlayableException: The playlist holds no tracks, or
                more than ``PLAYLIST_MAX_TRACKS``.
            ExternalServiceException: Plex failed.
        """
        self._mirror.speaker(speaker_id)
        title = self._title(request.kind, request.item_id)
        task = QueueingTask(
            self._sonos,
            self._plex,
            speaker_id,
            PlexItemKind(request.kind),
            request.item_id,
            title,
            request.action,
            newest_first=request.order == "newest_first",
        )
        # The app has no users: the task's events go to the local admin,
        # which every connection is (SSE-3).
        return self._tasks.start_task(task, caller_subject=None)

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


class QueueingTask(BaseTask):
    """Play, shuffle or add an item of the Plex library on a room's group
    (``docs/api-design.md`` section 3.4).

    Every item is one container of the Plex service, but an artist played or
    added: its albums, in release order, the first alone and the rest
    ``ADD_MANY_LIMIT`` a call. Plex is read before the queue changes. A
    failure ends the task where it is, the queue as far as it got.
    """

    def __init__(
        self,
        sonos_client: SonosClient,
        plex_client: PlexClient,
        speaker_id: str,
        kind: PlexItemKind,
        rating_key: str,
        title: str,
        action: QueueAction,
        newest_first: bool,
    ) -> None:
        super().__init__()
        self._sonos = sonos_client
        self._plex = plex_client
        self._speaker_id = speaker_id
        self._kind = kind
        self._rating_key = rating_key
        self._title = title
        self._action = action
        self._newest_first = newest_first

    def execute(
        self, progress_handle: ProgressHandle, **kwargs: Any
    ) -> QueueTaskResultSchema:
        """Queue the item.

        Raises:
            ItemNotFoundException: Plex no longer has the artist.
            InvalidOperationException: Plex has no albums of the artist.
            ExternalServiceException: Plex or the speaker failed.
        """
        first, *rest = self._containers()
        speaker_id = self._speaker_id
        replaces = self._action != "add"
        if replaces:
            self._sonos.clear_queue(speaker_id)
            # A speaker refuses shuffle while a stream is its source (UPnP
            # error 712), and clearing the queue leaves a stream playing.
            self._sonos.select_queue(speaker_id)
            # The mode first: the speaker draws what it queues with shuffle
            # on into the play order (docs/api-design.md section 1.5).
            self._sonos.set_shuffle(speaker_id, self._action == "shuffle")
        self._sonos.add_to_queue(speaker_id, first)
        if replaces:
            self._sonos.play_from_start(speaker_id)
        for start in range(0, len(rest), ADD_MANY_LIMIT):
            self._sonos.add_many_to_queue(
                speaker_id, rest[start : start + ADD_MANY_LIMIT]
            )
        return QueueTaskResultSchema()

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
