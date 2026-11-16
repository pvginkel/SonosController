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
    QueueRequestSchema,
)
from app.schemas.task_schema import TaskStartResponse
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


@queue_bp.route("", methods=["POST"])
@api.validate(
    json=QueueRequestSchema,
    resp=SpectreeResponse(
        HTTP_202=(
            TaskStartResponse,
            "The task that queues the item started. Its `task_id` names its "
            "events, `task_event` on the event stream.",
        ),
        HTTP_400=(
            ApiErrorResponseSchema,
            "The body is not a JSON object, lacks `kind`, `item_id` or "
            "`action`, holds a value none of them takes or a field that is "
            "not one of the four, or asks to shuffle a track.",
        ),
        HTTP_404=(
            ApiErrorResponseSchema,
            "No speaker has this id, the code `SPEAKER_NOT_FOUND`; or Plex has "
            "no item of this `kind` with this `item_id`, the code "
            "`ITEM_NOT_FOUND`.",
        ),
        HTTP_409=(
            ApiErrorResponseSchema,
            "The playlist holds no tracks, or more than the backend queues: "
            "the code `PLAYLIST_NOT_PLAYABLE`. Or the backend is shutting "
            "down: `INVALID_OPERATION`.",
        ),
        HTTP_502=(ApiErrorResponseSchema, "Plex failed or did not answer."),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def queue_item(
    speaker_id: str,
    queue_service: QueueService = Provide[ServiceContainer.queue_service],
) -> tuple[Response, int]:
    """Play, shuffle or add an item of the library on the speaker's group.

    `play` and `shuffle` replace the queue and play it from its first track;
    `add` appends and changes nothing else. An artist with `play` or `add`
    is its albums in release order, `order` deciding which comes first, each
    in disc and track order; with `shuffle` it is all its tracks, shuffled.

    The speaker and the item are checked, then a task does the queueing and
    the answer is its `task_id`. Its events, `task_event`, reach every
    connection: `task_completed` when it is done, or `task_failed` with
    `data.error`, the failure in words a client can show. Music starts once
    the item is queued, or an artist's first album, and the rest of the
    artist follows; each step reaches the clients as `queue_changed`. A
    failed task leaves the queue as far as it got.
    """
    queueing = QueueRequestSchema.model_validate(request.get_json())
    started = queue_service.queue(speaker_id, queueing)
    return jsonify(started.model_dump(mode="json")), 202
