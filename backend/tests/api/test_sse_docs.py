"""Tests for the event stream's place in the OpenAPI document.

The document is the contract the app's clients are built from
(``docs/change-discipline.md``): it must say how a client reads the stream
and name every event the app sends, and what it says must be what the stream
does.
"""

import pytest

from app.schemas.stream_event_schema import (
    PlaybackChangedEvent,
    QueueChangedEvent,
    SpeakerPayload,
    SpeakersChangedEvent,
    StreamMessage,
    VolumeChangedEvent,
)
from tests.fakes.sonos_client import queue_track

GARAGE = "RINCON_FAKE0000000101400"


@pytest.fixture
def document(client) -> dict:
    response = client.get("/api/docs/openapi.json")
    assert response.status_code == 200
    return response.get_json()


def schema(document: dict, reference: dict) -> dict:
    """The schema a ``$ref`` of the document names."""
    prefix = "#/components/schemas/"
    assert reference["$ref"].startswith(prefix)
    return document["components"]["schemas"][reference["$ref"].removeprefix(prefix)]


class TestEventStreamDocument:
    def test_the_stream_says_how_a_client_reads_it(self, document):
        operation = document["paths"]["/api/sse/stream"]["get"]

        assert operation["summary"] == "The event stream."
        description = operation["description"]
        for part in (
            "named `ready`",
            '`{"type": …, "payload": …}`',
            "`: heartbeat`",
            "no event ids and no replay",
            "named `rejected`",
            "Every connection receives every event",
            "at most one event of a type for a speaker in 250 ms",
        ):
            assert part in description

    def test_the_stream_names_what_a_client_fetches_on_each_event(self, document):
        description = document["paths"]["/api/sse/stream"]["get"]["description"]

        rows = [line for line in description.splitlines() if line.startswith("| `")]
        assert [row.split("|")[1].strip() for row in rows] == [
            "`type`",
            "`speakers_changed`",
            "`volume_changed`",
            "`playback_changed`",
            "`queue_changed`",
        ]
        assert "`GET /api/speakers`" in rows[1]
        assert "`GET /api/speakers/{speaker_id}`" in rows[2]
        assert "`GET /api/speakers/{speaker_id}/playback`" in rows[3]
        assert "`GET /api/speakers/{speaker_id}/queue`" in rows[4]

    def test_the_request_id_is_a_required_query_parameter(self, document):
        (parameter,) = document["paths"]["/api/sse/stream"]["get"]["parameters"]

        assert parameter["name"] == "request_id"
        assert parameter["in"] == "query"
        assert parameter["required"] is True
        assert parameter["schema"] == {"type": "string"}
        assert "colon" in parameter["description"]

    def test_the_answer_is_an_event_stream_of_every_event(self, document):
        responses = document["paths"]["/api/sse/stream"]["get"]["responses"]

        assert list(responses) == ["200"]
        message = schema(document, responses["200"]["content"]["text/event-stream"]["schema"])
        assert message["description"]
        assert message["discriminator"]["propertyName"] == "type"
        assert sorted(message["discriminator"]["mapping"]) == [
            "playback_changed",
            "queue_changed",
            "speakers_changed",
            "volume_changed",
        ]
        assert len(message["oneOf"]) == 4

    @pytest.mark.parametrize(
        ("event_type", "payload_properties"),
        [
            ("speakers_changed", []),
            ("volume_changed", ["speaker_id"]),
            ("playback_changed", ["speaker_id"]),
            ("queue_changed", ["speaker_id"]),
        ],
    )
    def test_each_event_has_its_payloads_schema_with_every_field_described(
        self, document, event_type, payload_properties
    ):
        responses = document["paths"]["/api/sse/stream"]["get"]["responses"]
        message = schema(document, responses["200"]["content"]["text/event-stream"]["schema"])
        event = schema(document, {"$ref": message["discriminator"]["mapping"][event_type]})

        assert "A client" in event["description"]
        assert "fetches `GET /api/speakers" in " ".join(event["description"].split())
        assert sorted(event["required"]) == ["payload", "type"]
        assert event["properties"]["type"]["const"] == event_type
        assert event["properties"]["type"]["description"]
        assert event["properties"]["payload"]["description"]

        payload = schema(document, event["properties"]["payload"])
        assert payload["type"] == "object"
        assert payload["description"]
        assert list(payload.get("properties", {})) == payload_properties
        assert payload.get("required", []) == payload_properties
        for field in payload.get("properties", {}).values():
            assert field["type"] == "string"
            assert field["description"]

    def test_the_document_is_the_same_each_time_it_is_asked_for(self, client, document):
        assert client.get("/api/docs/openapi.json").get_json() == document

    def test_the_rest_of_the_document_is_as_generated(self, document):
        assert document["paths"]["/metrics"]["get"]["summary"]
        assert "BearerAuth" in document["components"]["securitySchemes"]


class TestEventStream:
    def test_the_stream_sends_what_the_document_says(self, client, sonos, mirror):
        sonos.add_room(GARAGE, "Garage")
        sonos.load_queue(GARAGE, [queue_track(1), queue_track(2)])
        mirror.start()
        sonos.settle()

        response = client.get("/api/sse/stream?request_id=documented", buffered=False)
        try:
            frames = response.iter_encoded()
            assert response.status_code == 200
            assert response.mimetype == "text/event-stream"
            assert next(frames) == b"event: ready\ndata:\n\n"

            sonos.rename_room(GARAGE, "Workshop")
            sonos.settle()
            sonos.set_volume(GARAGE, 30)
            sonos.settle()
            sonos.play(GARAGE)
            sonos.settle()
            sonos.set_shuffle(GARAGE, True)
            sonos.settle()
            received = [next(frames).decode() for _ in range(4)]
        finally:
            response.close()

        for frame in received:
            assert frame.startswith("data: ") and frame.endswith("\n\n")
        # In the order the stream held them: an event of a type sent less
        # than 250 ms before waits.
        events = {
            StreamMessage.model_validate_json(frame.removeprefix("data: ")).root
            for frame in received
        }
        assert events == {
            SpeakersChangedEvent(),
            VolumeChangedEvent(payload=SpeakerPayload(speaker_id=GARAGE)),
            PlaybackChangedEvent(payload=SpeakerPayload(speaker_id=GARAGE)),
            QueueChangedEvent(payload=SpeakerPayload(speaker_id=GARAGE)),
        }
        speaker = '{"speaker_id": "' + GARAGE + '"}'
        assert sorted(received) == [
            'data: {"type":"playback_changed","payload":' + speaker + "}\n\n",
            'data: {"type":"queue_changed","payload":' + speaker + "}\n\n",
            'data: {"type":"speakers_changed","payload":{}}\n\n',
            'data: {"type":"volume_changed","payload":' + speaker + "}\n\n",
        ]

    def test_a_request_without_a_request_id_is_rejected_as_documented(self, client):
        response = client.get("/api/sse/stream")

        assert response.status_code == 200
        assert response.get_data(as_text=True) == (
            'event: rejected\ndata: {"status":400,"message":"BAD REQUEST"}\n\n'
        )
