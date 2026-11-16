"""The household's favourites: its marks on artists, albums and playlists of
the Plex library (``docs/api-design.md`` sections 3.6 and 5).

A favourite is the store's record of which item is meant; the item itself is
read from Plex. What leaves here is ready to serve: every string is Unicode
NFC, and a picture is the artwork id the library carries for the item.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime

from psycopg.errors import UniqueViolation
from sqlalchemy import Select, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import scoped_savepoint
from app.exceptions import ItemNotFoundException, RecordNotFoundException
from app.models import Favourite, ItemKind
from app.services.plex_client import PlexClient
from app.utils.artwork_id import plex_artwork_id
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist

_PLEX_TYPE: dict[ItemKind, type[PlexArtist | PlexAlbum | PlexPlaylist]] = {
    ItemKind.ARTIST: PlexArtist,
    ItemKind.ALBUM: PlexAlbum,
    ItemKind.PLAYLIST: PlexPlaylist,
}

# The store's one mark per item, on kind and guid (app.models.favourite).
_ONE_MARK_PER_ITEM = "uq_favourites_item_kind_guid"

# The largest id the favourite's key, a PostgreSQL integer, holds.
_LARGEST_ID = 2**31 - 1


@dataclass(frozen=True)
class ServedFavourite:
    """A favourite as the API serves it: the store's record, with its item
    as Plex has it now."""

    id: int
    kind: ItemKind
    item_id: str | None
    title: str
    subtitle: str | None
    artwork_id: str | None
    available: bool
    created_at: datetime


class FavouriteService:
    """The favourites, kept in the store and shared by every client."""

    def __init__(self, db_session: Session, plex_client: PlexClient) -> None:
        self._db = db_session
        self._plex = plex_client

    def mark(self, kind: ItemKind, item_id: str) -> tuple[ServedFavourite, bool]:
        """Mark an item of the library: the favourite that marks it, and
        whether this mark made it.

        The item is read from Plex as it is now. One favourite marks every
        item of a kind and ``guid``: the item marked already, or the other
        copy of a release the library holds twice, is answered with the
        favourite that marks it, as is a mark that meets another of the same
        item stored first.

        Raises:
            ItemNotFoundException: Plex has no item of this kind with this
                rating key.
            ExternalServiceException: Plex failed.
        """
        item = self._plex.read_items([item_id]).get(item_id)
        if not isinstance(item, _PLEX_TYPE[kind]):
            raise ItemNotFoundException(kind.value.capitalize(), item_id)
        marking = self._db.scalars(_marking(kind, item.guid)).one_or_none()
        if marking is not None:
            return _served(marking, item), False
        favourite = Favourite(
            item_kind=kind,
            guid=item.guid,
            rating_key=item.rating_key,
            title=item.title,
            artist_title=item.artist if isinstance(item, PlexAlbum) else None,
        )
        try:
            with scoped_savepoint(self._db):
                self._db.add(favourite)
                self._db.flush()
        except IntegrityError as exc:
            if not (
                isinstance(exc.orig, UniqueViolation)
                and exc.orig.diag.constraint_name == _ONE_MARK_PER_ITEM
            ):
                raise
            # A mark of the item made at the same time committed first.
            stored_first = self._db.scalars(_marking(kind, item.guid)).one()
            return _served(stored_first, item), False
        return _served(favourite, item), True

    def unmark(self, favourite_id: int) -> None:
        """Remove a favourite. Plex is not asked.

        Raises:
            RecordNotFoundException: The store holds no favourite with this
                id.
        """
        if (
            favourite_id > _LARGEST_ID
            or self._db.scalar(
                delete(Favourite)
                .where(Favourite.id == favourite_id)
                .returning(Favourite.id)
            )
            is None
        ):
            raise RecordNotFoundException("Favourite", favourite_id)


def _marking(kind: ItemKind, guid: str) -> Select[Favourite]:
    """The favourite that marks the items of this kind and guid."""
    return select(Favourite).where(Favourite.item_kind == kind, Favourite.guid == guid)


def _served(
    favourite: Favourite, item: PlexArtist | PlexAlbum | PlexPlaylist
) -> ServedFavourite:
    """A favourite with its item, which Plex has now."""
    return ServedFavourite(
        id=favourite.id,
        kind=favourite.item_kind,
        item_id=item.rating_key,
        title=_nfc(item.title),
        subtitle=_nfc(item.artist) if isinstance(item, PlexAlbum) else None,
        artwork_id=plex_artwork_id(item.picture) if item.picture is not None else None,
        available=True,
        created_at=favourite.created_at,
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
