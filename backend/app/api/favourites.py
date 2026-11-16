"""The household's favourites (``docs/api-design.md`` section 3.6)."""

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify, request
from spectree import Response as SpectreeResponse

from app.models import ItemKind
from app.schemas.common import ApiErrorResponseSchema
from app.schemas.favourite_schema import (
    FavouriteListQuerySchema,
    FavouritePageSchema,
    FavouriteRequestSchema,
    FavouriteSchema,
)
from app.services.container import ServiceContainer
from app.services.favourite_service import FavouriteService
from app.utils.pagination import build_pagination_meta
from app.utils.spectree_config import api

favourites_bp = Blueprint("favourites", __name__, url_prefix="/favourites")


@favourites_bp.route("", methods=["GET"])
@api.validate(
    query=FavouriteListQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=(FavouritePageSchema, "A page of the favourites."),
        HTTP_400=(
            ApiErrorResponseSchema,
            "`page`, `per_page`, `sort_by` or `sort_dir` holds a value the "
            "list does not take.",
        ),
    ),
)
@inject
def list_favourites(
    favourite_service: FavouriteService = Provide[ServiceContainer.favourite_service],
) -> Response:
    """The household's favourites, each with its item as Plex has it now.

    A favourite is `available` when Plex has its item. One whose item Plex
    does not have is listed with its title and subtitle, without `item_id`
    and `artwork_id`, and is kept. While Plex fails the favourites are
    still listed, none of them `available`.
    """
    query = FavouriteListQuerySchema.model_validate(request.args.to_dict())
    favourites, total = favourite_service.list_favourites(query)
    page = FavouritePageSchema(
        data=[FavouriteSchema.model_validate(favourite) for favourite in favourites],
        meta=build_pagination_meta(
            total, query.page, query.per_page, query.sort_by, query.sort_dir
        ),
    )
    return jsonify(page.model_dump(mode="json"))


@favourites_bp.route("", methods=["POST"])
@api.validate(
    json=FavouriteRequestSchema,
    resp=SpectreeResponse(
        HTTP_200=(
            FavouriteSchema,
            "The item was marked already: the favourite that marks it.",
        ),
        HTTP_201=(FavouriteSchema, "The new favourite."),
        HTTP_400=(
            ApiErrorResponseSchema,
            "The body is not a JSON object, lacks `kind` or `item_id`, holds "
            "a `kind` other than `artist`, `album` or `playlist`, a track's "
            "among them, or a field that is not one of the two.",
        ),
        HTTP_404=(
            ApiErrorResponseSchema,
            "Plex has no item of this `kind` with this `item_id`. The code is "
            "`ITEM_NOT_FOUND`.",
        ),
        HTTP_502=(ApiErrorResponseSchema, "Plex failed or did not answer."),
    ),
)
@inject
def mark_favourite(
    favourite_service: FavouriteService = Provide[ServiceContainer.favourite_service],
) -> tuple[Response, int]:
    """Mark an artist, an album or a playlist of the library as a favourite.

    The item is read from Plex as it is at this request. Marking an item
    that is marked already is no error, so that two clients may mark one
    item at once: the answer is the favourite that marks it. The two copies
    of a release the library holds twice share one favourite.
    """
    marking = FavouriteRequestSchema.model_validate(request.get_json())
    favourite, created = favourite_service.mark(ItemKind(marking.kind), marking.item_id)
    answer = FavouriteSchema.model_validate(favourite).model_dump(mode="json")
    return jsonify(answer), 201 if created else 200


@favourites_bp.route("/<int:favourite_id>", methods=["DELETE"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_204=(None, "The mark is removed."),
        HTTP_404=(
            ApiErrorResponseSchema,
            "No favourite has this id. The code is `RECORD_NOT_FOUND`.",
        ),
    ),
    path_parameter_descriptions={"favourite_id": "The favourite: its `id`."},
)
@inject
def unmark_favourite(
    favourite_id: int,
    favourite_service: FavouriteService = Provide[ServiceContainer.favourite_service],
) -> tuple[str, int]:
    """Remove a favourite: the mark on its item. Plex is not asked."""
    favourite_service.unmark(favourite_id)
    return "", 204
