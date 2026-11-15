"""Tests for the Plex client, on the in-memory library.

What ``PlexClient`` promises its callers, shown on ``FakePlexClient``; the
live suite (``tests/live/``) shows the same of the plexapi client on the real
Plex. The plexapi client itself is tested here only where it needs no Plex:
what it refuses to ask, and a Plex that does not answer.
"""

import io
import socket

import pytest
from PIL import Image

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckStatus
from app.services.plex_client import PlexapiPlexClient
from tests.fakes.plex_client import FakePlexClient

ALBUM = "2001"
TRACKS = ("2002", "2003", "2004")
OTHER_ALBUM = "2101"
OTHER_TRACK = "2102"
BARE_ALBUM = "2201"
BARE_TRACK = "2202"

RED = (200, 60, 30)
BLUE = (20, 40, 200)

TOKEN = "a-token-no-failure-may-carry"

NOT_A_PICTURE = [
    "",
    "http://192.0.2.1/cover.jpg",
    "//192.0.2.1/library/metadata/2001/thumb/1700000000",
    "/library/sections",
    "/library/metadata/2001",
    "/library/metadata/2001/thumb/1700000000?url=http://192.0.2.1/",
    "/library/metadata/2001/thumb/1700000000/../../../../identity",
]


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    """Two albums with a picture, one of them not square, and one without."""
    plex.add_album(ALBUM, TRACKS, size=(1000, 1000), colour=RED)
    plex.add_album(OTHER_ALBUM, [OTHER_TRACK], size=(1000, 600), colour=BLUE)
    plex.add_album(BARE_ALBUM, [BARE_TRACK], size=None)
    return plex


@pytest.fixture
def unanswered(app) -> PlexapiPlexClient:
    """The plexapi client on an address nothing listens at."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return PlexapiPlexClient(
        settings=AppSettings(
            sonos_seed_hosts=["192.0.2.1"],
            sonos_event_advertise_host="sonos-events.invalid",
            plex_url=f"http://127.0.0.1:{port}/",
            plex_token=TOKEN,
        ),
        health_service=app.container.health_service(),
    )


def _image(jpeg: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    return image


def _assert_colour(image: Image.Image, colour: tuple[int, int, int]) -> None:
    """The picture's colour, within what JPEG changes of it."""
    pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
    assert all(abs(got - want) <= 8 for got, want in zip(pixel, colour, strict=True))


class TestPlexClientReadiness:
    def test_the_check_is_registered_and_not_critical(self, library, client):
        response = client.get("/health/readyz")

        assert response.status_code == 200
        assert response.get_json()["checks"]["plex"] == {
            "status": "ok",
            "critical": False,
        }

    def test_a_plex_that_does_not_answer_degrades_and_stays_ready(
        self, library, client
    ):
        library.reachable = False

        response = client.get("/health/readyz")

        body = response.get_json()
        assert response.status_code == 200
        assert body["ready"] is True
        assert body["status"] == "degraded"
        assert body["checks"]["plex"] == {
            "status": "error",
            "critical": False,
            "detail": "Plex did not answer (ConnectionError)",
        }

    def test_the_container_provides_the_plexapi_client(self, app):
        assert app.container.plex_client.provides is PlexapiPlexClient

    def test_the_plexapi_client_is_built_without_plex(self, unanswered):
        assert isinstance(unanswered, PlexapiPlexClient)

    def test_the_plexapi_client_reports_a_plex_that_does_not_answer(
        self, unanswered
    ):
        result = unanswered.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "Plex did not answer (ConnectionError)"


