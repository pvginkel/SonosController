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

import os
from collections.abc import Generator

import pytest
from flask import Flask

from app.app_config import AppSettings

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
