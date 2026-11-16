"""The events the backend sends on the event stream, as a client reads them.

Each is an unnamed message of the stream whose data is one JSON object,
``{"type": …, "payload": …}`` (``docs/api-design.md`` section 4). These
models are both what is sent, ``StreamEvent`` by ``EventSender`` and
``TaskStreamEvent`` by the template's task service, and what the OpenAPI
document shows of the stream (``app/api/sse_docs.py``).

An event of ``StreamEvent`` names what changed and carries no state: a client
fetches. Those models are frozen, so that equal events can be told apart
from different ones when they are coalesced.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.schemas.task_schema import TaskEvent

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


class QueueChangedEvent(BaseModel):
    """The queue a speaker plays from changed: its tracks or their order,
    shuffle included, or the speaker joined or left a group and plays from
    another queue. A client that shows the speaker's queue fetches
    `GET /api/speakers/{speaker_id}/queue`. A queue is a group's, so the
    event is sent once for every speaker of the group.
    """

    model_config = _EVENT

    type: Literal["queue_changed"] = Field(
        default="queue_changed", description="The event's type."
    )
    payload: SpeakerPayload = Field(description="The speaker whose queue changed.")


class FavouritesChangedEvent(BaseModel):
    """The favourites changed: one was marked or unmarked, or a listing found
    one whose item Plex had moved and brought it up to date, or removed it
    as a second mark of that item. A client that shows the favourites
    fetches `GET /api/favourites`, and one that shows an artist, an album or
    a playlist fetches it again, for its `favourite_id`.
    """

    model_config = _EVENT

    type: Literal["favourites_changed"] = Field(
        default="favourites_changed", description="The event's type."
    )
    payload: EmptyPayload = Field(
        default_factory=EmptyPayload,
        description="Empty: the event is about the favourites, not one speaker.",
    )


class TaskEventPayload(TaskEvent):
    """What a task reports: that it started, its progress, that it completed
    or that it failed."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    data: dict[str, Any] | None = Field(
        None,
        description=(
            "What the event carries, by `event_type`: `null` for "
            '`task_started`; `{"text": …, "value": …}` for `progress_update`, '
            "the value from 0 to 1; the task's result for `task_completed`, an "
            'empty object for a queueing task; `{"error": …}` for '
            "`task_failed`, the failure in words a client can show."
        ),
    )


class TaskStreamEvent(BaseModel):
    """A task a client started, such as the queueing of
    `POST /api/speakers/{speaker_id}/queue`, started, made progress,
    completed or failed. A client fetches nothing for it: it watches for the
    `task_id` it was given, and ignores the events of other tasks.
    """

    model_config = _EVENT

    type: Literal["task_event"] = Field(
        default="task_event", description="The event's type."
    )
    payload: TaskEventPayload = Field(description="The task's event.")


_CHANGE_EVENTS = (
    SpeakersChangedEvent
    | VolumeChangedEvent
    | PlaybackChangedEvent
    | QueueChangedEvent
    | FavouritesChangedEvent
)

# What EventSender sends.
StreamEvent = Annotated[_CHANGE_EVENTS, Field(discriminator="type")]


class StreamMessage(
    RootModel[Annotated[_CHANGE_EVENTS | TaskStreamEvent, Field(discriminator="type")]]
):
    """One message of the event stream: the JSON object in its data. Its
    `type` tells which event it is.
    """
