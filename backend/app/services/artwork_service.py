"""Artwork: a picture by its id, as a square in the form a client draws.

``docs/api-design.md`` section 3.7. A Plex item's picture is read from Plex,
which resizes it; anything else that plays has the picture its speaker
serves, for as long as that track is the one loaded. The picture is scaled to
cover the square and cropped at the centre, and leaves as a JPEG or as LVGL
9's binary image, which a display draws without decoding: RGB565, dithered,
written by LVGL's own converter.

Finished pictures are kept in memory by id, size and format, the newest
``CACHE_BYTES`` of them.
"""

import io
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from app.core_exceptions import ExternalServiceException
from app.exceptions import RecordNotFoundException
from app.services.household_mirror import HouseholdMirror
from app.services.plex_client import PlexClient
from app.services.sonos_client import SonosClient
from app.utils.artwork_id import PlexPicture, SpeakerPicture, parse_artwork_id
from app.utils.lvgl import ColorFormat, CompressMethod, LVGLImage

SIZE_MIN = 16
JPEG_SIZE_MAX = 1024
LVGL_SIZE_MAX = 480

CACHE_BYTES = 16 * 1024 * 1024

_JPEG_QUALITY = 85

# What Pillow raises for bytes it cannot make a picture of.
_PILLOW_ERRORS = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)

# The 8 × 8 Bayer matrix, as the part of a level each pixel is moved by before
# it is rounded down: between -0.5 and 0.5, and never either.
_BAYER = np.array(
    [
        [0, 32, 8, 40, 2, 34, 10, 42],
        [48, 16, 56, 24, 50, 18, 58, 26],
        [12, 44, 4, 36, 14, 46, 6, 38],
        [60, 28, 52, 20, 62, 30, 54, 22],
        [3, 35, 11, 43, 1, 33, 9, 41],
        [51, 19, 59, 27, 49, 17, 57, 25],
        [15, 47, 7, 39, 13, 45, 5, 37],
        [63, 31, 55, 23, 61, 29, 53, 21],
    ],
    dtype=np.float32,
)
_DITHER = (_BAYER + 0.5) / 64 - 0.5


class ArtworkFormat(StrEnum):
    """The form a picture leaves in."""

    JPEG = "jpeg"
    LVGL = "lvgl"


class ArtworkService:
    """Pictures by artwork id."""

    def __init__(
        self,
        household_mirror: HouseholdMirror,
        sonos_client: SonosClient,
        plex_client: PlexClient,
        cache_bytes: int = CACHE_BYTES,
    ) -> None:
        self._mirror = household_mirror
        self._sonos = sonos_client
        self._plex = plex_client
        self._cache_bytes = cache_bytes
        self._lock = threading.Lock()
        # Guarded by _lock; the picture used last is last.
        self._cache: OrderedDict[tuple[str, int, ArtworkFormat], bytes] = OrderedDict()

    def read(self, artwork_id: str, size: int, artwork_format: ArtworkFormat) -> bytes:
        """The picture an id names, ``size`` × ``size`` pixels.

        Raises:
            RecordNotFoundException: Nothing made this id, Plex has no picture
                there, or the id names a speaker's picture of a track that is
                no longer the one loaded.
            ExternalServiceException: Plex or the speaker failed, or gave
                something that is not a picture.
        """
        picture = parse_artwork_id(artwork_id)
        if isinstance(picture, PlexPicture):
            path = picture.path
            return self._finished(
                artwork_id,
                size,
                artwork_format,
                f"Plex's picture {path}",
                lambda: self._plex.read_picture(path, size),
            )
        if isinstance(picture, SpeakerPicture):
            speaker_id = picture.speaker_id
            # A speaker serves a picture after its track stopped being the
            # one loaded, and so would the cache.
            address = self._mirror.speaker_picture(speaker_id, artwork_id)
            if address is not None:
                return self._finished(
                    artwork_id,
                    size,
                    artwork_format,
                    f"the picture speaker {speaker_id} serves",
                    lambda: self._sonos.read_picture(speaker_id, address),
                )
        raise RecordNotFoundException("Artwork", artwork_id)

    def _finished(
        self,
        artwork_id: str,
        size: int,
        artwork_format: ArtworkFormat,
        origin: str,
        read_source: Callable[[], bytes | None],
    ) -> bytes:
        """The finished picture: the one kept, or one made from its source
        and kept."""
        key = (artwork_id, size, artwork_format)
        with self._lock:
            kept = self._cache.get(key)
            if kept is not None:
                self._cache.move_to_end(key)
                return kept

        source = read_source()
        if source is None:
            raise RecordNotFoundException("Artwork", artwork_id)
        square = _square(source, size, origin)
        content = (
            _jpeg(square) if artwork_format is ArtworkFormat.JPEG else _lvgl(square)
        )

        with self._lock:
            self._cache[key] = content
            self._cache.move_to_end(key)
            while sum(len(kept) for kept in self._cache.values()) > self._cache_bytes:
                self._cache.popitem(last=False)
        return content


def _square(source: bytes, size: int, origin: str) -> Image.Image:
    """The picture scaled to cover a square, cropped at the centre."""
    try:
        with Image.open(io.BytesIO(source)) as image:
            rgb = image.convert("RGB")
    except _PILLOW_ERRORS as exc:
        raise ExternalServiceException(
            f"prepare {origin}", f"it is not a picture ({type(exc).__name__})"
        ) from exc
    return ImageOps.fit(rgb, (size, size), Image.Resampling.LANCZOS)


def _jpeg(square: Image.Image) -> bytes:
    content = io.BytesIO()
    square.save(content, "JPEG", quality=_JPEG_QUALITY)
    return content.getvalue()


def _lvgl(square: Image.Image) -> bytes:
    """LVGL 9's binary image: its 12-byte header, then the pixels as RGB565,
    uncompressed."""
    image = LVGLImage().set_data(
        ColorFormat.RGB565, square.width, square.height, _rgb565(np.asarray(square))
    )
    # The converter writes to a file and to nothing else.
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "artwork.bin"
        image.to_bin(str(file), CompressMethod.NONE)
        return file.read_bytes()


def _rgb565(rgb: NDArray[np.uint8]) -> bytes:
    """The pixels as little-endian RGB565, with ordered dithering.

    Each channel is moved by the Bayer matrix's part of a level before it is
    rounded down to its 5 or 6 bits, so that an area of one colour averages
    to that colour (section 1.9). A value that is a level stays on it.
    """
    height, width, _ = rgb.shape
    dither = np.tile(_DITHER, (height // 8 + 1, width // 8 + 1))[:height, :width]

    def levels(channel: int, top: int) -> NDArray[np.uint16]:
        level = rgb[:, :, channel].astype(np.float32) * (top / 255)
        return np.clip(np.floor(level + dither + 0.5), 0, top).astype(np.uint16)

    packed = (levels(0, 31) << 11) | (levels(1, 63) << 5) | levels(2, 31)
    return packed.astype("<u2").tobytes()
