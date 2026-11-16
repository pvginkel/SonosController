"""Tests for the artwork endpoint, on the in-memory household and library."""

import pytest

from app.services.artwork_service import ArtworkService
from app.services.household_mirror import HouseholdMirror
from app.utils.artwork_id import plex_artwork_id
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import (
    FakeSonosClient,
    app_track,
    picture_colour,
    queue_track,
)
from tests.pictures import GREEN, assert_colour, bands, from_jpeg, from_lvgl

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"

ORANGE = (200, 60, 30)

CACHE_CONTROL = "public, max-age=31536000, immutable"

@pytest.fixture
def household(
    sonos: FakeSonosClient,
    plex: FakePlexClient,
    mirror: HouseholdMirror,
    artwork: ArtworkService,
) -> FakeSonosClient:
    """The Garage at a Plex track, the Lounge at a track of another service,
    mirrored."""
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.add_room(LOUNGE, "Lounge", model="Playbase", volume=40)
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2)])
    sonos.load_queue(LOUNGE, [app_track(1), app_track(2)])
    plex.add_album("2001", ["1001", "1002"], size=(1000, 1000), colour=ORANGE)
    plex.add_album("2002", ["1003"])
    plex.set_image("2002", bands((1200, 300)))
    mirror.start()
    sonos.settle()
    return sonos


@pytest.fixture
def plex_id(household: FakeSonosClient, mirror: HouseholdMirror) -> str:
    return mirror.playback(GARAGE).track.artwork_id


@pytest.fixture
def speaker_id(household: FakeSonosClient, mirror: HouseholdMirror) -> str:
    return mirror.playback(LOUNGE).track.artwork_id


def not_found(artwork_id: str) -> dict:
    return {
        "error": f"Artwork {artwork_id} was not found",
        "details": {"message": "The requested resource could not be found"},
        "code": "RECORD_NOT_FOUND",
    }


def without_correlation_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "correlationId"}


class TestGetArtwork:
    def test_a_jpeg(self, client, plex_id):
        response = client.get(f"/api/artwork/{plex_id}?size=400&format=jpeg")

        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/jpeg"
        assert response.headers["Cache-Control"] == CACHE_CONTROL
        assert int(response.headers["Content-Length"]) == len(response.data)
        image = from_jpeg(response.data)
        assert image.size == (400, 400)
        assert_colour(image, ORANGE)

    def test_the_format_is_jpeg_unless_asked_otherwise(self, client, plex_id):
        response = client.get(f"/api/artwork/{plex_id}?size=160")

        assert response.status_code == 200
        assert response.headers["Content-Type"] == "image/jpeg"
        assert from_jpeg(response.data).size == (160, 160)

    @pytest.mark.parametrize("size", [80, 160, 400])
    def test_an_lvgl_image(self, client, plex_id, size):
        response = client.get(f"/api/artwork/{plex_id}?size={size}&format=lvgl")

        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["Cache-Control"] == CACHE_CONTROL
        assert int(response.headers["Content-Length"]) == 12 + 2 * size * size
        assert len(response.data) == 12 + 2 * size * size
        image = from_lvgl(response.data)
        assert image.size == (size, size)
        assert_colour(image, ORANGE)

    def test_a_picture_that_is_not_square_is_cropped_at_the_centre(
        self, client, household, plex
    ):
        artwork_id = plex_artwork_id(plex.read_track_picture("1003"))

        response = client.get(f"/api/artwork/{artwork_id}?size=300")

        image = from_jpeg(response.data)
        assert image.size == (300, 300)
        assert_colour(image, GREEN)

    def test_the_speakers_picture_of_what_it_has_loaded(self, client, speaker_id):
        response = client.get(f"/api/artwork/{speaker_id}?size=400&format=lvgl")

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == CACHE_CONTROL
        image = from_lvgl(response.data)
        assert image.size == (400, 400)
        assert_colour(image, picture_colour(app_track(1).picture))

    @pytest.mark.parametrize(
        "query",
        [
            "size=16",
            "size=1024",
            "size=1024&format=jpeg",
            "size=16&format=lvgl",
            "size=480&format=lvgl",
        ],
    )
    def test_the_sizes_at_the_limits_are_answered(self, client, plex_id, query):
        assert client.get(f"/api/artwork/{plex_id}?{query}").status_code == 200

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "format=jpeg",
            "size=",
            "size=large",
            "size=40.5",
            "size=15",
            "size=0",
            "size=-400",
            "size=1025",
            "size=1025&format=jpeg",
            "size=15&format=lvgl",
            "size=481&format=lvgl",
            "size=400&format=png",
            "size=400&format=LVGL",
            "size=400&format=",
        ],
    )
    def test_a_size_or_a_format_that_is_not_served_is_refused(
        self, client, plex_id, query
    ):
        response = client.get(f"/api/artwork/{plex_id}?{query}")

        assert response.status_code == 400
        assert "Cache-Control" not in response.headers

    @pytest.mark.parametrize("artwork_id", ["nothing", "cGxleA", "RINCON_FAKE0000000101400"])
    def test_an_unknown_id_is_not_found(self, client, household, artwork_id):
        response = client.get(f"/api/artwork/{artwork_id}?size=400")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(artwork_id)
        assert "Cache-Control" not in response.headers

    def test_a_picture_plex_does_not_have_is_not_found(self, client, household):
        artwork_id = plex_artwork_id("/library/metadata/9999/thumb/1700000000")

        response = client.get(f"/api/artwork/{artwork_id}?size=400&format=lvgl")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(artwork_id)

    def test_a_speakers_picture_is_not_found_once_another_track_is_loaded(
        self, client, household, speaker_id
    ):
        assert client.get(f"/api/artwork/{speaker_id}?size=80").status_code == 200

        household.next(LOUNGE)
        household.settle()

        response = client.get(f"/api/artwork/{speaker_id}?size=80")
        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(speaker_id)

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, plex, plex_id):
        plex.reachable = False

        response = client.get(f"/api/artwork/{plex_id}?size=400")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == {
            "error": (
                "Cannot read Plex's picture /library/metadata/2001/thumb/1700000000 "
                "at 400 pixels because external service failed: Plex did not "
                "answer (ConnectionError)"
            ),
            "details": {"message": "External service request failed"},
            "code": "EXTERNAL_SERVICE_ERROR",
        }
        assert "Cache-Control" not in response.headers

    def test_a_speaker_that_does_not_answer_is_a_bad_gateway(
        self, client, household, speaker_id
    ):
        household.silence_room(LOUNGE)
        try:
            response = client.get(f"/api/artwork/{speaker_id}?size=400")
        finally:
            household.answer_again(LOUNGE)

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == {
            "error": (
                f"Cannot read the picture served by speaker {LOUNGE} because "
                "external service failed: the speaker did not answer "
                "(ConnectTimeout)"
            ),
            "details": {"message": "External service request failed"},
            "code": "EXTERNAL_SERVICE_ERROR",
        }

    def test_while_plex_is_down_the_playback_state_is_served_without_artwork(
        self, household, plex, mirror
    ):
        # Plex has answered: the backend knows which server it is.
        assert mirror.playback(GARAGE).track.track_id == "1001"
        plex.reachable = False
        household.play(GARAGE)
        household.settle()

        playback = mirror.playback(GARAGE)

        assert playback.state == "playing"
        assert playback.track.title == "Track 1"
        assert playback.track.artwork_id is None

    def test_the_tracks_of_an_album_have_one_picture(
        self, client, household, mirror, plex_id
    ):
        household.next(GARAGE)
        household.settle()

        assert mirror.playback(GARAGE).track.track_id == "1002"
        assert mirror.playback(GARAGE).track.artwork_id == plex_id
        assert client.get(f"/api/artwork/{plex_id}?size=80").status_code == 200


