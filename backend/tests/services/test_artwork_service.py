"""Tests for the artwork service, on the in-memory household and library.

The service runs on the real mirror, the fake Sonos client and the fake Plex
client. A lossless picture, where a test needs each pixel as it was, is one a
speaker serves: the fake Plex answers JPEGs, as Plex does.
"""

import numpy as np
import pytest
from PIL import Image

from app.core_exceptions import ExternalServiceException
from app.exceptions import RecordNotFoundException
from app.services.artwork_service import ArtworkFormat, ArtworkService
from app.services.household_mirror import HouseholdMirror
from app.utils.artwork_id import plex_artwork_id, speaker_artwork_id
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import (
    FakeSonosClient,
    app_track,
    picture_colour,
    queue_track,
)
from tests.pictures import (
    GREEN,
    assert_colour,
    bands,
    from_jpeg,
    from_lvgl,
    levels,
    png,
)

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"

JPEG = ArtworkFormat.JPEG
LVGL = ArtworkFormat.LVGL

ORANGE = (200, 60, 30)
TEAL = (20, 140, 160)

ALBUM = "2001"
WIDE_ALBUM = "2002"
TALL_ALBUM = "2003"
BARE_ALBUM = "2004"


@pytest.fixture
def household(sonos: FakeSonosClient) -> FakeSonosClient:
    """The Garage with three Plex tracks queued, and the Lounge with two
    tracks of another service."""
    sonos.add_room(GARAGE, "Garage", model="Play:3", volume=20)
    sonos.add_room(LOUNGE, "Lounge", model="Playbase", volume=40)
    sonos.load_queue(GARAGE, [queue_track(1), queue_track(2), queue_track(3)])
    sonos.load_queue(LOUNGE, [app_track(1), app_track(2)])
    return sonos


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    """The Garage's first track is of an album with a square picture. Two
    albums have a picture that is not square, and one has none."""
    plex.add_album(ALBUM, ["1001", "1002"], size=(1000, 1000), colour=ORANGE)
    plex.add_album(WIDE_ALBUM, ["1003"])
    plex.set_image(WIDE_ALBUM, bands((1200, 300)))
    plex.add_album(TALL_ALBUM, ["1004"])
    plex.set_image(TALL_ALBUM, bands((300, 1200)))
    plex.add_album(BARE_ALBUM, ["1005"], size=None)
    return plex


@pytest.fixture
def started(
    household: FakeSonosClient, library: FakePlexClient, mirror: HouseholdMirror
) -> HouseholdMirror:
    mirror.start()
    household.settle()
    return mirror


@pytest.fixture
def service(artwork: ArtworkService, started: HouseholdMirror) -> ArtworkService:
    return artwork


@pytest.fixture
def plex_id(started: HouseholdMirror) -> str:
    """The artwork id of what the Garage has loaded: Plex's picture."""
    return started.playback(GARAGE).track.artwork_id


@pytest.fixture
def speaker_id(started: HouseholdMirror) -> str:
    """The artwork id of what the Lounge has loaded: the speaker's picture."""
    return started.playback(LOUNGE).track.artwork_id


def album_id(library: FakePlexClient, track: str) -> str:
    return plex_artwork_id(library.read_track_picture(track))


def serve(household: FakeSonosClient, image: Image.Image) -> None:
    """Have the Lounge serve this picture, as it is, for what it has loaded."""
    household.serve_picture(app_track(1).picture, png(image))


