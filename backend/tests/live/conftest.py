"""The live suite: the real Sonos client against the real speakers.

Marked ``live`` and deselected by default; CI never runs it. Run it from a
SonosController environment, from ``backend/``, with the dev stack stopped
(both listen for the speakers' events on one port)::

    cexec python uv run pytest -m live tests/live

It runs on the settings ``kc project setup`` writes to the git-ignored
``.env``; no address is in this repository. The standing rules
(``docs/change-discipline.md``): the Garage speaker only, at low volume, its
queue, position, volume and grouping left as found. Every other room is only
read.

Before the first test it checks that the Garage can take the suite, and stops
the run with how to fix it where it cannot (TEST-2).
"""

from pathlib import Path

import pytest
from dotenv import dotenv_values
from flask import Flask
from pydantic import ValidationError

from app.app_config import AppEnvironment, AppSettings
from app.core_exceptions import ExternalServiceException
from app.exceptions import ConfigurationError
from app.services.sonos_client import SoCoSonosClient, TransportState

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

GARAGE = "Garage"


@pytest.fixture(scope="session")
def live_client(app: Flask) -> SoCoSonosClient:
    """The SoCo client on the household ``.env`` names."""
    try:
        # The file, not the environment: tests/conftest.py points the
        # environment's SONOS_* at nothing.
        settings = AppSettings.load(
            AppEnvironment.model_validate(dotenv_values(_ENV_FILE))
        )
    except (ValidationError, ConfigurationError) as exc:
        pytest.exit(
            f"The live suite reads the household from {_ENV_FILE}, and that "
            f"file does not give it:\n{exc}\n"
            "Run `kc project setup` from the repo root.",
            returncode=1,
        )
    return SoCoSonosClient(
        settings=settings, health_service=app.container.health_service()
    )


@pytest.fixture(scope="session", autouse=True)
def garage(live_client: SoCoSonosClient) -> str:
    """The Garage's speaker id, once the Garage is fit for the suite."""
    try:
        household = live_client.read_household()
    except ExternalServiceException as exc:
        pytest.exit(
            f"No speaker answers: {exc.message}\n"
            f"Switch the Garage speaker on. If the speakers' addresses changed, "
            f"remove SONOS_SEED_HOSTS from {_ENV_FILE} and run `kc project "
            "setup` from the repo root.",
            returncode=1,
        )
    rooms = [room for room in household.rooms if room.name == GARAGE]
    if len(rooms) != 1:
        pytest.exit(
            f"The household has no room named {GARAGE}: its rooms are "
            f"{sorted(room.name for room in household.rooms)}. The live suite "
            "commands that room alone.",
            returncode=1,
        )
    speaker_id = rooms[0].id
    group = next(g for g in household.groups if speaker_id in g.member_ids)
    if group.member_ids != (speaker_id,):
        pytest.exit(
            f"The {GARAGE} is grouped with another room, which the live suite "
            "would command with it. Ungroup it in the Sonos app.",
            returncode=1,
        )
    playback = live_client.read_playback(speaker_id)
    if playback.transport_state is not TransportState.STOPPED:
        pytest.exit(
            f"The {GARAGE} is {playback.transport_state.value}: the live suite "
            "would interrupt it, and cannot put the elapsed time back. Run it "
            "when the speaker is stopped.",
            returncode=1,
        )
    if (
        not playback.transport_uri.startswith("x-rincon-queue:")
        or playback.queue_length < 2
    ):
        pytest.exit(
            f"The {GARAGE} needs a queue of two tracks or more as its source "
            "for the transport round trip. Queue an album from the Sonos app "
            "and stop it.",
            returncode=1,
        )
    return speaker_id


@pytest.fixture(autouse=True)
def sonos(live_client: SoCoSonosClient) -> SoCoSonosClient:
    """The real client, in place of the suite's fake."""
    return live_client
