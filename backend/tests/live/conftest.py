"""The live suite: the real Sonos and Plex clients against the real speakers
and the real Plex.

Marked ``live`` and deselected by default; CI never runs it. Run it from a
SonosController environment, from ``backend/``, with the dev stack stopped
(both listen for the speakers' events on one port)::

    cexec python uv run pytest -m live tests/live

It runs on the settings ``kc project setup`` writes to the git-ignored
``.env``; no address and no token is in this repository. The standing rules
(``docs/change-discipline.md``): the Garage speaker only, at low volume, its
queue, position, volume and grouping left as found. Every other room is only
read, and so is Plex. The Plex token is never printed: a test asserts on
nothing that holds it.

Before the first test it checks that the Garage and Plex can take the suite,
and stops the run with how to fix it where they cannot (TEST-2).
"""

from pathlib import Path
from urllib.parse import unquote

import pytest
from dotenv import dotenv_values
from flask import Flask
from pydantic import ValidationError

from app.app_config import AppEnvironment, AppSettings
from app.core_exceptions import ExternalServiceException
from app.exceptions import ConfigurationError
from app.services.health_service import CheckStatus
from app.services.plex_client import PlexapiPlexClient
from app.services.sonos_client import SoCoSonosClient, TransportState

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

GARAGE = "Garage"


@pytest.fixture(scope="session")
def live_settings() -> AppSettings:
    """The settings ``.env`` holds: the real household and the real Plex."""
    held = dotenv_values(_ENV_FILE)
    # tests/conftest.py points the environment's SONOS_* and PLEX_* at
    # nothing, and the environment gives what the file lacks.
    lacking = sorted(
        name
        for name, field in AppEnvironment.model_fields.items()
        if field.is_required() and not held.get(name)
    )
    if lacking:
        pytest.exit(
            f"The live suite reads the household and Plex from {_ENV_FILE}, "
            f"and that file lacks {', '.join(lacking)}.\n"
            "Run `kc project setup` from the repo root.",
            returncode=1,
        )
    try:
        return AppSettings.load(AppEnvironment.model_validate(held))
    except (ValidationError, ConfigurationError) as exc:
        pytest.exit(
            f"The live suite reads the household and Plex from {_ENV_FILE}, "
            f"and that file does not give them:\n{exc}\n"
            "Run `kc project setup` from the repo root.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def live_client(app: Flask, live_settings: AppSettings) -> SoCoSonosClient:
    """The SoCo client on the household ``.env`` names."""
    return SoCoSonosClient(
        settings=live_settings, health_service=app.container.health_service()
    )


@pytest.fixture(scope="session")
def live_plex(app: Flask, live_settings: AppSettings) -> PlexapiPlexClient:
    """The plexapi client on the Plex ``.env`` names."""
    return PlexapiPlexClient(
        settings=live_settings, health_service=app.container.health_service()
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


@pytest.fixture(scope="session", autouse=True)
def plex_track(
    live_client: SoCoSonosClient, live_plex: PlexapiPlexClient, garage: str
) -> str:
    """The rating key of the Plex track the Garage's queue is at, once Plex
    is fit for the suite."""
    result = live_plex.check_readiness()
    if result.status is not CheckStatus.OK:
        pytest.exit(
            f"Plex is not fit for the suite: {result.detail}\n"
            f"Its address, token and library are PLEX_URL, PLEX_TOKEN and "
            f"PLEX_LIBRARY in {_ENV_FILE}. If one of them changed, remove the "
            "three lines and run `kc project setup` from the repo root.",
            returncode=1,
        )
    track = live_client.read_playback(garage).track
    # A queue entry from the Plex service names its track:
    # x-sonos-http:<machine>%3a<section>%3a<rating key>%3atrack.mp3?…
    # (docs/api-design.md section 1.3).
    entry = unquote(track.uri if track else "").split("?")[0].split(":")
    if (
        len(entry) != 5
        or entry[0] != "x-sonos-http"
        or not entry[3].isdigit()
        or not entry[4].startswith("track.")
    ):
        pytest.exit(
            f"The {GARAGE}'s queue is not at a track from Plex, and the live "
            "suite asks Plex for the picture of that track. Queue an album of "
            "the Plex library from the Sonos app and stop it.",
            returncode=1,
        )
    return entry[3]


@pytest.fixture(autouse=True)
def sonos(live_client: SoCoSonosClient) -> SoCoSonosClient:
    """The real client, in place of the suite's fake."""
    return live_client


@pytest.fixture(autouse=True)
def plex(live_plex: PlexapiPlexClient) -> PlexapiPlexClient:
    """The real client, in place of the suite's fake."""
    return live_plex