class TestArtworkServicePlex:
    @pytest.mark.parametrize("size", [16, 400, 1024])
    def test_a_jpeg_is_the_square_asked_for(self, service, plex_id, size):
        image = from_jpeg(service.read(plex_id, size, JPEG))

        assert image.size == (size, size)
        assert_colour(image, ORANGE)

    @pytest.mark.parametrize("size", [16, 80, 160, 400, 480])
    def test_an_lvgl_image_is_the_square_asked_for(self, service, plex_id, size):
        content = service.read(plex_id, size, LVGL)

        assert len(content) == 12 + 2 * size * size
        image = from_lvgl(content)
        assert image.size == (size, size)
        assert_colour(image, ORANGE)

    @pytest.mark.parametrize("track", ["1003", "1004"])
    @pytest.mark.parametrize("size", [100, 300, 400])
    @pytest.mark.parametrize("artwork_format", [JPEG, LVGL])
    def test_a_picture_that_is_not_square_is_cropped_at_the_centre(
        self, service, library, track, size, artwork_format
    ):
        content = service.read(album_id(library, track), size, artwork_format)

        image = from_jpeg(content) if artwork_format is JPEG else from_lvgl(content)
        assert image.size == (size, size)
        assert_colour(image, GREEN)

    def test_an_id_with_an_old_stamp_answers_the_picture_as_it_is_now(
        self, service, library, plex_id
    ):
        library.set_picture(ALBUM, (800, 800), TEAL)

        assert_colour(from_jpeg(service.read(plex_id, 200, JPEG)), TEAL)

    def test_an_album_without_a_picture_is_not_found(self, service):
        artwork_id = plex_artwork_id(f"/library/metadata/{BARE_ALBUM}/thumb/1")

        with pytest.raises(RecordNotFoundException) as raised:
            service.read(artwork_id, 100, JPEG)

        assert raised.value.message == f"Artwork {artwork_id} was not found"

    @pytest.mark.parametrize(
        "path",
        [
            "/library/metadata/9999/thumb/1700000000",
            "/library/sections",
            "http://192.0.2.1/cover.jpg",
            "/library/metadata/2001/thumb/1700000000?url=http://192.0.2.1/",
        ],
    )
    def test_a_path_plex_has_no_picture_at_is_not_found(self, service, path):
        with pytest.raises(RecordNotFoundException):
            service.read(plex_artwork_id(path), 100, JPEG)

    def test_a_plex_that_does_not_answer_fails(self, service, library, plex_id):
        library.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            service.read(plex_id, 100, JPEG)

        assert raised.value.cause == "Plex did not answer (ConnectionError)"


