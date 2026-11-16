"""The queue of a speaker's group, as the API answers it, and the query of its
list (``docs/api-design.md`` section 3.4)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
