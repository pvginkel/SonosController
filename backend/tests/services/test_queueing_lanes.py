"""Tests for the queueing lanes, on a task service of their own and tasks
that do nothing but take their turns.

The queue service's tests drive the lanes with queueing tasks on the fake
household; these hold what those cannot reach: a task ended before a worker
took it, one cancelled through the task service, and a task the task service
refuses.
"""

import threading
from collections.abc import Generator
from typing import Any

import pytest
from flask import Flask
from pydantic import BaseModel

from app.exceptions import InvalidOperationException
from app.services.base_task import BaseTask, ProgressHandle
from app.services.queueing_lanes import QueueingLanes
from app.services.task_service import TaskService
from app.utils.lifecycle_coordinator import LifecycleCoordinator, LifecycleEvent

GROUP = "RINCON_FAKE0000000101400"
OTHER_GROUP = "RINCON_FAKE0000000201400"


class Done(BaseModel):
    pass


class Turn(BaseTask):
    """Writes its name down when its turn comes, and ``/`` and its name when
    the turn ends; with ``held``, it holds its turn until ``release()``."""

    def __init__(
        self,
        lanes: QueueingLanes,
        group_id: str,
        name: str,
        taken: list[str],
        held: bool = False,
    ) -> None:
        super().__init__()
        self._lanes = lanes
        self._group_id = group_id
        self._name = name
        self._taken = taken
        self._holding = threading.Event()
        self._released = threading.Event()
        if not held:
            self._released.set()

    def execute(self, progress_handle: ProgressHandle, **kwargs: Any) -> Done:
        with self._lanes.turn(self._group_id, self):
            if not self.is_cancelled:
                self._taken.append(self._name)
                self._holding.set()
                assert self._released.wait(5.0), "the turn was not released"
                self._taken.append(f"/{self._name}")
        return Done()

    def wait(self) -> None:
        """Wait until the task holds its turn."""
        assert self._holding.wait(5.0), "the turn did not come"

    def release(self) -> None:
        self._released.set()


@pytest.fixture
def workers() -> int:
    return 4


@pytest.fixture
def coordinator() -> LifecycleCoordinator:
    return LifecycleCoordinator(graceful_shutdown_timeout=5)


@pytest.fixture
def task_service(
    app: Flask, coordinator: LifecycleCoordinator, workers: int
) -> Generator[TaskService]:
    """A task service of the test's own, whose events reach the ``tasks``
    fixture's connection."""
    service = TaskService(
        lifecycle_coordinator=coordinator,
        sse_connection_manager=app.container.sse_connection_manager(),
        max_workers=workers,
    )
    try:
        yield service
    finally:
        service.shutdown()


@pytest.fixture
def lanes(task_service: TaskService) -> QueueingLanes:
    return QueueingLanes(task_service)


@pytest.fixture
def held(lanes: QueueingLanes) -> Generator[list[Turn]]:
    """The held tasks a test makes, released after it."""
    turns: list[Turn] = []
    try:
        yield turns
    finally:
        for turn in turns:
            turn.release()


@pytest.fixture
def taken() -> list[str]:
    return []


@pytest.fixture
def first(lanes: QueueingLanes, held: list[Turn], taken: list[str]) -> Turn:
    """A task that holds its turn on the group."""
    task = Turn(lanes, GROUP, "first", taken, held=True)
    held.append(task)
    lanes.start(GROUP, task, replaces=False)
    task.wait()
    return task


def completed(events: list[dict]) -> bool:
    return [event["event_type"] for event in events] == [
        "task_started",
        "task_completed",
    ]