class TestArtworkServiceSpeaker:
    def test_the_speakers_picture_of_what_it_has_loaded(self, service, speaker_id):
        image = from_jpeg(service.read(speaker_id, 200, JPEG))

        assert image.size == (200, 200)
        assert_colour(image, picture_colour(app_track(1).picture))

    def test_a_small_picture_is_scaled_up(self, service, speaker_id):
        # The speaker serves 300 × 300.
        image = from_lvgl(service.read(speaker_id, 400, LVGL))

        assert image.size == (400, 400)
        assert_colour(image, picture_colour(app_track(1).picture))

    @pytest.mark.parametrize("shape", [(1200, 300), (300, 1200), (120, 30)])
    def test_a_picture_that_is_not_square_is_cropped_at_the_centre(
        self, service, household, speaker_id, shape
    ):
        serve(household, bands(shape))

        image = from_jpeg(service.read(speaker_id, 120, JPEG))

        assert image.size == (120, 120)
        assert_colour(image, GREEN)

    def test_a_picture_with_transparency_is_served(self, service, household, speaker_id):
        serve(household, Image.new("RGBA", (50, 50), (*TEAL, 128)))

        assert_colour(from_jpeg(service.read(speaker_id, 50, JPEG)), TEAL)

    def test_it_is_not_found_once_another_track_is_loaded(
        self, service, household, speaker_id
    ):
        # Kept: the speaker's picture is not answered from what is kept.
        service.read(speaker_id, 100, JPEG)

        household.next(LOUNGE)
        household.settle()

        with pytest.raises(RecordNotFoundException):
            service.read(speaker_id, 100, JPEG)
        # The speaker itself still serves it.
        assert household.read_picture(LOUNGE, app_track(1).picture)

    def test_it_is_found_again_when_its_track_is_loaded_again(
        self, service, household, speaker_id
    ):
        household.next(LOUNGE)
        household.previous(LOUNGE)
        household.settle()

        assert service.read(speaker_id, 100, JPEG)

    def test_a_plex_tracks_picture_on_the_speaker_is_not_found(self, service):
        track = queue_track(1)
        artwork_id = speaker_artwork_id(GARAGE, track.uri, track.picture)

        with pytest.raises(RecordNotFoundException):
            service.read(artwork_id, 100, JPEG)

    def test_another_plex_servers_track_has_the_picture_the_speaker_serves(
        self, service, started, household
    ):
        theirs = queue_track(1, machine_identifier="anotherserver")
        household.load_queue(GARAGE, [theirs])
        household.settle()
        artwork_id = started.playback(GARAGE).track.artwork_id
        assert artwork_id == speaker_artwork_id(GARAGE, theirs.uri, theirs.picture)

        image = from_jpeg(service.read(artwork_id, 100, JPEG))

        assert image.size == (100, 100)
        assert_colour(image, picture_colour(theirs.picture))

    def test_a_plex_services_picture_fails_while_plex_has_never_answered(
        self, artwork, household, plex, mirror
    ):
        plex.reachable = False
        mirror.start()
        household.settle()
        track = queue_track(1)
        artwork_id = speaker_artwork_id(GARAGE, track.uri, track.picture)

        with pytest.raises(ExternalServiceException) as raised:
            artwork.read(artwork_id, 100, JPEG)

        assert raised.value.message == (
            "Cannot read which server Plex is because external service "
            "failed: Plex did not answer (ConnectionError)"
        )

    def test_a_speaker_the_household_does_not_have_is_not_found(self, service):
        track = app_track(1)
        artwork_id = speaker_artwork_id("RINCON_NONE", track.uri, track.picture)

        with pytest.raises(RecordNotFoundException):
            service.read(artwork_id, 100, JPEG)

    def test_a_speaker_that_does_not_answer_fails(self, service, household, speaker_id):
        household.silence_room(LOUNGE)
        try:
            with pytest.raises(ExternalServiceException):
                service.read(speaker_id, 100, JPEG)
        finally:
            household.answer_again(LOUNGE)

    @pytest.mark.parametrize(
        ("content", "error"),
        [
            (b"", "UnidentifiedImageError"),
            (b"<html>not a picture</html>", "UnidentifiedImageError"),
            (b"\xff\xd8\xff\xe0 cut short", "OSError"),
        ],
    )
    def test_what_is_not_a_picture_fails(
        self, service, household, speaker_id, content, error
    ):
        household.serve_picture(app_track(1).picture, content)

        with pytest.raises(ExternalServiceException) as raised:
            service.read(speaker_id, 100, JPEG)

        assert raised.value.operation == f"prepare the picture speaker {LOUNGE} serves"
        assert raised.value.cause == f"it is not a picture ({error})"


class TestArtworkServiceUnknownIds:
    @pytest.mark.parametrize("artwork_id", ["", "nothing", "!!!", "cGxleA", "../.."])
    def test_what_is_no_id_is_not_found(self, service, artwork_id):
        with pytest.raises(RecordNotFoundException) as raised:
            service.read(artwork_id, 100, JPEG)

        assert raised.value.message == f"Artwork {artwork_id} was not found"


