"""Tests for the playback endpoints, on the in-memory household."""

import logging
from dataclasses import replace
from typing import get_args

import pytest

from app.schemas.playback_schema import (
    PlaybackActionName,
    PlaybackSourceName,
    PlaybackStateName,
)
from app.services.household_mirror import (
    HouseholdMirror,
    PlaybackAction,
    PlaybackSource,
    PlaybackStatus,
)
from app.services.sonos_client import TransportState
from app.utils.artwork_id import plex_artwork_id, speaker_artwork_id
from tests.api.documents import json_schema, undescribed
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import FakeSonosClient, app_track, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
KITCHEN = "RINCON_FAKE0000000301400"
NOWHERE = "RINCON_FAKE0000000991400"

COMMANDS = ["play", "pause", "next", "previous"]

# What follows "Cannot" for each command.
OPERATIONS = {
    "play": "play on speaker {}",
    "pause": "pause speaker {}",
    "next": "skip to the next track on speaker {}",
    "previous": "go back to the previous track on speaker {}",
}


@pytest.fixture
def household(
    sonos: FakeSonosClient, plex: FakePlexClient, mirror: HouseholdMirror
) -> FakeSonosClient:
    """The Garage with three Plex tracks queued, the Lounge with two tracks
    of another service and the Kitchen with nothing, mirrored."""
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.add_room(LOUNGE, "Lounge", model="Playbase", volume=40)
    sonos.add_room(KITCHEN, "Kitchen")
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2), queue_track(3)])
    sonos.load_queue(LOUNGE, [app_track(1), app_track(2)])
    plex.add_album("2001", ["1001", "1002", "1003"])
    mirror.start()
    sonos.settle()
    return sonos


def without_correlation_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "correlationId"}


def playback(client, speaker_id: str) -> dict:
    response = client.get(f"/api/speakers/{speaker_id}/playback")
    assert response.status_code == 200
    return response.get_json()


def command(client, speaker_id: str, name: str):
    return client.post(f"/api/speakers/{speaker_id}/playback/{name}")


def not_available(name: str, speaker_id: str) -> dict:
    return {
        "error": (
            f"Cannot {OPERATIONS[name].format(speaker_id)} because the speaker "
            "does not allow it now"
        ),
        "details": {"message": "The requested operation cannot be performed"},
        "code": "ACTION_NOT_AVAILABLE",
    }


