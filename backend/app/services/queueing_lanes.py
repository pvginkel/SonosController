"""The queueing tasks of each group, run one at a time in the order asked
(``docs/api-design.md`` section 3.4).

A group's queue is its coordinator's, so a task takes its turn in the lane of
the coordinator its room had when the task was asked. A task that replaces the queue, a Play or a Shuffle, ends
every task asked before it on the group: one still waiting for its turn makes
no call to a speaker, and the one in its turn stops before its next call. The
replacing task's turn comes once that one has left the lane, so nothing of an
earlier item lands after it.

A task that is ended returns, and the task service reports it completed:
nothing failed, and a client watching its id stops waiting. Ending is
``BaseTask.cancel()``, which a task reads as ``is_cancelled``.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.schemas.task_schema import TaskStartResponse
from app.services.base_task import BaseTask
from app.services.task_service import TaskService


@dataclass
class _Lane:
    waiting: list[BaseTask] = field(default_factory=list)
    running: BaseTask | None = None


class QueueingLanes:
    """Gives each group's queueing tasks their turns."""

    def __init__(self, task_service: TaskService) -> None:
        self._tasks = task_service
        self._changed = threading.Condition()
        self._lanes: dict[str, _Lane] = {}

    def start(self, group_id: str, task: BaseTask, replaces: bool) -> TaskStartResponse:
        """Start the task, to take its turn on the group after every task
        asked before it; with ``replaces``, end those tasks.

        The task's ``execute()`` runs inside ``turn()``.

        Raises:
            InvalidOperationException: The task service is shutting down;
                no lane changed.
        """
        with self._changed:
            # Started under the lock: the task service hands its tasks to
            # its workers in the order they were started, so every task
            # ahead of a waiting one in its lane already has a worker.
            # The app has no users: the task's events go to the local
            # admin, which every connection is (SSE-3).
            started = self._tasks.start_task(task, caller_subject=None)
            lane = self._lanes.setdefault(group_id, _Lane())
            if replaces:
                for earlier in lane.waiting:
                    earlier.cancel()
                lane.waiting.clear()
                if lane.running is not None:
                    lane.running.cancel()
            lane.waiting.append(task)
            self._changed.notify_all()
        return started

    @contextmanager
    def turn(self, group_id: str, task: BaseTask) -> Iterator[None]:
        """Wait for the task's turn on the group, and hold it until the block
        ends.

        The turn comes once every task asked before it has left the lane, or
        at once for a task that was ended: the block then reads
        ``task.is_cancelled`` and makes no call.
        """
        with self._changed:
            lane = self._lanes.setdefault(group_id, _Lane())
            while not task.is_cancelled and (
                lane.running is not None or lane.waiting[0] is not task
            ):
                self._changed.wait()
            # A task ended through the task service's cancel is still in
            # its lane.
            if task in lane.waiting:
                lane.waiting.remove(task)
            if not task.is_cancelled:
                lane.running = task
        try:
            yield
        finally:
            with self._changed:
                if lane.running is task:
                    lane.running = None
                self._changed.notify_all()
