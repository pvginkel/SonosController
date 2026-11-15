"""The artwork endpoint (``docs/api-design.md`` section 3.7)."""

from typing import Any

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, request
from spectree import Response as SpectreeResponse

from app.schemas.artwork_schema import ArtworkQuerySchema
from app.schemas.common import ApiErrorResponseSchema
from app.services.artwork_service import ArtworkFormat, ArtworkService
from app.services.container import ServiceContainer
from app.utils.spectree_config import api

ARTWORK_PATH = "/api/artwork/{artwork_id}"

CACHE_CONTROL = "public, max-age=31536000, immutable"

_MIMETYPES = {
    ArtworkFormat.JPEG: "image/jpeg",
    ArtworkFormat.LVGL: "application/octet-stream",
}

artwork_bp = Blueprint("artwork", __name__, url_prefix="/artwork")


@artwork_bp.route("/<artwork_id>", methods=["GET"])
@api.validate(
    query=ArtworkQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=None,
        HTTP_400=ApiErrorResponseSchema,
        HTTP_404=ApiErrorResponseSchema,
        HTTP_502=ApiErrorResponseSchema,
    ),
)
@inject
def get_artwork(
    artwork_id: str,
    artwork_service: ArtworkService = Provide[ServiceContainer.artwork_service],
) -> Response:
    """A picture, as a square of the size asked for.

    An artwork id names one picture: when the picture changes, so does the
    id, and two things with the same picture carry the same id, as an album
    and its tracks do. A client can therefore keep a picture by id, size and
    format for as long as it likes, and the answer's `Cache-Control` says so.

    The picture is scaled to cover the square and cropped at the centre.

    `format=jpeg` answers `image/jpeg`.

    `format=lvgl` answers `application/octet-stream` with `Content-Length`:
    LVGL 9's binary image at exactly `size` × `size`, which a display draws
    without decoding. It is a 12-byte header and then the pixels, row by
    row, 2 bytes each. The header, little-endian: the magic byte `0x19`, the
    colour format `0x12` (RGB565), 16 bits of flags (`0`), then the width,
    the height and the stride (2 × `size`) as 16 bits each, and 16 bits
    reserved (`0`). A pixel is RGB565, little-endian, with no alpha. The
    pixels are not compressed, and they are dithered (ordered, 8 × 8).

    `400`: `size` or `format` is missing, malformed or out of range.

    `404`: no picture has this id; a client shows its placeholder. It is also
    the answer for the picture of something that plays from another source
    than the library, such as what the Sonos app started, once that is no
    longer what its speaker has loaded.

    `502`: Plex or the speaker did not give the picture.
    """
    query = ArtworkQuerySchema.model_validate(request.args.to_dict())
    content = artwork_service.read(artwork_id, query.size, query.format)
    response = Response(content, mimetype=_MIMETYPES[query.format])
    response.headers["Cache-Control"] = CACHE_CONTROL
    return response


def describe_artwork(document: dict[str, Any]) -> None:
    """Have an OpenAPI document say what the route cannot: what its path
    parameter is, and that its ``200`` is a picture."""
    # Spectree documents a validated route for the first app of a process
    # alone: the document of a later app, as a test suite builds them, has
    # no artwork path.
    if ARTWORK_PATH not in document["paths"]:
        return
    operation = document["paths"][ARTWORK_PATH]["get"]
    (artwork_id,) = (
        parameter for parameter in operation["parameters"] if parameter["in"] == "path"
    )
    artwork_id["description"] = (
        "The `artwork_id` of whatever carries one, such as a playback state's "
        "track. It is opaque: a client builds none and reads nothing from one."
    )
    operation["responses"]["200"] = {
        "description": (
            "The picture: `image/jpeg` for `format=jpeg`, "
            "`application/octet-stream` for `format=lvgl`."
        ),
        "headers": {
            "Cache-Control": {
                "description": (
                    f"`{CACHE_CONTROL}`: the picture of an id never changes."
                ),
                "schema": {"type": "string"},
            }
        },
        "content": {
            mimetype: {"schema": {"type": "string", "format": "binary"}}
            for mimetype in _MIMETYPES.values()
        },
    }
