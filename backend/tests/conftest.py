"""Pytest configuration and fixtures.

Infrastructure fixtures (app, client, session, OIDC, SSE) are defined in
conftest_infrastructure.py. This file re-exports them and adds app-specific
domain fixtures.

A fixture defined here after the import replaces the template's fixture of
the same name. To change the settings the test apps are built from, replace
``test_settings`` or ``test_app_settings``. With a database they are
session-scoped, because the app is, and a replacement keeps that scope.

A process the suite starts builds its app from the environment and ``.env``
alone, not from these fixtures (``tests/test_openapi_cache.py`` builds one
for the OpenAPI document). A setting the app cannot start without, which
only a fixture here gives, gets a default for those processes at the top of
this file, after the imports: ``os.environ.setdefault("MY_SETTING", "value")``.
Without one the suite passes where ``.env`` sets it, and fails in CI.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from queue import Empty
from typing import Any

import pytest
from flask import Flask

from app.app_config import AppSettings
from app.services.artwork_service import ArtworkService
from app.services.event_sender import COALESCE_SECONDS
from app.services.household_mirror import CHECK_SECONDS, HouseholdMirror
from app.services.sse_connection_manager import SSEConnection
from app.services.task_service import TaskService

# Import all infrastructure fixtures
from tests.conftest_infrastructure import *  # noqa: F403
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import FakeSonosClient

# conftest_infrastructure has loaded .env into the environment, and in a
# SonosController environment .env names the real household and the real
# Plex, with its token. A process the suite starts gets these instead:
# TEST-NET-1 (RFC 5737) and .invalid (RFC 2606) reach nothing.
os.environ["SONOS_SEED_HOSTS"] = "192.0.2.1"
os.environ["SONOS_EVENT_ADVERTISE_HOST"] = "sonos-events.invalid"
os.environ["PLEX_URL"] = "http://192.0.2.1:32400"
os.environ["PLEX_TOKEN"] = "the-suites-plex-token"
os.environ["PLEX_LIBRARY"] = "Music"


_BACKEND_DIR = Path(__file__).resolve().parent.parent

_DUMP_DOCUMENT = """
import json
import sys

from app import create_app

