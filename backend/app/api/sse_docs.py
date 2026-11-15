"""The event stream in the OpenAPI document.

The stream's route is the template's (``app/api/sse.py``) and carries no
schema, so the generated document shows its path and nothing of what arrives
on it. The document is the contract the app's clients are built from, so this
module completes it: how a client reads the stream, and each event the app
sends, with the schema of its payload.
"""

from typing import Any

from app.schemas.stream_event_schema import StreamMessage

STREAM_PATH = "/api/sse/stream"

_SUMMARY = "The event stream."

_DESCRIPTION = """\
Server-sent events that tell a client what changed. An event names what \
changed and carries no state: the client fetches the state it shows.

How a client reads the stream:

- It opens the stream with a `request_id` of its own making. The answer is \
`200` with `text/event-stream`, and stays open.
- The first event is named `ready` and has empty data. From then on the \
client is connected, and it fetches what it shows: the stream has no event \
ids and no replay, so this holds for the first connection and for every \
reconnection.
- Every event after it is an unnamed message whose data is one JSON object, \
`{"type": …, "payload": …}`. The schema of the `200` answer is that object.
- A comment line, `: heartbeat`, arrives while the stream is idle.
- A request the backend refuses is answered `200` as well, with one event \
named `rejected` whose data is `{"status": 400, "message": "BAD REQUEST"}`, \
and the stream ends. That is a `request_id` that is missing or holds a colon.

Every connection receives every event; there is nothing to subscribe to. A \
client ignores the events of the speakers it does not show, and a `type` it \
does not know.

| `type` | `payload` | A client fetches |
|---|---|---|
| `speakers_changed` | `{}` | `GET /api/speakers`: a speaker appeared or \
went, was renamed, or joined or left a group. |
| `volume_changed` | `{"speaker_id": …}` | `GET /api/speakers/{speaker_id}` |
| `playback_changed` | `{"speaker_id": …}` | \
`GET /api/speakers/{speaker_id}/playback` |

An event about a group, its volume or what it plays, is sent once for every \
speaker of the group, so a client follows the one speaker it shows. The \
backend sends at most one event of a type for a speaker in 250 ms, and always \
the last one: a burst of changes arrives as one or two events.
"""

_REQUEST_ID = {
    "name": "request_id",
    "in": "query",
    "required": True,
    "description": (
        "The client's own name for its stream: any text without a colon, such "
        "as a UUID. A new stream with the same `request_id` replaces the old "
        "one, which ends."
    ),
    "schema": {"type": "string"},
}


def describe_event_stream(document: dict[str, Any]) -> None:
    """Add the event stream and its events to an OpenAPI document."""
    message = StreamMessage.model_json_schema(
        mode="serialization", ref_template="#/components/schemas/{model}"
    )
    document["components"]["schemas"].update(message.pop("$defs"))
    document["components"]["schemas"][StreamMessage.__name__] = message

    operation = document["paths"][STREAM_PATH]["get"]
    operation["summary"] = _SUMMARY
    operation["description"] = _DESCRIPTION
    operation["parameters"] = [_REQUEST_ID]
    operation["responses"] = {
        "200": {
            "description": (
                "The stream: `ready`, then one message for every event. The "
                "schema is that of a message's data."
            ),
            "content": {
                "text/event-stream": {
                    "schema": {
                        "$ref": f"#/components/schemas/{StreamMessage.__name__}"
                    }
                }
            },
        }
    }