class TestGetPlayback:
    def test_a_queued_plex_track(self, client, household, plex):
        assert playback(client, GARAGE) == {
            "state": "stopped",
            "source": "queue",
            "actions": ["play", "pause", "next"],
            "shuffle": False,
            "queue_position": 1,
            "queue_length": 3,
            "track": {
                "title": "Track 1",
                "artist": "An Artist",
                "album": "An Album",
                "duration_ms": 181000,
                "artwork_id": plex_artwork_id(plex.read_track_picture("1001")),
                "track_id": "1001",
            },
        }

    def test_a_track_of_another_plex_server_is_not_from_plex(
        self, client, household, plex, artwork
    ):
        # The other server's track has a rating key the library has too.
        theirs = queue_track(1, machine_identifier="anotherserver")
        household.load_queue(GARAGE, [theirs])
        household.settle()

        track = playback(client, GARAGE)["track"]

        assert track == {
            "title": "Track 1",
            "artist": "An Artist",
            "album": "An Album",
            "duration_ms": 181000,
            "artwork_id": speaker_artwork_id(GARAGE, theirs.uri, theirs.picture),
            "track_id": None,
        }
        assert track["artwork_id"] != plex_artwork_id(plex.read_track_picture("1001"))
        response = client.get(f"/api/artwork/{track['artwork_id']}?size=100")
        assert response.status_code == 200
        assert response.mimetype == "image/jpeg"

    def test_the_artwork_id_is_answered_by_the_artwork_endpoint(
        self, client, household, artwork
    ):
        for speaker_id in (GARAGE, LOUNGE):
            artwork_id = playback(client, speaker_id)["track"]["artwork_id"]

            response = client.get(f"/api/artwork/{artwork_id}?size=400&format=lvgl")

            assert response.status_code == 200
            assert len(response.data) == 12 + 2 * 400 * 400

    def test_nothing_loaded(self, client, household):
        assert playback(client, KITCHEN) == {
            "state": "stopped",
            "source": "queue",
            "actions": [],
            "shuffle": False,
            "queue_position": None,
            "queue_length": 0,
            "track": None,
        }

    def test_a_radio_stream_started_elsewhere(self, client, household):
        household.start_stream(GARAGE, "Radio One", "An Artist - A Song")
        household.settle()

        assert playback(client, GARAGE) == {
            "state": "playing",
            "source": "radio",
            "actions": ["play", "pause"],
            "shuffle": False,
            "queue_position": None,
            "queue_length": None,
            "track": {
                "title": "An Artist - A Song",
                "artist": "Radio One",
                "album": None,
                "duration_ms": None,
                "artwork_id": None,
                "track_id": None,
            },
        }

    def test_a_track_that_is_not_from_plex(self, client, household):
        answer = playback(client, LOUNGE)

        assert answer["source"] == "queue"
        assert answer["track"]["title"] == "App Track 1"
        assert answer["track"]["track_id"] is None
        assert answer["track"]["artwork_id"] is not None

    def test_what_changes_elsewhere_is_served(self, client, household):
        household.set_shuffle(GARAGE, True)
        household.play(GARAGE)
        household.next(GARAGE)
        household.settle()

        answer = playback(client, GARAGE)

        assert (answer["state"], answer["shuffle"]) == ("playing", True)
        assert (answer["queue_position"], answer["queue_length"]) == (2, 3)
        assert answer["actions"] == ["play", "pause", "next", "previous"]
        # The second track of the play order: shuffle draws it.
        [second] = household.read_queue(GARAGE, 1, 1).tracks
        assert answer["track"]["title"] == second.title
        assert answer["track"]["track_id"] in ("1002", "1003")

        household.start_buffering(GARAGE)
        household.settle()
        assert playback(client, GARAGE)["state"] == "buffering"

    def test_every_speaker_of_a_group_answers_the_groups_playback(
        self, client, household
    ):
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert playback(client, LOUNGE) == playback(client, GARAGE)
        assert playback(client, LOUNGE)["track"]["track_id"] == "1001"

    def test_a_tracks_texts_are_nfc(self, client, household):
        household.load_queue(
            GARAGE,
            [replace(queue_track(1), title="Café", artist="Björk")],
        )
        household.settle()

        track = playback(client, GARAGE)["track"]

        assert (track["title"], track["artist"]) == ("Café", "Björk")

    def test_while_plex_is_down_it_is_served_without_artwork(
        self, client, household, plex
    ):
        plex.reachable = False

        answer = playback(client, GARAGE)

        assert answer["track"]["title"] == "Track 1"
        assert answer["track"]["artwork_id"] is None

    @pytest.mark.parametrize("speaker_id", [NOWHERE, "Garage"])
    def test_an_unknown_speaker_is_not_found(self, client, household, speaker_id):
        response = client.get(f"/api/speakers/{speaker_id}/playback")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == {
            "error": f"Speaker {speaker_id} was not found",
            "details": {"message": "The requested resource could not be found"},
            "code": "SPEAKER_NOT_FOUND",
        }

    def test_every_speaker_is_not_found_while_no_speaker_answers(
        self, client, sonos, mirror
    ):
        sonos.add_room(GARAGE, "Garage")
        sonos.reachable = False
        mirror.start()

        response = client.get(f"/api/speakers/{GARAGE}/playback")

        assert response.status_code == 404
        assert response.get_json()["code"] == "SPEAKER_NOT_FOUND"