class TestPlexClientTrackPicture:
    def test_a_tracks_picture_is_its_albums(self, library):
        picture = library.read_track_picture(TRACKS[0])

        assert picture is not None
        assert picture.startswith(f"/library/metadata/{ALBUM}/thumb/")
        assert picture.removeprefix(f"/library/metadata/{ALBUM}/thumb/").isdigit()

    def test_the_tracks_of_an_album_share_one_picture(self, library):
        pictures = {library.read_track_picture(track) for track in TRACKS}

        assert len(pictures) == 1
        assert library.read_track_picture(OTHER_TRACK) not in pictures

    def test_a_new_picture_has_a_new_path(self, library):
        before = library.read_track_picture(TRACKS[0])

        library.set_picture(ALBUM, (1000, 1000), BLUE)

        after = library.read_track_picture(TRACKS[0])
        assert after != before
        assert after.startswith(f"/library/metadata/{ALBUM}/thumb/")

    def test_a_track_plex_does_not_have_has_no_picture(self, library):
        assert library.read_track_picture("9999") is None

    def test_a_track_of_an_album_without_a_picture_has_none(self, library):
        assert library.read_track_picture(BARE_TRACK) is None

    def test_an_album_is_not_a_track(self, library):
        assert library.read_track_picture(ALBUM) is None

    def test_a_plex_that_does_not_answer_fails(self, library):
        library.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.read_track_picture(TRACKS[0])

        assert raised.value.operation == "read the picture of Plex track 2002"
        assert raised.value.cause == "Plex did not answer (ConnectionError)"

    def test_the_plexapi_client_fails_without_the_token_or_the_address(
        self, unanswered
    ):
        with pytest.raises(ExternalServiceException) as raised:
            unanswered.read_track_picture(TRACKS[0])

        assert raised.value.message == (
            "Cannot read the picture of Plex track 2002 because external "
            "service failed: Plex did not answer (ConnectionError)"
        )

    @pytest.mark.parametrize("rating_key", ["", "abc", "12/../../identity", "1,2"])
    def test_the_plexapi_client_asks_plex_for_a_rating_key_only(
        self, unanswered, rating_key
    ):
        # Plex does not answer: a call that reached it would fail.
        assert unanswered.read_track_picture(rating_key) is None


class TestPlexClientPicture:
    def test_a_picture_is_a_jpeg_of_the_size_asked_for(self, library):
        picture = library.read_track_picture(TRACKS[0])

        image = _image(library.read_picture(picture, 400))

        assert image.size == (400, 400)
        _assert_colour(image, RED)

    def test_a_picture_that_is_not_square_covers_the_square(self, library):
        picture = library.read_track_picture(OTHER_TRACK)

        image = _image(library.read_picture(picture, 300))

        assert image.size == (500, 300)
        _assert_colour(image, BLUE)

    def test_a_small_picture_is_scaled_up(self, library):
        picture = library.read_track_picture(TRACKS[0])

        assert _image(library.read_picture(picture, 1024)).size == (1024, 1024)

    def test_another_stamp_answers_the_picture_as_it_is_now(self, library):
        before = library.read_track_picture(TRACKS[0])
        library.set_picture(ALBUM, (1000, 1000), BLUE)

        _assert_colour(_image(library.read_picture(before, 100)), BLUE)

    def test_an_item_without_a_picture_has_none(self, library):
        assert library.read_picture(f"/library/metadata/{BARE_ALBUM}/thumb/1", 100) is None
        # A track has no picture of its own.
        assert library.read_picture(f"/library/metadata/{TRACKS[0]}/thumb/1", 100) is None
        assert library.read_picture("/library/metadata/9999/thumb/1", 100) is None

    @pytest.mark.parametrize("picture", NOT_A_PICTURE)
    def test_a_path_that_is_no_pictures_has_none(self, library, picture):
        assert library.read_picture(picture, 100) is None

    @pytest.mark.parametrize("picture", NOT_A_PICTURE)
    def test_the_plexapi_client_asks_plex_for_a_picture_path_only(
        self, unanswered, picture
    ):
        # Plex does not answer: a call that reached it would fail.
        assert unanswered.read_picture(picture, 100) is None

    def test_a_plex_that_does_not_answer_fails(self, library):
        picture = library.read_track_picture(TRACKS[0])
        library.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.read_picture(picture, 100)

        assert raised.value.operation == (
            f"read Plex's picture {picture} at 100 pixels"
        )
        assert raised.value.cause == "Plex did not answer (ConnectionError)"

    def test_the_plexapi_client_fails_without_the_token_or_the_address(
        self, unanswered
    ):
        with pytest.raises(ExternalServiceException) as raised:
            unanswered.read_picture("/library/metadata/2001/thumb/1700000000", 100)

        assert raised.value.message == (
            "Cannot read Plex's picture /library/metadata/2001/thumb/1700000000 "
            "at 100 pixels because external service failed: Plex did not "
            "answer (ConnectionError)"
        )
