"""The music library's artists, albums, tracks and playlists, as the API
answers them, and the queries of their lists (``docs/api-design.md`` section
3.5)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import (
    PaginationMetaSchema,
    PaginationQuerySchema,
    SortDirection,
)

_ARTWORK = (
    "for `GET /api/artwork/{artwork_id}`. Two items with the same picture "
    "carry the same id, and a picture that changes gets another."
)


def _favourite(item: str) -> str:
    return (
        f"The `id` of the favourite that marks the {item}, the one "
        "`POST /api/favourites` answers for it and the `favourite_id` that "
        "`DELETE /api/favourites/{favourite_id}` removes; `null` when no "
        f"favourite marks the {item}."
    )


class ArtistSchema(BaseModel):
    """An artist of the music library."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "Plex's rating key of the artist, the `artist_id` of a path and "
            "the `item_id` to queue the artist with."
        )
    )
    title: str = Field(description="The artist's name.")
    index_letter: str = Field(
        pattern="^[A-Z#]$",
        description=(
            "The letter the artist files under: `A` to `Z`, the first letter "
            "of the title the artist is sorted by, without its accent; `#` "
            "when that title starts with anything else, such as a digit. A "
            "letter's artists are together in the list, and those under `#` "
            "come before `A`."
        ),
    )
    artwork_id: str | None = Field(
        description=(
            f"The id of the artist's picture, {_ARTWORK} `null` for an artist "
            "without a picture."
        )
    )
    favourite_id: int | None = Field(description=_favourite("artist"))


class AlbumSchema(BaseModel):
    """An album of the music library."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "Plex's rating key of the album, the `album_id` of a path and the "
            "`item_id` to queue the album with."
        )
    )
    title: str = Field(description="The album's title.")
    artist_id: str = Field(description="The `id` of the album's artist.")
    artist: str = Field(description="The name of the album's artist.")
    year: int | None = Field(
        description="The year the album was released; `null` for an album without a date."
    )
    track_count: int = Field(ge=0, description="How many tracks the album has.")
    artwork_id: str | None = Field(
        description=(
            f"The id of the album's picture, {_ARTWORK} It is the album's "
            "cover, or its artist's picture where the album has no cover; "
            "`null` when it has neither."
        )
    )
    favourite_id: int | None = Field(
        description=(
            f"{_favourite('album')} The two copies of a release the library "
            "holds twice carry the one favourite."
        )
    )


class TrackSchema(BaseModel):
    """A track of an album."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "Plex's rating key of the track, the `item_id` to queue the track "
            "with, and the `track_id` of the playback's track when it plays."
        )
    )
    title: str = Field(description="The track's title.")
    artist: str = Field(
        description=(
            "The track's own artist where it has one, as on a compilation, "
            "and its album's artist otherwise."
        )
    )
    disc: int | None = Field(
        description=(
            "The disc of the album the track is on, from 1; `null` for a "
            "track without one."
        )
    )
    number: int | None = Field(
        description="The track's number on its disc; `null` for a track without one."
    )
    duration_ms: int | None = Field(
        description="The track's length in milliseconds; `null` when it is not known."
    )
    artwork_id: str | None = Field(
        description=(
            f"The id of the track's picture, {_ARTWORK} It is its album's "
            "picture, which the album's tracks share: the album's cover, or "
            "its artist's picture where the album has no cover; `null` when "
            "the album has neither."
        )
    )


class PlaylistSchema(BaseModel):
    """A playlist of music."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "Plex's rating key of the playlist, the `playlist_id` of a path "
            "and the `item_id` to queue the playlist with."
        )
    )
    title: str = Field(description="The playlist's title.")
    track_count: int = Field(ge=0, description="How many tracks the playlist holds.")
    duration_ms: int | None = Field(
        description=(
            "The length of the playlist's tracks together, in milliseconds; "
            "`null` for a playlist without tracks."
        )
    )
    artwork_id: str | None = Field(
        description=(
            "The id of the playlist's picture, which Plex composes of its "
            "tracks' covers, for `GET /api/artwork/{artwork_id}`; `null` for "
            "a playlist without tracks. `GET /api/library/playlists/"
            "{playlist_id}` gives the picture a new id at every request, and "
            "the list carries that id from then on; an older id goes on "
            "answering the picture."
        )
    )
    favourite_id: int | None = Field(description=_favourite("playlist"))


class ArtistListQuerySchema(PaginationQuerySchema):
    sort_by: Literal["sort_title"] = Field(
        "sort_title",
        description=(
            "`sort_title`, the only order: by the title Plex sorts the artist "
            "by, without accents and in one case, the artists that file "
            "under `#` first; then by `id`."
        ),
    )


class AlbumListQuerySchema(PaginationQuerySchema):
    sort_by: Literal["release", "title"] = Field(
        "release",
        description=(
            "`release`: by release date, then title, then `id`, the albums "
            "without a date last in either direction; the order in which an "
            "artist is queued. `title`: by title, without accents and in one "
            "case, then `id`."
        ),
    )
    sort_dir: SortDirection = Field(
        "desc",
        description="Sort direction. `desc`, the default, is newest first for `release`.",
    )


class TrackListQuerySchema(PaginationQuerySchema):
    sort_by: Literal["position"] = Field(
        "position",
        description=(
            "`position`, the only order: the track's place on the album, by "
            "`disc`, then `number`, then `id`. A track without a disc or a "
            "number comes before those that have one."
        ),
    )


class PlaylistListQuerySchema(PaginationQuerySchema):
    sort_by: Literal["title"] = Field(
        "title",
        description=(
            "`title`, the only order: by title, without accents and in one "
            "case, then `id`."
        ),
    )


class ArtistPageSchema(BaseModel):
    """A page of the artists."""

    data: list[ArtistSchema] = Field(description="The artists of the page.")
    meta: PaginationMetaSchema = Field(
        description="Which page it is, and how many artists there are."
    )


class AlbumPageSchema(BaseModel):
    """A page of an artist's albums."""

    data: list[AlbumSchema] = Field(description="The albums of the page.")
    meta: PaginationMetaSchema = Field(
        description="Which page it is, and how many albums the artist has."
    )


class TrackPageSchema(BaseModel):
    """A page of an album's tracks."""

    data: list[TrackSchema] = Field(description="The tracks of the page.")
    meta: PaginationMetaSchema = Field(
        description="Which page it is, and how many tracks the album has."
    )


class PlaylistPageSchema(BaseModel):
    """A page of the playlists."""

    data: list[PlaylistSchema] = Field(description="The playlists of the page.")
    meta: PaginationMetaSchema = Field(
        description="Which page it is, and how many playlists are listed."
    )
