"""Tests for the event stream's place in the OpenAPI document.

The document is the contract the app's clients are built from
(``docs/change-discipline.md``): it must say how a client reads the stream
and name every event the app sends, and what it says must be what the stream
does.
"""

import pytest

from app.schemas.stream_event_schema import (
    FavouritesChangedEvent,
    PlaybackChangedEvent,
    QueueChangedEvent,
    SpeakerPayload,
    SpeakersChangedEvent,
    StreamMessage,
    TaskStreamEvent,
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
            "`favourites_changed`",
            "`task_event`",
        ]
        assert "`GET /api/speakers`" in rows[1]
        assert "`GET /api/speakers/{speaker_id}`" in rows[2]
        assert "`GET /api/speakers/{speaker_id}/playback`" in rows[3]
        assert "`GET /api/speakers/{speaker_id}/queue`" in rows[4]
        assert "`GET /api/favourites`" in rows[5]
        assert "Nothing" in rows[6]
        assert "`POST /api/speakers/{speaker_id}/queue`" in rows[6]

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
            "favourites_changed",
            "playback_changed",
            "queue_changed",
            "speakers_changed",
            "task_event",
            "volume_changed",
        ]
        assert len(message["oneOf"]) == 6

    @pytest.mark.parametrize(
        ("event_type", "payload_properties", "fetched"),
        [
            ("speakers_changed", [], "`GET /api/speakers`"),
            ("volume_changed", ["speaker_id"], "`GET /api/speakers/{speaker_id}`"),
            (
                "playback_changed",
                ["speaker_id"],
                "`GET /api/speakers/{speaker_id}/playback`",
            ),
            ("queue_changed", ["speaker_id"], "`GET /api/speakers/{speaker_id}/queue`"),
            ("favourites_changed", [], "`GET /api/favourites`"),
        ],
    )
    def test_each_event_has_its_payloads_schema_with_every_field_described(
        self, document, event_type, payload_properties, fetched
    ):
        responses = document["paths"]["/api/sse/stream"]["get"]["responses"]
        message = schema(document, responses["200"]["content"]["text/event-stream"]["schema"])
        event = schema(document, {"$ref": message["discriminator"]["mapping"][event_type]})

        assert "A client" in event["description"]
        assert f"fetches {fetched}" in " ".join(event["description"].split())
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

    def test_the_task_event_is_the_tasks_event_with_every_field_described(
        self, document
    ):
        responses = document["paths"]["/api/sse/stream"]["get"]["responses"]
        message = schema(document, responses["200"]["content"]["text/event-stream"]["schema"])
        event = schema(document, {"$ref": message["discriminator"]["mapping"]["task_event"]})

        assert "`task_id`" in event["description"]
        assert sorted(event["required"]) == ["payload", "type"]
        assert event["properties"]["type"]["const"] == "task_event"
        payload = schema(document, event["properties"]["payload"])
        fields = ["data", "event_type", "task_id", "timestamp"]
        assert sorted(payload["properties"]) == fields
        assert sorted(payload["required"]) == fields
        for name, field in payload["properties"].items():
            assert field.get("description"), name
        event_type = schema(document, payload["properties"]["event_type"])
        assert sorted(event_type["enum"]) == [
            "progress_update",
            "task_completed",
            "task_failed",
            "task_started",
        ]
        data = payload["properties"]["data"]["description"]
        for part in ("`task_completed`", "`task_failed`", '`{"error": …}`'):
            assert part in data

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

    def test_a_tasks_events_are_sent_as_the_document_says(
        self, client, sonos, plex, mirror, tasks
    ):
        sonos.add_room(GARAGE, "Garage")
        plex.add_album("11", ["111"], None)
        mirror.start()
        sonos.settle()

        response = client.get("/api/sse/stream?request_id=tasks", buffered=False)
        try:
            frames = response.iter_encoded()
            assert next(frames) == b"event: ready\ndata:\n\n"
            started = client.post(
                f"/api/speakers/{GARAGE}/queue",
                json={"kind": "album", "item_id": "11", "action": "add"},
            ).get_json()
            tasks.finish(started["task_id"])
            reported = []
            while len(reported) < 2:
                frame = next(frames).decode()
                event = StreamMessage.model_validate_json(
                    frame.removeprefix("data: ")
                ).root
                if isinstance(event, TaskStreamEvent):
                    reported.append(event.payload)
        finally:
            response.close()

        assert [payload.task_id for payload in reported] == [started["task_id"]] * 2
        assert [payload.event_type for payload in reported] == [
            "task_started",
            "task_completed",
        ]
        assert reported[1].data == {}

    def test_a_change_of_the_favourites_is_sent_as_the_document_says(
        self, client, plex, sender
    ):
        plex.add_artist("5001", "Radiohead", size=None)

        response = client.get("/api/sse/stream?request_id=favourites", buffered=False)
        try:
            frames = response.iter_encoded()
            assert next(frames) == b"event: ready\ndata:\n\n"
            marked = client.post(
                "/api/favourites", json={"kind": "artist", "item_id": "5001"}
            )
            assert marked.status_code == 201
            frame = next(frames).decode()
        finally:
            response.close()

        assert frame == 'data: {"type":"favourites_changed","payload":{}}\n\n'
        event = StreamMessage.model_validate_json(frame.removeprefix("data: ")).root
        assert event == FavouritesChangedEvent()

    def test_a_request_without_a_request_id_is_rejected_as_documented(self, client):
        response = client.get("/api/sse/stream")

        assert response.status_code == 200
        assert response.get_data(as_text=True) == (
            'event: rejected\ndata: {"status":400,"message":"BAD REQUEST"}\n\n'
        )
