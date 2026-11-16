"""The in-memory Plex pytest runs against.

``FakePlexClient`` keeps a small library: albums, each with its tracks and a
picture. It answers ``PlexClient``'s calls from it the way the real Plex was
seen to (``docs/api-design.md`` section 1.9, and the live suite): a track's
picture is its album's, at a path that carries the album's rating key and
the picture's stamp; a picture comes back as a JPEG that covers the square
asked for and keeps its shape; a path with another stamp answers the picture
as it is now. Like plexapi, it holds which server it is from Plex's first
answer.

A test builds its library with ``add_album()``.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass

from PIL import Image

from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckResult, CheckStatus, HealthService
from app.services.plex_client import PlexClient

_PICTURE = re.compile(r"/library/metadata/([0-9]+)/thumb/[0-9]+")

_UNREACHABLE = "Plex did not answer (ConnectionError)"

# The server's machine identifier, which the fake household's Plex queue
# entries carry.
MACHINE_IDENTIFIER = "fakeserver"


@dataclass
class _Album:
    rating_key: str
    picture: Image.Image | None
    stamp: int


class FakePlexClient(PlexClient):
    def __init__(self, health_service: HealthService) -> None:
        super().__init__(health_service)
        self._albums: dict[str, _Album] = {}
        self._album_of_track: dict[str, str] = {}
        self._next_stamp = 1700000000
        # False: Plex does not answer, as when its pod is down.
        self.reachable = True
        # plexapi reads the server at the first call that needs it, and
        # holds it from then on.
        self._server_read = False

    # ------------------------------------------------------------------
    # Building a library
    # ------------------------------------------------------------------

    def add_album(
        self,
        rating_key: str,
        track_keys: Iterable[str],
        size: tuple[int, int] | None = (1000, 1000),
        colour: tuple[int, int, int] = (200, 60, 30),
    ) -> None:
        """Add an album with its tracks, by their rating keys. Its picture is
        ``size`` pixels of one colour; ``size=None`` is an album without a
        picture."""
        self._albums[rating_key] = _Album(rating_key, None, 0)
        for track_key in track_keys:
            self._album_of_track[track_key] = rating_key
        if size is not None:
            self.set_picture(rating_key, size, colour)

    def set_picture(
        self,
        rating_key: str,
        size: tuple[int, int],
        colour: tuple[int, int, int],
    ) -> None:
        """Give an album another picture, of one colour: its path gets a new
        stamp."""
        self.set_image(rating_key, Image.new("RGB", size, colour))

    def set_image(self, rating_key: str, image: Image.Image) -> None:
        """Give an album this picture: its path gets a new stamp."""
        album = self._albums[rating_key]
        album.picture = image
        album.stamp = self._next_stamp
        self._next_stamp += 1

    # ------------------------------------------------------------------
    # PlexClient
    # ------------------------------------------------------------------

    def check_readiness(self) -> CheckResult:
        if not self.reachable:
            return CheckResult(CheckStatus.ERROR, _UNREACHABLE)
        self._server_read = True
        return CheckResult(CheckStatus.OK)

    def machine_identifier(self) -> str:
        if not self._server_read:
            self._require_reachable("read which server Plex is")
            self._server_read = True
        return MACHINE_IDENTIFIER

    def read_track_picture(self, rating_key: str) -> str | None:
        self._require_reachable(f"read the picture of Plex track {rating_key}")
        self._server_read = True
        album_key = self._album_of_track.get(rating_key)
        if album_key is None:
            return None
        album = self._albums[album_key]
        if album.picture is None:
            return None
        return f"/library/metadata/{album.rating_key}/thumb/{album.stamp}"

    def read_picture(self, picture: str, size: int) -> bytes | None:
        path = _PICTURE.fullmatch(picture)
        if path is None:
            return None
        self._require_reachable(f"read Plex's picture {picture} at {size} pixels")
        album = self._albums.get(path.group(1))
        if album is None or album.picture is None:
            return None
        width, height = album.picture.size
        factor = size / min(width, height)
        resized = album.picture.resize(
            (max(size, round(width * factor)), max(size, round(height * factor)))
        )
        jpeg = io.BytesIO()
        resized.save(jpeg, "JPEG")
        return jpeg.getvalue()

    def _require_reachable(self, operation: str) -> None:
        if not self.reachable:
            raise ExternalServiceException(operation, _UNREACHABLE)