class TestArtworkDocument:
    @pytest.fixture
    def operation(self, openapi_document) -> dict:
        return openapi_document["paths"]["/api/artwork/{artwork_id}"]["get"]

    def test_it_says_what_the_endpoint_serves(self, operation):
        assert operation["summary"] == "A picture, as a square of the size asked for."
        description = " ".join(operation["description"].split())
        for part in (
            "An artwork id names one picture",
            "scaled to cover the square and cropped at the centre",
            "`format=jpeg` answers `image/jpeg`",
            "`format=lvgl` answers `application/octet-stream` with `Content-Length`",
            "12-byte header",
            "the magic byte `0x19`",
            "the colour format `0x12` (RGB565)",
            "little-endian",
            "dithered",
            "`400`:",
            "`404`:",
            "`502`:",
        ):
            assert part in description

    def test_every_parameter_is_described(self, operation):
        parameters = {
            parameter["name"]: parameter for parameter in operation["parameters"]
        }

        assert sorted(parameters) == ["artwork_id", "format", "size"]
        assert parameters["artwork_id"]["in"] == "path"
        assert "opaque" in parameters["artwork_id"]["description"]
        size = parameters["size"]
        assert (size["in"], size["required"]) == ("query", True)
        assert (size["schema"]["minimum"], size["schema"]["maximum"]) == (16, 1024)
        assert "to 480 for `lvgl`" in size["description"]
        assert (parameters["format"]["in"], parameters["format"]["required"]) == (
            "query",
            False,
        )
        assert parameters["format"]["schema"]["default"] == "jpeg"
        assert "`lvgl`" in parameters["format"]["description"]

    def test_the_format_is_one_of_two(self, openapi_document, operation):
        (parameter,) = (p for p in operation["parameters"] if p["name"] == "format")
        name = parameter["schema"]["$ref"].removeprefix("#/components/schemas/")

        schemas = openapi_document["components"]["schemas"]
        assert schemas[name]["enum"] == ["jpeg", "lvgl"]

    def test_the_answer_is_a_picture_a_client_may_keep(self, operation):
        answer = operation["responses"]["200"]

        assert sorted(answer["content"]) == ["application/octet-stream", "image/jpeg"]
        for content in answer["content"].values():
            assert content["schema"] == {"type": "string", "format": "binary"}
        assert CACHE_CONTROL in answer["headers"]["Cache-Control"]["description"]

    def test_every_error_status_is_documented_with_the_envelope(self, operation):
        responses = operation["responses"]

        assert sorted(responses) == ["200", "400", "404", "502"]
        for status in ("400", "404", "502"):
            schema = responses[status]["content"]["application/json"]["schema"]
            assert "ApiErrorResponseSchema" in schema["$ref"]
