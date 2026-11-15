"""Task status, cancel, and remove endpoints.

Every app using the task system needs these. They only talk to TaskService
(pure infrastructure) and are template-owned.

A task that doesn't exist, or that the call can't apply to (cancelling a
finished task, removing an unfinished one), raises RecordNotFoundException,
which the error handlers answer with 404 and the error envelope.
"""

from typing import Any

from dependency_injector.wiring import Provide, inject
from flask import Blueprint
from spectree import Response as SpectreeResponse

from app.exceptions import RecordNotFoundException
from app.schemas.common import ApiErrorResponseSchema
from app.schemas.task_schema import TaskActionResponse, TaskInfo
from app.services.container import ServiceContainer
from app.services.task_service import TaskService
from app.utils.spectree_config import api

tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")


@tasks_bp.route("/<task_id>/status", methods=["GET"])
@api.validate(resp=SpectreeResponse(HTTP_200=TaskInfo, HTTP_404=ApiErrorResponseSchema))
@inject
def get_task_status(
    task_id: str,
    task_service: TaskService = Provide[ServiceContainer.task_service],
) -> Any:
    """Get current status of a task."""
    task_info = task_service.get_task_status(task_id)
    if not task_info:
        raise RecordNotFoundException("Task", task_id)

    return task_info


@tasks_bp.route("/<task_id>/cancel", methods=["POST"])
@api.validate(
    resp=SpectreeResponse(HTTP_200=TaskActionResponse, HTTP_404=ApiErrorResponseSchema)
)
@inject
def cancel_task(
    task_id: str,
    task_service: TaskService = Provide[ServiceContainer.task_service],
) -> Any:
    """Cancel a running task."""
    success = task_service.cancel_task(task_id)
    if not success:
        raise RecordNotFoundException("Cancellable task", task_id)

    return TaskActionResponse(success=True, message="Task cancellation requested")


@tasks_bp.route("/<task_id>", methods=["DELETE"])
@api.validate(
    resp=SpectreeResponse(HTTP_200=TaskActionResponse, HTTP_404=ApiErrorResponseSchema)
)
@inject
def remove_task(
    task_id: str,
    task_service: TaskService = Provide[ServiceContainer.task_service],
) -> Any:
    """Remove a completed task from registry."""
    success = task_service.remove_completed_task(task_id)
    if not success:
        raise RecordNotFoundException("Finished task", task_id)

    return TaskActionResponse(success=True, message="Task removed from registry")
