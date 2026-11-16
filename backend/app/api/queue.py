"""The queue of a speaker's group (``docs/api-design.md`` section 3.4)."""

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify, request
from spectree import Response as SpectreeResponse

from app.api.speakers import SPEAKER_FAILED, SPEAKER_ID_PARAMETER, SPEAKER_NOT_FOUND
from app.schemas.common import ApiErrorResponseSchema
from app.schemas.queue_schema import (
    QueueEntrySchema,
    QueueListQuerySchema,
    QueuePageSchema,
)
from app.services.container import ServiceContainer
from app.services.queue_service import QueueService
from app.utils.pagination import build_pagination_meta
from app.utils.spectree_config import api

queue_bp = Blueprint("queue", __name__, url_prefix="/speakers/<speaker_id>/queue")


@queue_bp.route("", methods=["GET"])
@api.validate(
    query=QueueListQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=(QueuePageSchema, "A page of the queue of the speaker's group."),
        HTTP_400=(
            ApiErrorResponseSchema,
            "`page`, `per_page`, `sort_by`, `sort_dir` or `locate` holds a "
            "value the list does not take.",
        ),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
        HTTP_502=(
            ApiErrorResponseSchema,
            "The speaker failed or did not answer, or Plex failed to give the "
            "pictures of the page's tracks.",
        ),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def list_queue(
    speaker_id: str,
    queue_service: QueueService = Provide[ServiceContainer.queue_service],
) -> Response:
    """The queue of the speaker's group.

    A queue is a group's: every speaker of a group answers the same. It is
    read from the speaker at every request and listed as the speaker gives
    it: with shuffle on, in the order in which it plays. Whatever queued it,
    the Sonos app included, is listed.

    `current_position` marks the track that is loaded, and `locate=current`
    answers the page that holds it. A client that shows the queue fetches
    this on the event `queue_changed` for the speaker, and on
    `playback_changed` for the track that is loaded.
    """
    query = QueueListQuerySchema.model_validate(request.args.to_dict())
    listing = queue_service.list_queue(speaker_id, query)
    page = QueuePageSchema(
        data=[QueueEntrySchema.model_validate(entry) for entry in listing.entries],
        meta=build_pagination_meta(
            listing.total, listing.page, query.per_page, query.sort_by, query.sort_dir
        ),
        current_position=listing.current_position,
    )
    return jsonify(page.model_dump(mode="json"))


@queue_bp.route("", methods=["DELETE"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_204=(None, "The speaker cleared the queue."),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, SPEAKER_FAILED),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def clear_queue(
    speaker_id: str,
    queue_service: QueueService = Provide[ServiceContainer.queue_service],
) -> tuple[str, int]:
    """Clear the queue of the speaker's group.

    A group that plays from its queue stops. What changed arrives as the
    events `queue_changed` and `playback_changed`.
    """
    queue_service.clear(speaker_id)
    return "", 204
