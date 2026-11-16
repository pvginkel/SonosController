"""The queue of a speaker's group (``docs/api-design.md`` section 3.4).

The queue lives on the speaker: every call reads or changes the queue of the
group's coordinator through the Sonos client, and nothing of it is kept here.
Whether the speaker exists, and where in its queue the group is, is read from
the household mirror.

An entry is a track of the Plex library when its address carries the machine
identifier of the configured Plex server: it gets its rating key and Plex's
picture, and every other entry neither. Every string is Unicode NFC.
"""

import logging
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from app.core_exceptions import ExternalServiceException
from app.schemas.queue_schema import QueueListQuerySchema
from app.services.household_mirror import HouseholdMirror
from app.services.plex_client import PlexClient
from app.services.sonos_client import SonosClient, Track
from app.utils.artwork_id import plex_artwork_id
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
    ) -> None:
        self._mirror = household_mirror
        self._sonos = sonos_client
        self._plex = plex_client

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
