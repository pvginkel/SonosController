"""Pictures for the tests of artwork: making them, and reading what the
backend made of them."""

import io

import numpy as np
from PIL import Image

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)


def bands(size: tuple[int, int]) -> Image.Image:
    """A picture of three bands along its longer side: red, green, blue.

    The square at the centre of a picture four times as long as it is wide
    lies inside the green band, an eighth of the square from either end.
    """
    width, height = size
    image = Image.new("RGB", size, GREEN)
    if width >= height:
        image.paste(RED, (0, 0, width // 3, height))
        image.paste(BLUE, (width - width // 3, 0, width, height))
    else:
        image.paste(RED, (0, 0, width, height // 3))
        image.paste(BLUE, (0, height - height // 3, width, height))
    return image


def png(image: Image.Image) -> bytes:
    content = io.BytesIO()
    image.save(content, "PNG")
    return content.getvalue()


def from_jpeg(content: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(content))
    assert image.format == "JPEG"
    return image.convert("RGB")


def from_lvgl(content: bytes) -> Image.Image:
    """The picture in LVGL 9's binary image, read as the display reads it:
    the 12-byte header, then RGB565, little-endian, uncompressed."""
    assert content[0] == 0x19, "the magic byte of LVGL 9"
    assert content[1] == 0x12, "the colour format: RGB565"
    assert content[2:4] == b"\x00\x00", "no flags: not compressed"
    width = int.from_bytes(content[4:6], "little")
    height = int.from_bytes(content[6:8], "little")
    assert int.from_bytes(content[8:10], "little") == 2 * width, "the stride"
    assert content[10:12] == b"\x00\x00", "reserved"
    assert len(content) == 12 + 2 * width * height
    pixels = np.frombuffer(content, dtype="<u2", offset=12).reshape(height, width)
    red = (pixels >> 11) * 255 // 31
    green = ((pixels >> 5) & 0x3F) * 255 // 63
    blue = (pixels & 0x1F) * 255 // 31
    return Image.fromarray(np.dstack([red, green, blue]).astype(np.uint8), "RGB")


def levels(content: bytes) -> np.ndarray:
    """The pixels of LVGL's binary image as their 5, 6 and 5 bits: an array
    of height × width × 3."""
    side = int.from_bytes(content[4:6], "little")
    pixels = np.frombuffer(content, dtype="<u2", offset=12).reshape(side, side)
    return np.dstack([pixels >> 11, (pixels >> 5) & 0x3F, pixels & 0x1F])


def assert_colour(
    image: Image.Image, colour: tuple[int, int, int], tolerance: int = 12
) -> None:
    """Every pixel has the colour, within what JPEG and 16 bits change of
    it."""
    difference = np.abs(np.asarray(image).astype(int) - np.array(colour))
    assert difference.max() <= tolerance, (
        f"a pixel is {difference.max()} from {colour}"
    )
