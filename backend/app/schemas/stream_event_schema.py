"""The events the backend sends on the event stream, as a client reads them.

Each is an unnamed message of the stream whose data is one JSON object,
``{"type": …, "payload": …}`` (``docs/api-design.md`` section 4). These
models are both what ``EventSender`` sends and what the OpenAPI document
shows of the stream (``app/api/sse_docs.py``).

An event names what changed and carries no state: a client fetches. The
models are frozen, so that equal events can be told apart from different
ones when they are coalesced.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

# Every field is in every message, the ones with a default included.
_EVENT = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)


class EmptyPayload(BaseModel):
    """An empty object: the event names nothing but its type."""

    model_config = _EVENT


class SpeakerPayload(BaseModel):
    """The speaker an event is about."""

    model_config = _EVENT

    speaker_id: str = Field(
        description=(
            "The speaker the event is about: its Sonos id (`RINCON_…`), the "
            "`id` of a speaker in `GET /api/speakers`."
        )
    )


class SpeakersChangedEvent(BaseModel):
    """The household's speakers changed: a speaker appeared or went, was
    renamed, or joined or left a group. A client fetches `GET /api/speakers`.
    """

    model_config = _EVENT

    type: Literal["speakers_changed"] = Field(
        default="speakers_changed", description="The event's type."
    )
    payload: EmptyPayload = Field(
        default_factory=EmptyPayload,
        description="Empty: the event is about the household, not one speaker.",
    )


class VolumeChangedEvent(BaseModel):
    """A speaker's volume or mute changed, or its group's. A client that shows
    the speaker fetches `GET /api/speakers/{speaker_id}`. A change of a
    group's volume is sent once for every speaker of the group.
    """

    model_config = _EVENT

    type: Literal["volume_changed"] = Field(
        default="volume_changed", description="The event's type."
    )
    payload: SpeakerPayload = Field(description="The speaker whose volume changed.")


class PlaybackChangedEvent(BaseModel):
    """What a speaker plays changed, or what can be done with it: the state,
    the track, the source, the allowed actions, shuffle or the place in the
    queue. A client that shows the speaker fetches
    `GET /api/speakers/{speaker_id}/playback`. Playback is a group's, so the
    event is sent once for every speaker of the group.
    """

    model_config = _EVENT

    type: Literal["playback_changed"] = Field(
        default="playback_changed", description="The event's type."
    )
    payload: SpeakerPayload = Field(description="The speaker whose playback changed.")


StreamEvent = Annotated[
    SpeakersChangedEvent | VolumeChangedEvent | PlaybackChangedEvent,
    Field(discriminator="type"),
]


class StreamMessage(RootModel[StreamEvent]):
    """One message of the event stream: the JSON object in its data. Its
    `type` tells which event it is.
    """
