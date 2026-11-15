"""Tests for artwork ids."""

import base64
import re

import pytest

from app.utils.artwork_id import (
    PlexPicture,
    SpeakerPicture,
    parse_artwork_id,
    plex_artwork_id,
    speaker_artwork_id,
)

PATH = "/library/metadata/2001/thumb/1700000000"
SPEAKER = "RINCON_FAKE0000000101400"
URI = "x-sonos-spotify:spotify%3atrack%3a1?sid=9&flags=8224&sn=7"
PICTURE = f"/getaa?s=1&u={URI}"


def _encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class TestArtworkId:
    def test_a_plex_id_names_its_path(self):
        assert parse_artwork_id(plex_artwork_id(PATH)) == PlexPicture(path=PATH)

    def test_one_path_has_one_id_and_another_stamp_another(self):
        assert plex_artwork_id(PATH) == plex_artwork_id(PATH)
        assert plex_artwork_id(PATH) != plex_artwork_id(
            "/library/metadata/2001/thumb/1700000001"
        )

    def test_a_speakers_id_names_its_speaker(self):
        artwork_id = speaker_artwork_id(SPEAKER, URI, PICTURE)

        assert parse_artwork_id(artwork_id) == SpeakerPicture(speaker_id=SPEAKER)

    def test_a_speakers_id_is_one_tracks_one_pictures_and_one_speakers(self):
        artwork_id = speaker_artwork_id(SPEAKER, URI, PICTURE)

        assert artwork_id == speaker_artwork_id(SPEAKER, URI, PICTURE)
        assert artwork_id != speaker_artwork_id(SPEAKER, URI + "0", PICTURE)
        assert artwork_id != speaker_artwork_id(SPEAKER, URI, PICTURE + "0")
        assert artwork_id != speaker_artwork_id("RINCON_OTHER", URI, PICTURE)

    def test_a_speakers_id_does_not_grow_with_the_tracks_address(self):
        short = speaker_artwork_id(SPEAKER, "x-file-cifs://nas/a.mp3", "/getaa?u=a")
        long = speaker_artwork_id(SPEAKER, "x-file-cifs://nas/" + "a" * 2000, PICTURE)

        assert len(long) == len(short)

    @pytest.mark.parametrize(
        "artwork_id",
        [plex_artwork_id(PATH), speaker_artwork_id(SPEAKER, URI, PICTURE)],
    )
    def test_an_id_fits_a_path_segment(self, artwork_id):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", artwork_id)

    @pytest.mark.parametrize(
        "artwork_id",
        [
            "",
            "nothing",
            "!!!",
            "ünïcode",
            _encoded("plex"),
            _encoded("plex:"),
            _encoded("sonos:"),
            _encoded("sonos:digest"),
            _encoded("sonos:digest:"),
            _encoded("sonos::RINCON_1"),
            _encoded(f"other:{PATH}"),
            base64.urlsafe_b64encode(b"plex:\xff\xfe").decode().rstrip("="),
            # The same bytes as an id, written another way.
            plex_artwork_id(PATH) + "=",
            plex_artwork_id(PATH)[:-1] + chr(ord(plex_artwork_id(PATH)[-1]) + 1),
        ],
    )
    def test_anything_else_names_nothing(self, artwork_id):
        assert parse_artwork_id(artwork_id) is None