class TestQueueingLanesStart:
    def test_it_starts_the_task_on_the_task_service(
        self, lanes, task_service, taken, tasks
    ):
        started = lanes.start(GROUP, Turn(lanes, GROUP, "one", taken), replaces=False)

        assert started.status == "pending"
        assert completed(tasks.finish(started.task_id))
        assert taken == ["one", "/one"]
        status = task_service.get_task_status(started.task_id)
        assert status is not None and status.status == "completed"

    def test_a_task_the_task_service_refuses_ends_nothing(
        self, lanes, coordinator, first, taken
    ):
        # What the coordinator's shutdown() sends first, without the rest.
        coordinator._raise_lifecycle_event(LifecycleEvent.PREPARE_SHUTDOWN)

        with pytest.raises(InvalidOperationException) as raised:
            lanes.start(GROUP, Turn(lanes, GROUP, "refused", taken), replaces=True)

        assert raised.value.message == (
            "Cannot start task because service is shutting down"
        )
        assert first.is_cancelled is False


class TestQueueingLanesTurn:
    def test_a_groups_tasks_take_turns_one_at_a_time_in_the_order_started(
        self, lanes, first, taken, tasks
    ):
        started = [
            lanes.start(GROUP, Turn(lanes, GROUP, name, taken), replaces=False)
            for name in ("second", "third", "fourth")
        ]
        for task in started:
            tasks.started(task.task_id)

        assert taken == ["first"]
        first.release()
        for task in started:
            assert completed(tasks.finish(task.task_id))
        assert taken == [
            "first",
            "/first",
            "second",
            "/second",
            "third",
            "/third",
            "fourth",
            "/fourth",
        ]

    def test_another_groups_tasks_take_their_turns_meanwhile(
        self, lanes, first, taken, tasks
    ):
        other = lanes.start(
            OTHER_GROUP, Turn(lanes, OTHER_GROUP, "other", taken), replaces=False
        )

        assert completed(tasks.finish(other.task_id))
        assert taken == ["first", "other", "/other"]

    def test_a_replacing_task_ends_the_task_in_its_turn_and_those_waiting(
        self, lanes, first, taken, tasks
    ):
        waiting = [
            lanes.start(GROUP, Turn(lanes, GROUP, name, taken), replaces=replaces)
            for name, replaces in (("waiting", False), ("replaced", True))
        ]
        for task in waiting:
            tasks.started(task.task_id)

        replacing = lanes.start(
            GROUP, Turn(lanes, GROUP, "replacing", taken), replaces=True
        )

        # At once, while the first still holds its turn.
        for task in waiting:
            assert completed(tasks.finish(task.task_id))
        assert first.is_cancelled is True
        first.release()
        assert completed(tasks.finish(replacing.task_id))
        assert taken == ["first", "/first", "replacing", "/replacing"]

    @pytest.mark.parametrize("workers", [1])
    def test_a_task_ended_before_a_worker_took_it_takes_no_turn(
        self, lanes, first, taken, tasks, task_service
    ):
        ended = lanes.start(GROUP, Turn(lanes, GROUP, "ended", taken), replaces=False)
        replacing = lanes.start(
            GROUP, Turn(lanes, GROUP, "replacing", taken), replaces=True
        )
        # The one worker holds the first's turn: neither has started.
        status = task_service.get_task_status(ended.task_id)
        assert status is not None and status.status == "pending"

        first.release()

        assert completed(tasks.finish(ended.task_id))
        assert completed(tasks.finish(replacing.task_id))
        assert taken == ["first", "/first", "replacing", "/replacing"]

    def test_a_task_cancelled_through_the_task_service_gives_way(
        self, lanes, first, taken, tasks, task_service
    ):
        cancelled = lanes.start(
            GROUP, Turn(lanes, GROUP, "cancelled", taken), replaces=False
        )
        tasks.started(cancelled.task_id)
        later = lanes.start(GROUP, Turn(lanes, GROUP, "later", taken), replaces=False)
        tasks.started(later.task_id)

        assert task_service.cancel_task(cancelled.task_id) is True
        first.release()

        # The task service sends no event for a task it cancelled.
        assert completed(tasks.finish(later.task_id))
        assert taken == ["first", "/first", "later", "/later"]
