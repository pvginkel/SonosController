"""The favourite: the household's mark on an artist, an album or a playlist of
the Plex library (``docs/api-design.md`` section 5)."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class ItemKind(StrEnum):
    """A kind of item of the Plex library a favourite can mark, as the API's
    ``kind`` writes it. A track is none."""

    ARTIST = "artist"
    ALBUM = "album"
    PLAYLIST = "playlist"


class Favourite(db.Model):  # type: ignore[name-defined]
    """A mark on one item of the Plex library, kept with what finds the item
    again after Plex has rescanned: its kind and ``guid``, the rating key and
    the titles it had when the favourite last found it."""

    __tablename__ = "favourites"
    __table_args__ = (
        # One mark per item, held on kind and guid: the two copies of a release
        # the library holds twice share one guid, and so one mark.
        UniqueConstraint("item_kind", "guid", name="uq_favourites_item_kind_guid"),
        CheckConstraint(
            "(item_kind = 'album') = (artist_title IS NOT NULL)",
            name="ck_favourites_artist_title_of_album",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_kind: Mapped[ItemKind] = mapped_column(
        Enum(
            ItemKind,
            name="ck_favourites_item_kind",
            native_enum=False,
            create_constraint=True,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        )
    )
    guid: Mapped[str] = mapped_column(Text)
    rating_key: Mapped[str] = mapped_column(Text)
    # The titles are kept as Plex gives them, an empty one too, so that the
    # lookup by title compares Plex's text with Plex's text.
    title: Mapped[str] = mapped_column(Text, info={"allow_empty": True})
    artist_title: Mapped[str | None] = mapped_column(Text, info={"allow_empty": True})
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
