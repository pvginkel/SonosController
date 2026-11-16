"""The household's favourites: its marks on artists, albums and playlists of
the Plex library (``docs/api-design.md`` sections 3.6 and 5).

A favourite is the store's record of which item is meant; the item itself is
read from Plex. What leaves here is ready to serve: every string is Unicode
NFC, and a picture is the artwork id the library carries for the item.

Every change to the store is announced to every client, as
``favourites_changed``, once the transaction that made it has committed
(section 4).
"""

import logging
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from psycopg.errors import UniqueViolation
from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core_exceptions import ExternalServiceException
from app.database import scoped_savepoint
from app.exceptions import ItemNotFoundException, RecordNotFoundException
from app.models import Favourite, ItemKind
from app.schemas.favourite_schema import FavouriteListQuerySchema
from app.schemas.stream_event_schema import FavouritesChangedEvent
from app.services.event_sender import EventSender
from app.services.plex_client import PlexClient
from app.utils.after_commit import after_commit
from app.utils.artwork_id import plex_artwork_id
from app.utils.pagination import apply_sorting
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist

logger = logging.getLogger(__name__)

_PLEX_TYPE: dict[ItemKind, type[PlexArtist | PlexAlbum | PlexPlaylist]] = {
    ItemKind.ARTIST: PlexArtist,
    ItemKind.ALBUM: PlexAlbum,
    ItemKind.PLAYLIST: PlexPlaylist,
}

# The store's one mark per item, on kind and guid (app.models.favourite).
_ONE_MARK_PER_ITEM = "uq_favourites_item_kind_guid"

# The guid of an item Plex has not matched: ``local://<rating key>``, no
# steadier than the rating key (docs/api-design.md section 1.8).
_UNMATCHED = "local://"

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

    def __init__(
        self, db_session: Session, plex_client: PlexClient, event_sender: EventSender
    ) -> None:
        self._db = db_session
        self._plex = plex_client
        self._events = event_sender

    def list_favourites(
        self, query: FavouriteListQuerySchema
    ) -> tuple[list[ServedFavourite], int]:
        """A page of the favourites, each with its item as Plex has it now,
        and how many favourites there are.

        The page's favourites are looked for together, in one read of Plex:
        each at the rating key it holds, where it is found when the item
        there is of its kind and has its ``guid``. One not found there is
        looked for on its own: by its ``guid``, unless a ``local://`` one,
        and of several items with it the first whose titles are the
        favourite's, else the first; else by its kind and titles, exactly.
        One found that way is brought up to date with its item's rating key,
        ``guid`` and titles, so that the next listing finds it at its rating
        key; and where another favourite marks that item, the one marked
        first stays and the other is removed, and not listed. A listing that
        brought one up to date or removed one is announced. One found
        nowhere is served as it is stored, not available, and kept.

        While Plex fails, the page is served from the store alone, none of it
        available, nothing stored changes, and the failure is logged.

        A title is ordered by the database's collation, ICU's ``und``: by
        its letters first, without accents and in one case.
        """
        page = self._db.scalars(
            apply_sorting(select(Favourite), Favourite, query.sort_by, query.sort_dir)
            .offset(query.offset)
            .limit(query.per_page)
        ).all()
        try:
            found = self._look_for(page)
        except ExternalServiceException as error:
            logger.warning("The favourites are served without their items: %s", error)
            found = {}
        removed: set[int] = set()
        for favourite in page:
            item = found.get(favourite.id)
            if (
                item is None
                or favourite.id in removed
                or _at_its_rating_key(favourite, item)
            ):
                continue
            removed |= self._bring_up_to_date(favourite, item)
        total = self._db.execute(
            select(func.count()).select_from(Favourite)
        ).scalar_one()
        return [
            _listed(favourite, found.get(favourite.id))
            for favourite in page
            if favourite.id not in removed
        ], total

    def mark(self, kind: ItemKind, item_id: str) -> tuple[ServedFavourite, bool]:
        """Mark an item of the library: the favourite that marks it, and
        whether this mark made it.

        The item is read from Plex as it is now. One favourite marks every
        item of a kind and ``guid``: the item marked already, or the other
        copy of a release the library holds twice, is answered with the
        favourite that marks it, as is a mark that meets another of the same
        item stored first. Only a mark that made the favourite is announced.

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
            artist_title=_artist_title(item),
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
        self._announce()
        return _served(favourite, item), True

    def favourite_ids(self, kind: ItemKind, guids: Iterable[str]) -> dict[str, int]:
        """The id of the favourite that marks the items of this kind and each
        of these guids, by guid: the favourite a mark of such an item answers.
        A guid no favourite marks is left out.

        One query of the store, none for no guids; Plex is not asked.
        """
        wanted = set(guids)
        if not wanted:
            return {}
        rows = self._db.execute(
            select(Favourite.guid, Favourite.id).where(
                Favourite.item_kind == kind, Favourite.guid.in_(wanted)
            )
        )
        return {row.guid: row.id for row in rows}

    def unmark(self, favourite_id: int) -> None:
        """Remove a favourite, and announce it. Plex is not asked.

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
        self._announce()

    def _announce(self) -> None:
        """Send ``favourites_changed`` once this transaction has committed;
        once, however many changes it holds."""
        after_commit(self._send_favourites_changed)

    def _send_favourites_changed(self) -> None:
        self._events.send(FavouritesChangedEvent())

    def _look_for(
        self, page: Sequence[Favourite]
    ) -> dict[int, PlexArtist | PlexAlbum | PlexPlaylist]:
        """The item each favourite of the page marks, by the favourite's id;
        a favourite Plex has no item for is left out.

        Raises:
            ExternalServiceException: Plex failed.
        """
        at_rating_keys = self._plex.read_items(
            favourite.rating_key for favourite in page
        )
        found: dict[int, PlexArtist | PlexAlbum | PlexPlaylist] = {}
        for favourite in page:
            item = at_rating_keys.get(favourite.rating_key)
            if item is None or not _at_its_rating_key(favourite, item):
                item = self._found_again(favourite)
            if item is not None:
                found[favourite.id] = item
        return found

    def _found_again(
        self, favourite: Favourite
    ) -> PlexArtist | PlexAlbum | PlexPlaylist | None:
        """The item of a favourite its rating key no longer finds: by its
        guid, then by its titles (``docs/api-design.md`` section 5,
        decision 2)."""
        kind = _PLEX_TYPE[favourite.item_kind]
        if not favourite.guid.startswith(_UNMATCHED):
            hits = self._plex.find_by_guid(kind, favourite.guid)
            if hits:
                return next((hit for hit in hits if _titled(hit, favourite)), hits[0])
        hits = self._plex.find_by_title(kind, favourite.title)
        return next((hit for hit in hits if _titled(hit, favourite)), None)

    def _bring_up_to_date(
        self, favourite: Favourite, item: PlexArtist | PlexAlbum | PlexPlaylist
    ) -> set[int]:
        """Have a favourite found by its guid or its titles hold its item's
        rating key, guid and titles: the ids of the favourites removed.

        Another favourite that marks the item, by its kind and guid, is
        removed when this one was marked first, and this one is when it was
        not, as a second mark of an item is answered with the favourite
        marked first.
        """
        self._announce()
        # Statements, not the session's unit of work: a listing at the same
        # time may have changed or removed these rows, which the unit of work
        # refuses.
        other = self._db.scalars(
            _marking(favourite.item_kind, item.guid).where(Favourite.id != favourite.id)
        ).one_or_none()
        if other is not None:
            marked_later = max(other, favourite, key=_marked_order)
            self._db.execute(delete(Favourite).where(Favourite.id == marked_later.id))
            if marked_later is favourite:
                return {favourite.id}
        self._db.execute(
            update(Favourite)
            .where(Favourite.id == favourite.id)
            .values(
                guid=item.guid,
                rating_key=item.rating_key,
                title=item.title,
                artist_title=_artist_title(item),
            )
        )
        return {other.id} if other is not None else set()


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