class TestPlaybackCommands:
    def test_play_and_pause(self, client, household):
        played = command(client, GARAGE, "play")
        household.settle()

        assert (played.status_code, played.data) == (204, b"")
        assert playback(client, GARAGE)["state"] == "playing"

        paused = command(client, GARAGE, "pause")
        household.settle()

        assert (paused.status_code, paused.data) == (204, b"")
        assert playback(client, GARAGE)["state"] == "paused"

    def test_next_and_previous(self, client, household):
        skipped = command(client, GARAGE, "next")
        household.settle()

        assert (skipped.status_code, skipped.data) == (204, b"")
        answer = playback(client, GARAGE)
        assert (answer["queue_position"], answer["track"]["track_id"]) == (2, "1002")

        back = command(client, GARAGE, "previous")
        household.settle()

        assert (back.status_code, back.data) == (204, b"")
        answer = playback(client, GARAGE)
        assert (answer["queue_position"], answer["track"]["track_id"]) == (1, "1001")

    def test_pause_stops_a_radio_stream(self, client, household):
        household.start_stream(GARAGE, "Radio One")
        household.settle()
        assert "pause" in playback(client, GARAGE)["actions"]

        response = command(client, GARAGE, "pause")
        household.settle()

        assert response.status_code == 204
        assert household.read_playback(GARAGE).transport_state is TransportState.STOPPED
        assert playback(client, GARAGE)["state"] == "stopped"

    def test_what_is_not_from_plex_is_played_paused_and_skipped(
        self, client, household
    ):
        for name in ("play", "next", "pause"):
            assert command(client, LOUNGE, name).status_code == 204
            household.settle()

        answer = playback(client, LOUNGE)
        assert (answer["state"], answer["track"]["title"]) == ("paused", "App Track 2")

    def test_a_command_acts_on_the_speakers_group(self, client, household):
        household.join(LOUNGE, GARAGE)
        household.settle()

        assert command(client, LOUNGE, "play").status_code == 204
        assert command(client, LOUNGE, "next").status_code == 204
        household.settle()

        for speaker_id in (GARAGE, LOUNGE):
            answer = playback(client, speaker_id)
            assert (answer["state"], answer["queue_position"]) == ("playing", 2)

    def test_the_speakers_event_reaches_every_connection(
        self, client, household, connect
    ):
        streams = [connect(), connect()]
        for stream in streams:
            stream.events()

        command(client, GARAGE, "play")
        household.settle()

        for stream in streams:
            events = stream.events()
            assert events != []
            assert all(
                event
                == {"type": "playback_changed", "payload": {"speaker_id": GARAGE}}
                for event in events
            )

    @pytest.mark.parametrize("name", COMMANDS)
    def test_no_command_is_available_with_nothing_loaded(
        self, client, household, name
    ):
        response = command(client, KITCHEN, name)

        assert response.status_code == 409
        assert without_correlation_id(response.get_json()) == not_available(
            name, KITCHEN
        )

    def test_previous_is_not_available_at_the_first_track(self, client, household):
        response = command(client, GARAGE, "previous")

        assert response.status_code == 409
        assert without_correlation_id(response.get_json()) == not_available(
            "previous", GARAGE
        )
        assert household.read_playback(GARAGE).queue_position == 1

    def test_next_is_not_available_at_the_last_track(self, client, household):
        command(client, GARAGE, "next")
        household.settle()
        command(client, GARAGE, "next")
        household.settle()

        response = command(client, GARAGE, "next")

        assert response.status_code == 409
        assert without_correlation_id(response.get_json()) == not_available(
            "next", GARAGE
        )
        assert household.read_playback(GARAGE).queue_position == 3

    @pytest.mark.parametrize("name", ["next", "previous"])
    def test_a_radio_stream_is_not_skipped(self, client, household, name):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        response = command(client, GARAGE, name)

        assert response.status_code == 409
        assert response.get_json()["code"] == "ACTION_NOT_AVAILABLE"
        assert household.read_playback(GARAGE).transport_state is TransportState.PLAYING

    @pytest.mark.parametrize("name", COMMANDS)
    def test_an_unknown_speaker_is_not_found(self, client, household, name):
        response = command(client, NOWHERE, name)

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == {
            "error": f"Speaker {NOWHERE} was not found",
            "details": {"message": "The requested resource could not be found"},
            "code": "SPEAKER_NOT_FOUND",
        }

    @pytest.mark.parametrize("name", ["play", "pause", "next"])
    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(
        self, client, household, name
    ):
        household.silence_room(GARAGE)
        try:
            response = command(client, GARAGE, name)
        finally:
            household.answer_again(GARAGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == {
            "error": (
                f"Cannot {OPERATIONS[name].format(GARAGE)} because external "
                "service failed: the speaker did not answer (ConnectTimeout)"
            ),
            "details": {"message": "External service request failed"},
            "code": "EXTERNAL_SERVICE_ERROR",
        }

    def test_a_command_the_speaker_refuses_after_all_is_not_available(
        self, client, household
    ):
        # The queue ends at its first track, and the event that says so has
        # not arrived: the mirror still lists next, and the speaker answers
        # UPnP error 711.
        household.before_call = lambda speaker_id: household.load_queue(
            GARAGE, [queue_track(1)]
        )

        response = command(client, GARAGE, "next")

        assert response.status_code == 409
        assert without_correlation_id(response.get_json()) == not_available(
            "next", GARAGE
        )

    def test_a_command_a_stream_refuses_after_all_is_not_available(
        self, client, household
    ):
        # A stream starts, and the event that says so has not arrived: the
        # mirror still lists next, and the speaker answers UPnP error 701.
        household.before_call = lambda speaker_id: household.start_stream(
            GARAGE, "Radio One", "A Song"
        )

        response = command(client, GARAGE, "next")

        assert response.status_code == 409
        assert without_correlation_id(response.get_json()) == not_available(
            "next", GARAGE
        )

    @pytest.mark.parametrize("name", COMMANDS)
    def test_a_command_does_not_wait_for_plex(
        self, client, household, plex, caplog, name
    ):
        household.next(GARAGE)
        household.settle()
        plex.reachable = False

        with caplog.at_level(logging.WARNING, "app.services.household_mirror"):
            response = command(client, GARAGE, name)

        assert response.status_code == 204
        assert caplog.records == []

    @pytest.mark.parametrize("name", COMMANDS)
    def test_a_command_is_a_post(self, client, household, name):
        response = client.get(f"/api/speakers/{GARAGE}/playback/{name}")

        assert response.status_code == 405


class TestPlaybackDocument:
    @pytest.fixture
    def paths(self, openapi_document) -> dict:
        return openapi_document["paths"]

    def test_the_playback_state(self, openapi_document, paths):
        operation = paths["/api/speakers/{speaker_id}/playback"]["get"]

        assert operation["summary"] == (
            "What the speaker plays, and what can be done with it."
        )
        description = " ".join(operation["description"].split())
        for part in (
            "Playback is a group's",
            "no elapsed time and no repeat",
            "`playback_changed`",
            "until the speaker has reported the change",
        ):
            assert part in description
        assert sorted(operation["responses"]) == ["200", "404"]
        state = json_schema(openapi_document, operation["responses"]["200"])
        assert sorted(state["required"]) == sorted(state["properties"])
        assert sorted(state["properties"]) == [
            "actions",
            "queue_length",
            "queue_position",
            "shuffle",
            "source",
            "state",
            "track",
        ]
        assert undescribed(openapi_document, state) == []

    def test_the_track(self, openapi_document, paths):
        operation = paths["/api/speakers/{speaker_id}/playback"]["get"]
        state = json_schema(openapi_document, operation["responses"]["200"])

        track, null = state["properties"]["track"]["anyOf"]
        assert null == {"type": "null"}
        track = json_schema(
            openapi_document, {"content": {"application/json": {"schema": track}}}
        )
        assert sorted(track["required"]) == sorted(track["properties"])
        assert sorted(track["properties"]) == [
            "album",
            "artist",
            "artwork_id",
            "duration_ms",
            "title",
            "track_id",
        ]
        assert "`GET /api/artwork/{artwork_id}`" in (
            track["properties"]["artwork_id"]["description"]
        )

    def test_the_statuses_are_closed_and_the_mirrors(self, openapi_document, paths):
        operation = paths["/api/speakers/{speaker_id}/playback"]["get"]
        state = json_schema(openapi_document, operation["responses"]["200"])

        properties = state["properties"]
        assert properties["state"]["enum"] == [s.value for s in PlaybackStatus]
        assert properties["source"]["enum"] == [s.value for s in PlaybackSource]
        assert properties["actions"]["items"]["enum"] == [
            a.value for a in PlaybackAction
        ]
        assert list(get_args(PlaybackStateName)) == [s.value for s in PlaybackStatus]
        assert list(get_args(PlaybackSourceName)) == [s.value for s in PlaybackSource]
        assert list(get_args(PlaybackActionName)) == [a.value for a in PlaybackAction]

    @pytest.mark.parametrize(
        ("name", "summary"),
        [
            ("play", "Play or resume what the speaker's group has loaded."),
            ("pause", "Pause the speaker's group."),
            ("next", "Skip to the next track."),
            ("previous", "Go back to the previous track."),
        ],
    )
    def test_a_command(self, paths, name, summary):
        operation = paths[f"/api/speakers/{{speaker_id}}/playback/{name}"]["post"]

        assert operation["summary"] == summary
        assert "`playback_changed`" in operation["description"]
        assert "requestBody" not in operation
        responses = operation["responses"]
        assert sorted(responses) == ["204", "404", "409", "502"]
        assert "content" not in responses["204"]
        assert "`SPEAKER_NOT_FOUND`" in responses["404"]["description"]
        assert "`ACTION_NOT_AVAILABLE`" in responses["409"]["description"]
        assert "`actions`" in responses["409"]["description"]
        assert "or the speaker refused it" in responses["409"]["description"]
        for status in ("404", "409", "502"):
            schema = responses[status]["content"]["application/json"]["schema"]
            assert "ApiErrorResponseSchema" in schema["$ref"]
            assert responses[status]["description"] != ""

    def test_pause_says_that_it_stops_what_cannot_pause(self, paths):
        operation = paths["/api/speakers/{speaker_id}/playback/pause"]["post"]

        assert "as a radio stream, is stopped" in operation["description"]

    @pytest.mark.parametrize("name", ["", "/play", "/pause", "/next", "/previous"])
    def test_the_speaker_is_described(self, paths, name):
        (operation,) = paths[f"/api/speakers/{{speaker_id}}/playback{name}"].values()

        (speaker_id,) = operation["parameters"]
        assert (speaker_id["name"], speaker_id["in"]) == ("speaker_id", "path")
        assert "its Sonos id" in speaker_id["description"]
