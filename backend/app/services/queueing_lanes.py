"""The queueing tasks and the Clears of each group, run one at a time in the
order asked (``docs/api-design.md`` section 3.4).

A group's queue is its coordinator's, so a task or a Clear takes its turn in
the lane of the coordinator its room had when it was asked. A task that
replaces the queue, a Play or a Shuffle, and a Clear end every task asked
before them on the group: one still waiting for its turn makes no call to a
speaker, and the one in its turn stops before its next call. Their turn comes
once that one has left the lane, so nothing of an earlier item lands after
them. A Clear is never ended: it is one call, made in its turn on the thread
that asked it, and what is asked after it lands after it.

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


class _Clear:
    """A Clear's place in its lane."""

    is_cancelled = False


@dataclass
class _Lane:
    waiting: list[BaseTask | _Clear] = field(default_factory=list)
    running: BaseTask | _Clear | None = None

    def end_tasks(self) -> None:
        """End every task in the lane, the one in its turn too; a Clear
        keeps its place."""
        for holder in self.waiting:
            if isinstance(holder, BaseTask):
                holder.cancel()
        self.waiting = [clear for clear in self.waiting if isinstance(clear, _Clear)]
        if isinstance(self.running, BaseTask):
            self.running.cancel()


class QueueingLanes:
    """Gives each group's queueing tasks their turns."""

    def __init__(self, task_service: TaskService) -> None:
        self._tasks = task_service
        self._changed = threading.Condition()
        self._lanes: dict[str, _Lane] = {}

    def start(self, group_id: str, task: BaseTask, replaces: bool) -> TaskStartResponse:
        """Start the task, to take its turn on the group after every task and
        Clear asked before it; with ``replaces``, end those tasks.

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
                lane.end_tasks()
            lane.waiting.append(task)
            self._changed.notify_all()
        return started

    @contextmanager
    def turn(self, group_id: str, task: BaseTask) -> Iterator[None]:
        """Wait for the task's turn on the group, and hold it until the block
        ends.

        The turn comes once every task and Clear asked before it has left the
        lane, or at once for a task that was ended: the block then reads
        ``task.is_cancelled`` and makes no call.
        """
        with self._changed:
            lane = self._lanes.setdefault(group_id, _Lane())
            self._wait_for_turn(lane, task)
        try:
            yield
        finally:
            self._leave(lane, task)

    @contextmanager
    def clear_turn(self, group_id: str) -> Iterator[None]:
        """End every task asked before on the group, as a task that replaces
        the queue does; then wait for a turn of the caller's own on the group,
        and hold it until the block ends.

        The turn comes once the task in its turn and every Clear asked before
        have left the lane, and every task asked from now on waits for it.
        """
        clear = _Clear()
        with self._changed:
            lane = self._lanes.setdefault(group_id, _Lane())
            lane.end_tasks()
            lane.waiting.append(clear)
            self._changed.notify_all()
            self._wait_for_turn(lane, clear)
        try:
            yield
        finally:
            self._leave(lane, clear)

    def _wait_for_turn(self, lane: _Lane, holder: BaseTask | _Clear) -> None:
        """Wait, holding ``_changed``, until the holder is first in the lane
        and nothing holds the turn, or it is ended; take the turn unless it
        was ended."""
        while not holder.is_cancelled and (
            lane.running is not None or lane.waiting[0] is not holder
        ):
            self._changed.wait()
        # A task ended through the task service's cancel is still in its
        # lane.
        if holder in lane.waiting:
            lane.waiting.remove(holder)
        if not holder.is_cancelled:
            lane.running = holder

    def _leave(self, lane: _Lane, holder: BaseTask | _Clear) -> None:
        with self._changed:
            if lane.running is holder:
                lane.running = None
            self._changed.notify_all()