app = create_app(skip_background_services=True)
response = app.test_client().get("/api/docs/openapi.json")
if response.status_code != 200:
    sys.exit(f"GET /api/docs/openapi.json answered {response.status_code}")
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(response.get_json(), f)
"""


@pytest.fixture(scope="session")
def openapi_document(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The app's OpenAPI document, as a deployed backend serves it.

    Spectree documents a route under ``@api.validate`` for the first app of
    a process alone, and the suite creates many: the document the ``client``
    fixture gets has none of them. This one comes from an app built in a
    process of its own, from the environment set above.
    """
    document_file = tmp_path_factory.mktemp("openapi") / "openapi.json"
    result = subprocess.run(
        [sys.executable, "-c", _DUMP_DOCUMENT, str(document_file)],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    document: dict[str, Any] = json.loads(document_file.read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="session")
def test_app_settings() -> AppSettings:
    """The app settings of the suite's apps: no speaker and no Plex is behind
    them."""
    return AppSettings(
        sonos_seed_hosts=["192.0.2.1"],
        sonos_event_advertise_host="sonos-events.invalid",
        plex_url="http://192.0.2.1:32400",
        plex_token="the-suites-plex-token",
        plex_library="Music",
    )


@pytest.fixture(autouse=True)
def sonos(app: Flask, plex: FakePlexClient) -> Generator[FakeSonosClient]:
    """The household every test runs against: the in-memory fake, empty, its
    Plex service serving the test's Plex.

    It overrides the container's Sonos client for every test, so that no test
    reaches a speaker; ``session_app_isolation`` undoes the override. The live
    suite replaces this fixture with the real client.
    """
    fake = FakeSonosClient(health_service=app.container.health_service(), plex=plex)
    app.container.sonos_client.override(fake)
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture(autouse=True)
def plex(app: Flask) -> Generator[FakePlexClient]:
    """The Plex every test runs against: the in-memory fake, its library
    empty.

    It overrides the container's Plex client for every test, so that no test
    reaches Plex, and undoes the override itself: pytest sets the autouse
    fixtures of this file up by name, this one before
    ``session_app_isolation``, which therefore takes the override for what
    it is to keep. The live suite replaces this fixture with the real client.
    """
    fake = FakePlexClient(health_service=app.container.health_service())
    app.container.plex_client.override(fake)
    try:
        yield fake
    finally:
        app.container.plex_client.reset_last_overriding()


@pytest.fixture
def check_seconds() -> float:
    """The time between two reads of the layout by the test's mirror: the
    app's, which no test waits for. A test of what those reads heal replaces
    this fixture with a time it can wait for."""
    return CHECK_SECONDS


@pytest.fixture
def in_step() -> bool:
    """Whether the household delivers its events in step with the test's
    mirror: each one once the mirror's healing thread has applied what the
    events before it handed it. A test of what the mirror does while that
    thread is busy replaces this fixture with False, and waits for the
    thread itself, with the mirror's ``wait_until_applied()``."""
    return True


@pytest.fixture
def mirror(
    app: Flask,
    sonos: FakeSonosClient,
    plex: FakePlexClient,
    check_seconds: float,
    in_step: bool,
) -> Generator[HouseholdMirror]:
    """The container's household mirror, on the test's household and library.

    Not started: a test builds its household and calls ``start()``. The
    household delivers its events in step with this mirror (``in_step``), so
    its ``settle()`` waits as well for the layouts the speakers sent, which
    the mirror's healing thread applies. The container gives every test a
    mirror and an event sender of its own, so that neither holds what an
    earlier test left.
    """
    container = app.container
    container.event_sender.reset()
    container.household_mirror.reset()
    mirror = container.household_mirror(check_seconds=check_seconds)
    if in_step:
        sonos.in_step_with(mirror.wait_until_applied)
    sender = container.event_sender()
    try:
        yield mirror
    finally:
        mirror.stop()
        sender.stop()
        # The app's lifecycle coordinator, which outlives the test, holds
        # what registered with it: the test's mirror and sender, and through
        # them its household and its library with their pictures.
        coordinator = container.lifecycle_coordinator()
        coordinator.unregister_lifecycle_notification(mirror._on_lifecycle_event)
        coordinator.unregister_lifecycle_notification(sender._on_lifecycle_event)
        container.household_mirror.reset()
        container.event_sender.reset()


@pytest.fixture
def artwork(app: Flask, mirror: HouseholdMirror) -> Generator[ArtworkService]:
    """The container's artwork service, on the test's mirror, household and
    library, with no picture kept. It is the one the artwork endpoint uses."""
    container = app.container
    container.artwork_service.reset()
    try:
        yield container.artwork_service()
    finally:
        container.artwork_service.reset()


class EventStream:
    """A client's connection to the event stream: the events it was sent."""

    def __init__(self, connection: SSEConnection) -> None:
        self.connection = connection

    def events(self) -> list[dict[str, Any]]:
        """The events sent so far, and those that follow within the time the
        backend may hold an event back."""
        events = []
        while True:
            try:
                frame = self.connection.next_frame(COALESCE_SECONDS + 0.25)
            except Empty:
                return events
            assert frame is not None, "the connection was closed"
            assert frame.startswith("data: ") and frame.endswith("\n\n")
            events.append(json.loads(frame.removeprefix("data: ")))


class TaskWatch:
    """The events of the tasks a test starts, as a connection to the event
    stream receives them, which every task's events reach. Each task's are
    kept apart, so a test can wait for the tasks it runs side by side in any
    order."""

    def __init__(self, connection: SSEConnection, task_service: TaskService) -> None:
        self._connection = connection
        self._task_service = task_service
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._finished: list[str] = []

    def started(self, task_id: str, timeout: float = 5.0) -> None:
        """Wait for the task's ``task_started``: a worker runs it."""
        self._wait(task_id, ("task_started",), timeout)

    def finish(self, task_id: str, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Wait for the task's last event, ``task_completed`` or
        ``task_failed``, and answer the task's events up to it, each the
        payload of a ``task_event``; the stream's other events are passed
        over."""
        events = self._wait(task_id, ("task_completed", "task_failed"), timeout)
        self._finished.append(task_id)
        return events

    def _wait(
        self, task_id: str, event_types: tuple[str, ...], timeout: float
    ) -> list[dict[str, Any]]:
        deadline = time.perf_counter() + timeout
        events = self._events.setdefault(task_id, [])
        while not any(event["event_type"] in event_types for event in events):
            try:
                frame = self._connection.next_frame(
                    max(0.0, deadline - time.perf_counter())
                )
            except Empty:
                pytest.fail(
                    f"Task {task_id} sent no {' or '.join(event_types)} "
                    f"within {timeout} s"
                )
            assert frame is not None, "the connection was closed"
            message = json.loads(frame.removeprefix("data: "))
            if message["type"] == "task_event":
                payload = message["payload"]
                self._events.setdefault(payload["task_id"], []).append(payload)
        return events

    def remove_finished(self) -> None:
        for task_id in self._finished:
            self._task_service.remove_completed_task(task_id)


@pytest.fixture
def tasks(app: Flask) -> Generator[TaskWatch]:
    """Wait for the tasks a test starts. The connection it watches on is
    opened before the test starts any, and the tasks that ended are removed
    from the task service, which lives for the whole run. The container's
    queueing lanes are reset after the test, so that no task it left takes a
    turn before the next test's."""
    container = app.container
    manager = container.sse_connection_manager()
    connection = manager.on_connect(
        f"tasks-{uuid.uuid4()}", "local-user", local_admin=True
    )
    watch = TaskWatch(connection, container.task_service())
    try:
        yield watch
    finally:
        manager.on_disconnect(connection)
        watch.remove_finished()
        container.queueing_lanes.reset()


@pytest.fixture
def connect(app: Flask) -> Generator[Callable[[], EventStream]]:
    """Open connections to the event stream, as the stream's route does for a
    client: each is the local admin's."""
    manager = app.container.sse_connection_manager()
    streams: list[EventStream] = []

    def connect() -> EventStream:
        connection = manager.on_connect(
            f"test-{uuid.uuid4()}", "local-user", local_admin=True
        )
        streams.append(EventStream(connection))
        return streams[-1]

    try:
        yield connect
    finally:
        for stream in streams:
            manager.on_disconnect(stream.connection)
