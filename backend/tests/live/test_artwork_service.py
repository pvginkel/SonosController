"""The artwork service on the real Plex: what the fake cannot prove.

The fake Plex answers a JPEG Pillow wrote. This shows that what the real
Plex answers for the album of the track the Garage's queue is at becomes
each picture a display asks for. Plex is only read, and no speaker is asked
for anything: the picture of a Plex track is Plex's.
"""

import pytest
from flask import Flask

from app.exceptions import RecordNotFoundException
from app.services.artwork_service import ArtworkFormat, ArtworkService
from app.services.household_mirror import HouseholdMirror
from app.services.plex_client import PlexapiPlexClient
from app.services.sonos_client import SoCoSonosClient
from app.utils.artwork_id import plex_artwork_id
from app.utils.lifecycle_coordinator import LifecycleCoordinator
from tests.pictures import from_jpeg, from_lvgl

pytestmark = pytest.mark.live

# A rating key far above any the library has given out.
NO_ITEM = "999999999"


@pytest.fixture
def service(
    app: Flask, sonos: SoCoSonosClient, plex: PlexapiPlexClient
) -> ArtworkService:
    """The artwork service on the real clients. Its mirror is not started:
    it subscribes to no speaker and sends no event."""
    mirror = HouseholdMirror(
        sonos_client=sonos,
        plex_client=plex,
        event_sender=app.container.event_sender(),
        lifecycle_coordinator=LifecycleCoordinator(graceful_shutdown_timeout=1),
    )
    return ArtworkService(household_mirror=mirror, sonos_client=sonos, plex_client=plex)


@pytest.fixture
def artwork_id(plex: PlexapiPlexClient, plex_track: str) -> str:
    """The artwork id of the track the Garage's queue is at."""
    picture = plex.read_track_picture(plex_track)
    assert picture is not None, "the track the Garage's queue is at has no picture"
    return plex_artwork_id(picture)


class TestArtworkService:
    @pytest.mark.parametrize("size", [80, 160, 400, 480])
    def test_plexs_picture_as_an_lvgl_image(self, service, artwork_id, size):
        content = service.read(artwork_id, size, ArtworkFormat.LVGL)

        assert len(content) == 12 + 2 * size * size
        assert from_lvgl(content).size == (size, size)

    @pytest.mark.parametrize("size", [16, 400, 1024])
    def test_plexs_picture_as_a_jpeg(self, service, artwork_id, size):
        image = from_jpeg(service.read(artwork_id, size, ArtworkFormat.JPEG))

        assert image.size == (size, size)

    def test_a_picture_is_more_than_one_colour(self, service, artwork_id):
        image = from_jpeg(service.read(artwork_id, 400, ArtworkFormat.JPEG))

        assert len(image.getcolors(maxcolors=400 * 400)) > 16

    def test_an_item_plex_does_not_have_is_not_found(self, service):
        artwork_id = plex_artwork_id(f"/library/metadata/{NO_ITEM}/thumb/1")

        with pytest.raises(RecordNotFoundException):
            service.read(artwork_id, 400, ArtworkFormat.LVGL)
