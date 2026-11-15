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
import uuid
from collections.abc import Callable, Generator
from queue import Empty
from typing import Any

import pytest
from flask import Flask

from app.app_config import AppSettings
from app.services.event_sender import COALESCE_SECONDS
from app.services.household_mirror import CHECK_SECONDS, HouseholdMirror
from app.services.sse_connection_manager import SSEConnection

# Import all infrastructure fixtures
from tests.conftest_infrastructure import *  # noqa: F403
from tests.fakes.sonos_client import FakeSonosClient

# conftest_infrastructure has loaded .env into the environment, and in a
# SonosController environment .env names the real household. A process the
# suite starts gets these instead: TEST-NET-1 (RFC 5737) and .invalid
# (RFC 2606) reach nothing.
os.environ["SONOS_SEED_HOSTS"] = "192.0.2.1"
os.environ["SONOS_EVENT_ADVERTISE_HOST"] = "sonos-events.invalid"


@pytest.fixture(scope="session")
def test_app_settings() -> AppSettings:
    """The app settings of the suite's apps: no speaker is behind them."""
    return AppSettings(
        sonos_seed_hosts=["192.0.2.1"],
        sonos_event_advertise_host="sonos-events.invalid",
    )


@pytest.fixture(autouse=True)
def sonos(app: Flask) -> Generator[FakeSonosClient]:
    """The household every test runs against: the in-memory fake, empty.

    It overrides the container's Sonos client for every test, so that no test
    reaches a speaker; ``session_app_isolation`` undoes the override. The live
    suite replaces this fixture with the real client.
    """
    fake = FakeSonosClient(health_service=app.container.health_service())
    app.container.sonos_client.override(fake)
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def check_seconds() -> float:
    """The time between two reads of the layout by the test's mirror: the
    app's, which no test waits for. A test of what those reads heal replaces
    this fixture with a time it can wait for."""
    return CHECK_SECONDS


@pytest.fixture
def mirror(
    app: Flask, sonos: FakeSonosClient, check_seconds: float
) -> Generator[HouseholdMirror]:
    """The container's household mirror, on the test's household.

    Not started: a test builds its household and calls ``start()``. The
    container gives every test a mirror and an event sender of its own, so
    that neither holds what an earlier test left.
    """
    container = app.container
    container.event_sender.reset()
    container.household_mirror.reset()
    mirror = container.household_mirror(check_seconds=check_seconds)
    try:
        yield mirror
    finally:
        mirror.stop()
        container.event_sender().stop()
        container.household_mirror.reset()
        container.event_sender.reset()


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