class TestArtworkServiceLvgl:
    """What ``format=lvgl`` writes, pixel by pixel, from a picture a speaker
    serves without loss."""

    def test_the_header_and_the_pixels_are_lvgl_9s_binary_image(
        self, service, household, speaker_id
    ):
        serve(household, Image.new("RGB", (24, 24), (255, 0, 0)))

        content = service.read(speaker_id, 24, LVGL)

        # Magic, RGB565, no flags, width, height, stride, reserved.
        assert content[:12] == bytes.fromhex("19 12 0000 1800 1800 3000 0000")
        # Red alone, in the upper five bits of a little-endian 16-bit pixel.
        assert content[12:] == b"\x00\xf8" * (24 * 24)

    @pytest.mark.parametrize(
        ("colour", "pixel"),
        [
            ((0, 0, 0), (0, 0, 0)),
            ((255, 255, 255), (31, 63, 31)),
            ((0, 255, 0), (0, 63, 0)),
            ((0, 0, 255), (0, 0, 31)),
        ],
    )
    def test_a_colour_16_bits_can_hold_is_not_dithered(
        self, service, household, speaker_id, colour, pixel
    ):
        serve(household, Image.new("RGB", (32, 32), colour))

        found = levels(service.read(speaker_id, 32, LVGL))

        assert (found == np.array(pixel)).all()

    def test_a_colour_between_two_levels_is_dithered_onto_both(
        self, service, household, speaker_id
    ):
        grey = 128
        serve(household, Image.new("RGB", (64, 64), (grey, grey, grey)))

        found = levels(service.read(speaker_id, 64, LVGL))

        for channel, top in enumerate((31, 63, 31)):
            level = grey * top / 255
            values = found[:, :, channel]
            assert set(np.unique(values)) == {int(level), int(level) + 1}
            # Each 8 × 8 tile averages to the colour, to the 64th of a level
            # that 64 pixels can show.
            for tile in (values[:8, :8], values[8:16, 24:32], values[56:, 56:]):
                assert abs(tile.mean() - level) <= 1 / 64

    def test_the_dither_is_ordered(self, service, household, speaker_id):
        serve(household, Image.new("RGB", (64, 64), (100, 150, 200)))

        found = levels(service.read(speaker_id, 64, LVGL))

        # The same pattern every 8 pixels, both ways.
        assert (found[:56, :, :] == found[8:, :, :]).all()
        assert (found[:, :56, :] == found[:, 8:, :]).all()

    def test_a_gradient_keeps_its_average(self, service, household, speaker_id):
        ramp = np.tile(np.linspace(0, 255, 256).astype(np.uint8), (64, 1))
        serve(household, Image.fromarray(np.dstack([ramp, ramp, ramp]), "RGB"))

        found = levels(service.read(speaker_id, 256, LVGL))

        # The picture is 256 × 64 and covers the square: its centre, 64 wide.
        source = np.linspace(0, 255, 256)[96:160]
        red = found[:, :, 0] * 255 / 31
        assert abs(red.mean() - source.mean()) < 1.0


class TestArtworkServiceCache:
    def test_a_finished_picture_is_kept(self, service, library, plex_id):
        first = service.read(plex_id, 100, JPEG)
        library.reachable = False

        assert service.read(plex_id, 100, JPEG) == first

    def test_it_is_kept_by_id_size_and_format(self, service, library, plex_id):
        service.read(plex_id, 100, JPEG)
        library.reachable = False

        for artwork_id, size, artwork_format in (
            (plex_id, 101, JPEG),
            (plex_id, 100, LVGL),
            (album_id_of_another_album(), 100, JPEG),
        ):
            with pytest.raises(ExternalServiceException):
                service.read(artwork_id, size, artwork_format)

    def test_the_pictures_used_longest_ago_make_room(
        self, started, household, library, plex_id
    ):
        # An LVGL image of 16 pixels is 524 bytes: two fit, three do not.
        service = ArtworkService(
            household_mirror=started,
            sonos_client=household,
            plex_client=library,
            cache_bytes=1100,
        )
        wide = album_id(library, "1003")
        tall = album_id(library, "1004")
        service.read(plex_id, 16, LVGL)
        service.read(wide, 16, LVGL)
        service.read(plex_id, 16, LVGL)
        service.read(tall, 16, LVGL)
        library.reachable = False

        assert service.read(plex_id, 16, LVGL)
        assert service.read(tall, 16, LVGL)
        with pytest.raises(ExternalServiceException):
            service.read(wide, 16, LVGL)

    def test_a_picture_larger_than_the_cache_is_served_and_not_kept(
        self, started, household, library, plex_id
    ):
        service = ArtworkService(
            household_mirror=started,
            sonos_client=household,
            plex_client=library,
            cache_bytes=500,
        )

        assert len(service.read(plex_id, 16, LVGL)) == 524
        library.reachable = False
        with pytest.raises(ExternalServiceException):
            service.read(plex_id, 16, LVGL)


def album_id_of_another_album() -> str:
    return plex_artwork_id(f"/library/metadata/{WIDE_ALBUM}/thumb/1")
