"""Tests for the queue endpoints, on the in-memory household and Plex."""

from datetime import date

import pytest

from app.services.household_mirror import HouseholdMirror
from app.utils.artwork_id import plex_artwork_id
from tests.api.documents import json_schema, schema_of, undescribed
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import FakeSonosClient, app_track, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
KITCHEN = "RINCON_FAKE0000000301400"
NOWHERE = "RINCON_FAKE0000000991400"

QUEUE = "/api/speakers/{speaker_id}/queue"

# What the listing does not take as its query.
REFUSED_QUERIES = [
    "page=0",
    "page=two",
    "per_page=0",
    "per_page=101",
    "sort_by=title",
    "sort_by=",
    "sort_dir=desc",
    "sort_dir=up",
    "locate=first",
    "locate=",
]


@pytest.fixture
def household(
    sonos: FakeSonosClient, plex: FakePlexClient, mirror: HouseholdMirror
) -> FakeSonosClient:
    """The Garage with five Plex tracks of one album queued, the Lounge with
    a Plex track and a track of another service, the Kitchen with nothing,
    mirrored."""
    sonos.add_room(GARAGE, "Garage")
    sonos.add_room(LOUNGE, "Lounge")
    sonos.add_room(KITCHEN, "Kitchen")
    sonos.load_queue(GARAGE, [queue_track(number) for number in range(1, 6)])
    sonos.load_queue(LOUNGE, [queue_track(1), app_track(2)])
    plex.add_album("2001", [str(1000 + number) for number in range(1, 6)])
    mirror.start()
    sonos.settle()
    return sonos


def without_correlation_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "correlationId"}


def listing(client, speaker_id: str, query: str = "") -> dict:
    response = client.get(f"/api/speakers/{speaker_id}/queue?{query}")
    assert response.status_code == 200
    return response.get_json()


def clear(client, speaker_id: str):
    return client.delete(f"/api/speakers/{speaker_id}/queue")


def not_found(speaker_id: str) -> dict:
    return {
        "error": f"Speaker {speaker_id} was not found",
        "details": {"message": "The requested resource could not be found"},
        "code": "SPEAKER_NOT_FOUND",
    }


def failed(operation: str, cause: str) -> dict:
    return {
        "error": f"Cannot {operation} because external service failed: {cause}",
        "details": {"message": "External service request failed"},
        "code": "EXTERNAL_SERVICE_ERROR",
    }


def values(schema: dict) -> list:
    """The values a schema of a ``Literal`` allows."""
    return schema.get("enum") or [schema["const"]]


# The library the queueing tests queue from, without pictures: an artist of
# two albums, a playlist, an empty playlist and one too large to queue.
BAND, OLD_ALBUM, NEW_ALBUM = "10", "11", "12"
MIX, EMPTY, HUGE = "40", "41", "42"


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    plex.add_artist(BAND, "The Band", size=None)
    plex.add_album(
        OLD_ALBUM, ["111", "112"], None, artist=BAND, released=date(2001, 1, 1)
    )
    plex.add_album(
        NEW_ALBUM, ["121", "122"], None, artist=BAND, released=date(2010, 1, 1)
    )
    plex.add_playlist(MIX, "A Mix", 2, (1, 1), track_keys=["122", "111"])
    plex.add_playlist(EMPTY, "Empty", 0)
    plex.add_playlist(HUGE, "All Music", 12019, (1, 1))
    return plex


def queue_item(client, speaker_id: str, body: object):
    return client.post(f"/api/speakers/{speaker_id}/queue", json=body)


def track_ids(client, speaker_id: str) -> list[str | None]:
    return [entry["track_id"] for entry in listing(client, speaker_id)["data"]]