def _listed(
    favourite: Favourite, item: PlexArtist | PlexAlbum | PlexPlaylist | None
) -> ServedFavourite:
    """A favourite with its item, or as it is stored where Plex has none."""
    return _served(favourite, item) if item is not None else _unavailable(favourite)


def _at_its_rating_key(
    favourite: Favourite, item: PlexArtist | PlexAlbum | PlexPlaylist
) -> bool:
    """Whether the item Plex has at the favourite's rating key is its item:
    of its kind, with its guid."""
    return (
        isinstance(item, _PLEX_TYPE[favourite.item_kind])
        and item.rating_key == favourite.rating_key
        and item.guid == favourite.guid
    )


def _titled(item: PlexArtist | PlexAlbum | PlexPlaylist, favourite: Favourite) -> bool:
    """Whether an item has the favourite's title and, for an album, its
    artist's title, exactly as Plex gave them."""
    return (item.title, _artist_title(item)) == (
        favourite.title,
        favourite.artist_title,
    )


def _artist_title(item: PlexArtist | PlexAlbum | PlexPlaylist) -> str | None:
    """The title of an album's artist; None for an artist and a playlist."""
    return item.artist if isinstance(item, PlexAlbum) else None


def _marked_order(favourite: Favourite) -> tuple[datetime, int]:
    """When a favourite was marked, its id ordering marks made at once."""
    return favourite.created_at, favourite.id


def _unavailable(favourite: Favourite) -> ServedFavourite:
    """A favourite whose item Plex does not give now, as it is stored."""
    return ServedFavourite(
        id=favourite.id,
        kind=favourite.item_kind,
        item_id=None,
        title=_nfc(favourite.title),
        subtitle=(
            _nfc(favourite.artist_title) if favourite.artist_title is not None else None
        ),
        artwork_id=None,
        available=False,
        created_at=favourite.created_at,
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
