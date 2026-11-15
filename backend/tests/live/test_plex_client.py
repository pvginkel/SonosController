"""The plexapi client against the real Plex: what the fake cannot prove.

Plex is only read. The token is asserted on through a flag, never in a
comparison pytest would print.
"""

import io
import re

import pytest
from PIL import Image

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckStatus
from app.services.plex_client import PlexapiPlexClient

pytestmark = pytest.mark.live

_PICTURE = re.compile(r"/library/metadata/([0-9]+)/thumb/[0-9]+")

# A rating key far above any the library has given out.
NO_ITEM = "999999999"

REFUSED_TOKEN = "not-the-token"


@pytest.fixture(scope="module")
def picture(live_plex: PlexapiPlexClient, plex_track: str) -> str:
    """The path of the picture of the track the Garage's queue is at."""
    path = live_plex.read_track_picture(plex_track)
    assert path is not None, "the track the Garage's queue is at has no picture"
    return path


def _image(jpeg: bytes | None) -> Image.Image:
    assert jpeg is not None
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    return image


def _other(app, live_settings: AppSettings, **changed: str | None) -> PlexapiPlexClient:
    """A client on the real Plex with one setting changed."""
    return PlexapiPlexClient(
        settings=live_settings.model_copy(update=changed),
        health_service=app.container.health_service(),
    )


class TestPlexSettings:
    def test_the_token_is_present(self, live_settings):
        present = bool(live_settings.plex_token)

        assert present


class TestPlexClientReadiness:
    def test_plex_answers_and_has_the_library(self, plex):
        assert plex.check_readiness().status is CheckStatus.OK

    def test_a_library_plex_does_not_have_is_reported(self, app, live_settings):
        client = _other(app, live_settings, plex_library="No such library")

        result = client.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "Plex has no music library named 'No such library'"

    def test_a_token_plex_refuses_is_reported(self, app, live_settings):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        result = client.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "Plex refused the token"


class TestPlexClientTrackPicture:
    def test_a_tracks_picture_is_its_albums(self, plex_track, picture):
        path = _PICTURE.fullmatch(picture)

        assert path is not None
        # The album's rating key, not the track's.
        assert path.group(1) != plex_track

    def test_a_track_plex_does_not_have_has_no_picture(self, plex):
        assert plex.read_track_picture(NO_ITEM) is None

    def test_an_album_is_not_a_track(self, plex, picture):
        album = _PICTURE.fullmatch(picture).group(1)

        assert plex.read_track_picture(album) is None

    def test_a_token_plex_refuses_fails_without_the_token_or_the_address(
        self, app, live_settings, plex_track
    ):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        with pytest.raises(ExternalServiceException) as raised:
            client.read_track_picture(plex_track)

        assert raised.value.cause == "Plex refused the token"
        named = live_settings.plex_url in raised.value.message
        assert not named


class TestPlexClientPicture:
    @pytest.mark.parametrize("size", [16, 400, 1024])
    def test_a_picture_is_a_jpeg_that_covers_the_square(self, plex, picture, size):
        image = _image(plex.read_picture(picture, size))

        assert min(image.size) == size

    def test_another_stamp_answers_the_picture_as_it_is_now(self, plex, picture):
        album = _PICTURE.fullmatch(picture).group(1)

        image = _image(plex.read_picture(f"/library/metadata/{album}/thumb/1", 100))

        assert min(image.size) == 100

    def test_an_item_without_a_picture_has_none(self, plex, plex_track):
        assert plex.read_picture(f"/library/metadata/{NO_ITEM}/thumb/1", 100) is None
        # A track has no picture of its own.
        assert plex.read_picture(f"/library/metadata/{plex_track}/thumb/1", 100) is None

    def test_a_token_plex_refuses_fails_without_the_token_or_the_address(
        self, app, live_settings, picture
    ):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        with pytest.raises(ExternalServiceException) as raised:
            client.read_picture(picture, 100)

        assert raised.value.cause == "Plex refused the token"
        named = live_settings.plex_url in raised.value.message
        assert not named
