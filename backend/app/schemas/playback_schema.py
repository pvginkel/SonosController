"""What a speaker plays, as the API answers it (``docs/api-design.md``
section 3.2).

The three ``Literal``s hold the values of the household mirror's
``PlaybackStatus``, ``PlaybackSource`` and ``PlaybackAction``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlaybackStateName = Literal["playing", "paused", "stopped", "buffering"]
PlaybackSourceName = Literal["queue", "radio", "line_in", "tv", "other"]
PlaybackActionName = Literal["play", "pause", "next", "previous"]


class TrackSchema(BaseModel):
    """The track a speaker's group has loaded."""

    model_config = ConfigDict(from_attributes=True)

    title: str = Field(
        description=(
            "The track's title. For a radio stream: what the stream reports "
            "as playing, and the station's name until it reports anything."
        )
    )
    artist: str = Field(
        description="The track's artist. For a radio stream: the station's name."
    )
    album: str | None = Field(
        description=(
            "The track's album; `null` where the source gives none, as a radio "
            "stream does."
        )
    )
    duration_ms: int | None = Field(
        description=(
            "The track's length in milliseconds; `null` where the source gives "
            "none, as a radio stream does."
        )
    )
    artwork_id: str | None = Field(
        description=(
            "The id of the track's picture, for `GET /api/artwork/{artwork_id}`; "
            "`null` when there is none to show, as for a radio stream. A track "
            "of the Plex library has Plex's picture, which the tracks of an "
            "album share. Anything else has the picture its speaker serves, "
            "which the artwork endpoint answers only while that track is the "
            "one loaded."
        )
    )
    track_id: str | None = Field(
        description=(
            "The Plex rating key of the track, when it was queued from the "
            "Plex library; `null` for anything else."
        )
    )


class PlaybackSchema(BaseModel):
    """What a speaker's group plays, and what can be done with it. It is the
    same for every speaker of a group.
    """

    model_config = ConfigDict(from_attributes=True)

    state: PlaybackStateName = Field(
        description=(
            "What the group is doing. `buffering`: it is about to play and "
            "waits for the music."
        )
    )
    source: PlaybackSourceName = Field(
        description=(
            "Where what plays comes from: `queue` the speaker's queue, `radio` "
            "a radio stream, `line_in` a line-in, `tv` a TV's sound, `other` "
            "anything else."
        )
    )
    actions: list[PlaybackActionName] = Field(
        description=(
            "The commands the speaker allows now, each the last part of its "
            "endpoint's path. A client offers these alone: a command that is "
            "not listed is answered `409`. A radio stream lists `pause`, "
            "which stops it."
        )
    )
    shuffle: bool = Field(description="Whether the queue plays in a shuffled order.")
    queue_position: int | None = Field(
        description=(
            "The place in the queue of the track that is loaded, from 1; "
            "`null` when the source is not the queue, and for an empty queue."
        )
    )
    queue_length: int | None = Field(
        description=(
            "The number of tracks in the queue; `null` when the source is not "
            "the queue."
        )
    )
    track: TrackSchema | None = Field(
        description="The track that is loaded; `null` when nothing is."
    )