class TestListQueue:
    def test_a_page_of_the_queue(self, client, household, plex):
        artwork_id = plex_artwork_id(plex.read_track_picture("1001"))

        assert listing(client, LOUNGE) == {
            "data": [
                {
                    "position": 1,
                    "title": "Track 1",
                    "artist": "An Artist",
                    "album": "An Album",
                    "duration_ms": 181000,
                    "artwork_id": artwork_id,
                    "track_id": "1001",
                },
                {
                    "position": 2,
                    "title": "App Track 2",
                    "artist": "An App Artist",
                    "album": "An App Album",
                    "duration_ms": 202000,
                    "artwork_id": None,
                    "track_id": None,
                },
            ],
            "meta": {
                "page": 1,
                "per_page": 20,
                "total": 2,
                "total_pages": 1,
                "sort_by": "position",
                "sort_dir": "asc",
            },
            "current_position": 1,
        }

    def test_a_page_and_its_meta(self, client, household):
        answer = listing(client, GARAGE, "page=2&per_page=2")

        assert [entry["position"] for entry in answer["data"]] == [3, 4]
        assert [entry["track_id"] for entry in answer["data"]] == ["1003", "1004"]
        assert answer["meta"] == {
            "page": 2,
            "per_page": 2,
            "total": 5,
            "total_pages": 3,
            "sort_by": "position",
            "sort_dir": "asc",
        }

    def test_locate_answers_the_page_of_the_current_track(self, client, household):
        for _ in range(3):
            household.next(GARAGE)
        household.settle()

        answer = listing(client, GARAGE, "page=1&per_page=2&locate=current")

        assert answer["current_position"] == 4
        assert answer["meta"]["page"] == 2
        assert [entry["position"] for entry in answer["data"]] == [3, 4]

    def test_the_current_position_is_null_while_a_stream_plays(self, client, household):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        answer = listing(client, GARAGE)

        assert answer["current_position"] is None
        assert answer["meta"]["total"] == 5

    def test_an_empty_queue_is_one_empty_page(self, client, household):
        assert listing(client, KITCHEN) == {
            "data": [],
            "meta": {
                "page": 1,
                "per_page": 20,
                "total": 0,
                "total_pages": 1,
                "sort_by": "position",
                "sort_dir": "asc",
            },
            "current_position": None,
        }

    def test_every_speaker_of_a_group_answers_the_groups_queue(self, client, household):
        household.join(KITCHEN, GARAGE)
        household.settle()

        assert listing(client, KITCHEN) == listing(client, GARAGE)

    def test_an_entry_of_another_plex_server_has_neither_id(self, client, household):
        household.load_queue(GARAGE, [queue_track(1, machine_identifier="another")])

        (entry,) = listing(client, GARAGE)["data"]

        assert (entry["track_id"], entry["artwork_id"]) == (None, None)

    def test_the_artwork_id_is_answered_by_the_artwork_endpoint(
        self, client, household, artwork
    ):
        artwork_id = listing(client, GARAGE)["data"][0]["artwork_id"]

        jpeg = client.get(f"/api/artwork/{artwork_id}?size=100")
        lvgl = client.get(f"/api/artwork/{artwork_id}?size=100&format=lvgl")

        assert (jpeg.status_code, jpeg.mimetype) == (200, "image/jpeg")
        assert lvgl.status_code == 200
        assert len(lvgl.data) == 12 + 2 * 100 * 100

    def test_an_unknown_speaker_is_not_found(self, client, household):
        response = client.get(f"/api/speakers/{NOWHERE}/queue")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(NOWHERE)

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(self, client, household):
        household.silence_room(GARAGE)
        try:
            response = client.get(f"/api/speakers/{GARAGE}/queue")
        finally:
            household.answer_again(GARAGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == failed(
            f"read the queue of speaker {GARAGE}",
            "the speaker did not answer (ConnectTimeout)",
        )

    def test_plex_that_does_not_answer_is_a_bad_gateway(self, client, household, plex):
        plex.machine_identifier()
        plex.reachable = False

        response = client.get(f"/api/speakers/{GARAGE}/queue")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == failed(
            "read the pictures of 5 Plex tracks",
            "Plex did not answer (ConnectionError)",
        )

    def test_while_plex_has_never_answered_it_is_served_without_plex_ids(
        self, client, household, plex
    ):
        plex.reachable = False

        answer = listing(client, LOUNGE)

        assert [entry["title"] for entry in answer["data"]] == [
            "Track 1",
            "App Track 2",
        ]
        assert [entry["track_id"] for entry in answer["data"]] == [None, None]
        assert [entry["artwork_id"] for entry in answer["data"]] == [None, None]

    def test_a_failure_names_no_address_and_no_token(
        self, client, household, plex, test_app_settings
    ):
        plex.machine_identifier()
        plex.reachable = False
        household.silence_room(LOUNGE)

        try:
            responses = [
                client.get(f"/api/speakers/{speaker_id}/queue")
                for speaker_id in (GARAGE, LOUNGE)
            ]
        finally:
            household.answer_again(LOUNGE)

        for response in responses:
            assert response.status_code == 502
            text = response.get_data(as_text=True)
            assert "192.0.2." not in text
            assert "32400" not in text
            assert test_app_settings.plex_token not in text

    @pytest.mark.parametrize("query", REFUSED_QUERIES)
    def test_a_query_the_list_does_not_take_is_refused(self, client, household, query):
        response = client.get(f"/api/speakers/{GARAGE}/queue?{query}")

        assert response.status_code == 400

    def test_the_whole_query_spelled_out_is_the_default(self, client, household):
        assert listing(
            client, GARAGE, "page=1&per_page=20&sort_by=position&sort_dir=asc"
        ) == listing(client, GARAGE)


class TestClearQueue:
    def test_it_clears_the_queue(self, client, household):
        response = clear(client, GARAGE)
        household.settle()

        assert response.status_code == 204
        assert response.data == b""
        answer = listing(client, GARAGE)
        assert (answer["data"], answer["meta"]["total"]) == ([], 0)
        assert answer["current_position"] is None
        playback = client.get(f"/api/speakers/{GARAGE}/playback").get_json()
        assert (playback["state"], playback["queue_length"]) == ("stopped", 0)

    def test_it_clears_the_queue_of_the_speakers_group(self, client, household):
        household.join(KITCHEN, GARAGE)
        household.settle()

        assert clear(client, KITCHEN).status_code == 204

        assert listing(client, GARAGE)["meta"]["total"] == 0
        assert listing(client, LOUNGE)["meta"]["total"] == 2

    def test_what_changed_reaches_every_connection(self, client, household, connect):
        household.join(KITCHEN, GARAGE)
        household.settle()
        streams = [connect(), connect()]
        for stream in streams:
            stream.events()

        clear(client, GARAGE)
        household.settle()

        for stream in streams:
            changed = sorted(
                (event["type"], event["payload"]["speaker_id"])
                for event in stream.events()
            )
            assert sorted(set(changed)) == [
                ("playback_changed", GARAGE),
                ("playback_changed", KITCHEN),
                ("queue_changed", GARAGE),
                ("queue_changed", KITCHEN),
            ]

    def test_it_ends_the_groups_queueing_and_answers_once_the_queue_is_empty(
        self, client, household, library, tasks, hold, aside
    ):
        held = hold("add_many_to_queue")
        artist = queue_item(
            client, GARAGE, {"kind": "artist", "item_id": BAND, "action": "play"}
        ).get_json()["task_id"]
        held.wait()
        waiting = queue_item(
            client, GARAGE, {"kind": "album", "item_id": OLD_ALBUM, "action": "add"}
        ).get_json()["task_id"]
        tasks.started(waiting)

        response = aside.submit(clear, client, GARAGE)

        # The waiting task is ended at once; the Clear waits for the call
        # under way.
        assert tasks.finish(waiting)[-1]["event_type"] == "task_completed"
        assert not response.done()
        held.release()
        assert response.result(5.0).status_code == 204
        assert track_ids(client, GARAGE) == []
        assert tasks.finish(artist)[-1]["event_type"] == "task_completed"
        assert track_ids(client, GARAGE) == []

    def test_an_unknown_speaker_is_not_found(self, client, household):
        response = clear(client, NOWHERE)

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(NOWHERE)

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(self, client, household):
        household.silence_room(GARAGE)
        try:
            response = clear(client, GARAGE)
        finally:
            household.answer_again(GARAGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == failed(
            f"clear the queue of speaker {GARAGE}",
            "the speaker did not answer (ConnectTimeout)",
        )
        assert listing(client, GARAGE)["meta"]["total"] == 5


class TestQueueItem:
    @pytest.fixture(autouse=True)
    def items(self, library: FakePlexClient) -> FakePlexClient:
        return library

    def test_it_starts_a_task_that_plays_the_item(self, client, household, tasks):
        response = queue_item(
            client, GARAGE, {"kind": "album", "item_id": OLD_ALBUM, "action": "play"}
        )

        assert response.status_code == 202
        started = response.get_json()
        assert sorted(started) == ["status", "task_id"]
        assert started["status"] == "pending"
        events = tasks.finish(started["task_id"])
        assert [event["event_type"] for event in events] == [
            "task_started",
            "task_completed",
        ]
        assert events[-1]["data"] == {}
        household.settle()
        assert track_ids(client, GARAGE) == ["111", "112"]
        playback = client.get(f"/api/speakers/{GARAGE}/playback").get_json()
        assert (playback["state"], playback["shuffle"]) == ("playing", False)
        assert playback["queue_position"] == 1

    @pytest.mark.parametrize(
        ("kind", "item_id", "action", "queued"),
        [
            ("artist", BAND, "play", ["121", "122", "111", "112"]),
            ("artist", BAND, "shuffle", ["111", "112", "121", "122"]),
            ("artist", BAND, "add", [*(f"100{n}" for n in range(1, 6)), "121", "122", "111", "112"]),
            ("album", NEW_ALBUM, "play", ["121", "122"]),
            ("album", NEW_ALBUM, "shuffle", ["121", "122"]),
            ("album", NEW_ALBUM, "add", [*(f"100{n}" for n in range(1, 6)), "121", "122"]),
            ("playlist", MIX, "play", ["122", "111"]),
            ("playlist", MIX, "shuffle", ["111", "122"]),
            ("playlist", MIX, "add", [*(f"100{n}" for n in range(1, 6)), "122", "111"]),
            ("track", "112", "play", ["112"]),
            ("track", "112", "add", [*(f"100{n}" for n in range(1, 6)), "112"]),
        ],
    )
    def test_every_item_takes_its_actions(
        self, client, household, tasks, kind, item_id, action, queued
    ):
        response = queue_item(
            client, GARAGE, {"kind": kind, "item_id": item_id, "action": action}
        )

        assert response.status_code == 202
        events = tasks.finish(response.get_json()["task_id"])
        assert events[-1]["event_type"] == "task_completed"
        household.settle()
        listed = track_ids(client, GARAGE)
        if action == "shuffle":
            assert sorted(listed) == queued
        else:
            assert listed == queued
        playback = client.get(f"/api/speakers/{GARAGE}/playback").get_json()
        assert playback["shuffle"] is (action == "shuffle")
        assert playback["state"] == ("stopped" if action == "add" else "playing")

    def test_an_artist_plays_oldest_first_on_request(self, client, household, tasks):
        body = {
            "kind": "artist",
            "item_id": BAND,
            "action": "play",
            "order": "oldest_first",
        }

        tasks.finish(queue_item(client, GARAGE, body).get_json()["task_id"])

        household.settle()
        assert track_ids(client, GARAGE) == ["111", "112", "121", "122"]

    def test_each_step_reaches_every_connection(
        self, client, household, tasks, connect
    ):
        household.join(KITCHEN, GARAGE)
        household.settle()
        streams = [connect(), connect()]
        for stream in streams:
            stream.events()

        response = queue_item(
            client, KITCHEN, {"kind": "artist", "item_id": BAND, "action": "play"}
        )
        task_id = response.get_json()["task_id"]
        tasks.finish(task_id)
        household.settle()

        for stream in streams:
            events = stream.events()
            changed = {
                (event["type"], event["payload"]["speaker_id"])
                for event in events
                if event["type"] != "task_event"
            }
            assert {
                ("queue_changed", GARAGE),
                ("queue_changed", KITCHEN),
                ("playback_changed", GARAGE),
                ("playback_changed", KITCHEN),
            } <= changed
            reported = [
                event["payload"]["event_type"]
                for event in events
                if event["type"] == "task_event"
                and event["payload"]["task_id"] == task_id
            ]
            assert reported == ["task_started", "task_completed"]

    def test_a_failed_task_reaches_every_connection_in_the_clients_words(
        self, client, household, tasks, connect
    ):
        household.plex_linked = False
        stream = connect()

        response = queue_item(
            client, GARAGE, {"kind": "album", "item_id": OLD_ALBUM, "action": "play"}
        )
        task_id = response.get_json()["task_id"]
        failed_event = tasks.finish(task_id)[-1]

        assert response.status_code == 202
        assert failed_event["event_type"] == "task_failed"
        assert failed_event["data"] == {
            "error": (
                f"Cannot add Plex album {OLD_ALBUM} to the queue of speaker "
                f"{GARAGE} because external service failed: the speaker lists "
                "no Plex service"
            )
        }
        (seen,) = [
            event["payload"]
            for event in stream.events()
            if event["type"] == "task_event"
            and event["payload"]["event_type"] == "task_failed"
        ]
        assert seen["data"] == failed_event["data"]
        # A failed play leaves the queue as far as it got.
        household.settle()
        assert listing(client, GARAGE)["meta"]["total"] == 0

    def test_an_unknown_speaker_is_not_found(self, client, household):
        response = queue_item(
            client, NOWHERE, {"kind": "album", "item_id": OLD_ALBUM, "action": "play"}
        )

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(NOWHERE)

    @pytest.mark.parametrize(
        ("kind", "item_id", "named"),
        [
            ("artist", "99", "Artist"),
            ("album", BAND, "Album"),
            ("track", OLD_ALBUM, "Track"),
            ("playlist", "99", "Playlist"),
        ],
    )
    def test_an_item_plex_does_not_have_is_not_found(
        self, client, household, kind, item_id, named
    ):
        response = queue_item(
            client, GARAGE, {"kind": kind, "item_id": item_id, "action": "add"}
        )

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == {
            "error": f"{named} {item_id} was not found",
            "details": {"message": "The requested resource could not be found"},
            "code": "ITEM_NOT_FOUND",
        }
        assert listing(client, GARAGE)["meta"]["total"] == 5

    @pytest.mark.parametrize(
        ("playlist", "cause"),
        [
            (EMPTY, "it holds no tracks"),
            (
                HUGE,
                "it holds 12019 tracks, more than the 1000 a playlist may hold "
                "to be queued",
            ),
        ],
    )
    def test_a_playlist_that_cannot_be_queued_is_a_conflict(
        self, client, household, playlist, cause
    ):
        response = queue_item(
            client, GARAGE, {"kind": "playlist", "item_id": playlist, "action": "play"}
        )

        assert response.status_code == 409
        body = without_correlation_id(response.get_json())
        assert body["error"] == f"Cannot queue playlist {playlist} because {cause}"
        assert body["code"] == "PLAYLIST_NOT_PLAYABLE"
        assert listing(client, GARAGE)["meta"]["total"] == 5

    def test_plex_that_does_not_answer_is_a_bad_gateway(
        self, client, household, plex, test_app_settings
    ):
        plex.reachable = False

        response = queue_item(
            client, GARAGE, {"kind": "album", "item_id": OLD_ALBUM, "action": "play"}
        )

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == failed(
            f"read Plex album {OLD_ALBUM}", "Plex did not answer (ConnectionError)"
        )
        text = response.get_data(as_text=True)
        assert "192.0.2." not in text
        assert test_app_settings.plex_token not in text

    @pytest.mark.parametrize(
        "body",
        [
            None,
            [],
            {},
            {"item_id": OLD_ALBUM, "action": "play"},
            {"kind": "album", "action": "play"},
            {"kind": "album", "item_id": OLD_ALBUM},
            {"kind": "song", "item_id": OLD_ALBUM, "action": "play"},
            {"kind": "album", "item_id": 11, "action": "play"},
            {"kind": "album", "item_id": OLD_ALBUM, "action": "replace"},
            {"kind": "album", "item_id": OLD_ALBUM, "action": "play", "order": "new"},
            {"kind": "album", "item_id": OLD_ALBUM, "action": "play", "at": 1},
            {"kind": "track", "item_id": "112", "action": "shuffle"},
        ],
    )
    def test_a_body_the_route_does_not_take_is_refused(self, client, household, body):
        response = queue_item(client, GARAGE, body)

        assert response.status_code == 400
        assert listing(client, GARAGE)["meta"]["total"] == 5


class TestQueueDocument:
    @pytest.fixture
    def path(self, openapi_document) -> dict:
        return openapi_document["paths"][QUEUE]

    def test_the_queue_is_read_queued_on_and_cleared(self, path):
        assert sorted(path) == ["delete", "get", "post"]

    def test_the_listing_says_what_it_serves(self, path):
        operation = path["get"]

        assert operation["summary"] == "The queue of the speaker's group."
        description = " ".join(operation["description"].split())
        for part in (
            "A queue is a group's",
            "read from the speaker at every request",
            "with shuffle on, in the order in which it plays",
            "`locate=current`",
            "`queue_changed`",
            "`playback_changed`",
        ):
            assert part in description

    def test_the_listing_takes_the_paging_and_locate(self, openapi_document, path):
        query = {
            parameter["name"]: schema_of(openapi_document, parameter["schema"])
            for parameter in path["get"]["parameters"]
            if parameter["in"] == "query"
        }

        assert sorted(query) == ["locate", "page", "per_page", "sort_by", "sort_dir"]
        assert values(query["sort_by"]) == ["position"]
        assert query["sort_by"]["default"] == "position"
        assert values(query["sort_dir"]) == ["asc"]
        assert query["sort_dir"]["default"] == "asc"
        assert (query["per_page"]["minimum"], query["per_page"]["maximum"]) == (1, 100)
        (locate,) = [
            part for part in query["locate"]["anyOf"] if part != {"type": "null"}
        ]
        assert values(locate) == ["current"]
        assert query["locate"]["default"] is None
        for name, schema in query.items():
            assert schema.get("description"), name

    def test_the_listing_answers_a_page_of_described_entries(
        self, openapi_document, path
    ):
        page = json_schema(openapi_document, path["get"]["responses"]["200"])

        assert sorted(page["properties"]) == ["current_position", "data", "meta"]
        assert sorted(page["required"]) == ["current_position", "data", "meta"]
        assert undescribed(openapi_document, page) == []
        entry = schema_of(openapi_document, page["properties"]["data"]["items"])
        fields = [
            "album",
            "artist",
            "artwork_id",
            "duration_ms",
            "position",
            "title",
            "track_id",
        ]
        assert sorted(entry["properties"]) == fields
        assert sorted(entry["required"]) == fields
        assert (
            "`GET /api/artwork/{artwork_id}`"
            in (entry["properties"]["artwork_id"]["description"])
        )

    @pytest.mark.parametrize(
        ("method", "statuses"),
        [
            ("get", ["200", "400", "404", "502"]),
            ("post", ["202", "400", "404", "409", "502"]),
            ("delete", ["204", "404", "502"]),
        ],
    )
    def test_every_error_status_is_documented_with_the_envelope(
        self, path, method, statuses
    ):
        responses = path[method]["responses"]

        assert sorted(responses) == statuses
        for status in statuses[1:]:
            schema = responses[status]["content"]["application/json"]["schema"]
            assert "ApiErrorResponseSchema" in schema["$ref"]
            assert responses[status]["description"]
        assert "`SPEAKER_NOT_FOUND`" in responses["404"]["description"]

    def test_clear_says_what_changes(self, path):
        operation = path["delete"]

        assert operation["summary"] == "Clear the queue of the speaker's group."
        assert "requestBody" not in operation
        assert "content" not in operation["responses"]["204"]
        description = " ".join(operation["description"].split())
        assert "A queueing task still running on the group is ended" in description
        assert "once the queue is empty" in description
        for event in ("`queue_changed`", "`playback_changed`"):
            assert event in description

    def test_queueing_says_what_it_does(self, path):
        operation = path["post"]

        assert operation["summary"] == (
            "Play, shuffle or add an item of the library on the speaker's group."
        )
        description = " ".join(operation["description"].split())
        for part in (
            "`play` and `shuffle` replace the queue",
            "`add` appends and changes nothing else",
            "release order",
            "`task_id`",
            "`task_event`",
            "`task_completed`",
            "`task_failed`",
            "`queue_changed`",
            "as far as it got",
        ):
            assert part in description
        responses = operation["responses"]
        assert "`ITEM_NOT_FOUND`" in responses["404"]["description"]
        assert "`PLAYLIST_NOT_PLAYABLE`" in responses["409"]["description"]
        assert "track" in responses["400"]["description"]

    def test_queueing_takes_a_described_item(self, openapi_document, path):
        body = path["post"]["requestBody"]
        schema = json_schema(openapi_document, body)

        assert body["required"] is True
        assert sorted(schema["properties"]) == ["action", "item_id", "kind", "order"]
        assert sorted(schema["required"]) == ["action", "item_id", "kind"]
        assert undescribed(openapi_document, schema) == []
        fields = {
            name: schema_of(openapi_document, field)
            for name, field in schema["properties"].items()
        }
        assert values(fields["kind"]) == ["artist", "album", "track", "playlist"]
        assert values(fields["action"]) == ["play", "shuffle", "add"]
        assert values(fields["order"]) == ["newest_first", "oldest_first"]
        assert fields["order"]["default"] == "newest_first"
        assert fields["item_id"]["type"] == "string"

    def test_queueing_answers_the_started_task(self, openapi_document, path):
        accepted = path["post"]["responses"]["202"]
        started = json_schema(openapi_document, accepted)

        assert sorted(started["properties"]) == ["status", "task_id"]
        assert "`task_event`" in accepted["description"]

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_the_speaker_is_described(self, path, method):
        (speaker_id,) = [
            parameter
            for parameter in path[method]["parameters"]
            if parameter["in"] == "path"
        ]
        assert speaker_id["name"] == "speaker_id"
        assert "its Sonos id" in speaker_id["description"]
