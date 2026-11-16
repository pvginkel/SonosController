"""The queue of a speaker's group, as the API answers it, the query of its
list and what a client asks to queue (``docs/api-design.md`` section 3.4)."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import PaginationMetaSchema, PaginationQuerySchema


class QueueEntrySchema(BaseModel):
    """An entry of a group's queue."""

    model_config = ConfigDict(from_attributes=True)

    position: int = Field(
        ge=1,
        description=(
            "The entry's place in the queue, from 1, in the order the speaker "
            "gives the queue: with shuffle on, the order in which it plays."
        ),
    )
    title: str = Field(description="The track's title.")
    artist: str = Field(description="The track's artist.")
    album: str | None = Field(
        description="The track's album; `null` where the entry gives none."
    )
    duration_ms: int | None = Field(
        description=(
            "The track's length in milliseconds; `null` where the entry gives none."
        )
    )
    artwork_id: str | None = Field(
        description=(
            "The id of the track's picture, for `GET /api/artwork/{artwork_id}`: "
            "Plex's, which the tracks of an album share, for a track queued "
            "from the Plex library. `null` for an entry that is not from the "
            "Plex library, and for a track without a picture."
        )
    )
    track_id: str | None = Field(
        description=(
            "The Plex rating key of the track, when it was queued from the "
            "Plex library; `null` for anything else."
        )
    )


class QueueListQuerySchema(PaginationQuerySchema):
    sort_by: Literal["position"] = Field(
        "position",
        description="`position`, the only order: the queue as the speaker gives it.",
    )
    sort_dir: Literal["asc"] = Field(
        "asc", description="`asc`, the only direction: from the queue's first entry."
    )
    locate: Literal["current"] | None = Field(
        None,
        description=(
            "`current`: answer the page that holds the track that is loaded, "
            "whatever `page` says; `meta.page` tells which page it is. While "
            "no track of the queue is loaded, the answer is the page `page` "
            "asks for."
        ),
    )


class QueuePageSchema(BaseModel):
    """A page of a group's queue, and where in it the group is."""

    data: list[QueueEntrySchema] = Field(description="The entries of the page.")
    meta: PaginationMetaSchema = Field(
        description="Which page it is, and how many entries the queue holds."
    )
    current_position: int | None = Field(
        ge=1,
        description=(
            "The `position` of the track that is loaded, the playback's "
            "`queue_position`; `null` when the group plays from elsewhere "
            "than its queue, and for an empty queue."
        ),
    )


QueueItemKind = Literal["artist", "album", "track", "playlist"]
QueueAction = Literal["play", "shuffle", "add"]


class QueueRequestSchema(BaseModel):
    """An item of the library to play, shuffle or add to the queue of the
    speaker's group. A track is played or added, never shuffled."""

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: QueueItemKind = Field(
        description="What the item is."
    )
    item_id: str = Field(
        description=(
            "Plex's rating key of the item: the `id` of an artist, an album, "
            "a track or a playlist of `/api/library`."
        )
    )
    action: QueueAction = Field(
        description=(
            "`play` replaces the queue with the item and plays it in order. "
            "`shuffle` replaces the queue with the item and plays it "
            "shuffled. `add` appends the item to the queue and changes "
            "nothing else: not the play mode, not what plays."
        )
    )
    order: Literal["newest_first", "oldest_first"] = Field(
        "newest_first",
        description=(
            "The order of an artist's albums with `play` or `add`: by release "
            "date, `newest_first` or `oldest_first`, each album in disc and "
            "track order. Ignored for every other item and action."
        ),
    )

    @model_validator(mode="after")
    def _a_track_is_not_shuffled(self) -> Self:
        if self.kind == "track" and self.action == "shuffle":
            raise ValueError("a track is played or added, not shuffled")
        return self


class QueueTaskResultSchema(BaseModel):
    """What a queueing task that completed reports: nothing but that it
    completed."""
