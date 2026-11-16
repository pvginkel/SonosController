"""The favourites as the API answers them, and the request that marks an item
(``docs/api-design.md`` section 3.6)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FavouriteKind = Literal["artist", "album", "playlist"]


class FavouriteRequestSchema(BaseModel):
    """An item of the library to mark as a favourite: an artist, an album or
    a playlist. A track cannot be one."""

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: FavouriteKind = Field(description="What the item is.")
    item_id: str = Field(
        description=(
            "Plex's rating key of the item: the `id` of an artist, an album or "
            "a playlist of `/api/library`."
        )
    )


class FavouriteSchema(BaseModel):
    """A favourite: the household's mark on an artist, an album or a playlist
    of the library, the same for every client."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="The favourite's id, the `favourite_id` of a path.")
    kind: FavouriteKind = Field(description="What the item is.")
    item_id: str | None = Field(
        description=(
            "Where the item is in Plex now: its rating key, the `id` of the "
            "artist, the album or the playlist in `/api/library`, and the "
            "`item_id` to queue it with. `null` when the favourite is not "
            "`available`."
        )
    )
    title: str = Field(
        description="The artist's name, or the album's or the playlist's title."
    )
    subtitle: str | None = Field(
        description="The name of an album's artist; `null` for an artist and a playlist."
    )
    artwork_id: str | None = Field(
        description=(
            "The id of the item's picture for `GET /api/artwork/{artwork_id}`: "
            "the one `/api/library` carries for the item. `null` for an item "
            "without a picture, and when the favourite is not `available`."
        )
    )
    available: bool = Field(
        description=(
            "Whether Plex has the item now. A favourite whose item Plex does "
            "not have is kept, with its title and subtitle."
        )
    )
    created_at: datetime = Field(description="When the item was marked.")
