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
read, and so is Plex. A test that changes the Garage's queue takes
``kept_queue``, which puts the queue back as it was found. The Plex token is
never printed: a test asserts on nothing that holds it.

Before the first test it checks that the Garage and Plex can take the suite,
and stops the run with how to fix it where they cannot (TEST-2).
"""

import time
from collections.abc import Generator
from pathlib import Path

import pytest
from dotenv import dotenv_values
from flask import Flask
from pydantic import ValidationError

from app.app_config import AppEnvironment, AppSettings
from app.core_exceptions import ExternalServiceException
from app.exceptions import ConfigurationError
from app.services.health_service import CheckStatus
from app.services.plex_client import PlexapiPlexClient
from app.services.sonos_client import (
    ADD_MANY_LIMIT,
    Playback,
    PlexItem,
    PlexItemKind,
    QueuePage,
    SoCoSonosClient,
    TransportAction,
    TransportState,
)
from app.utils.plex_queue_entry import PlexQueueEntry, parse_plex_queue_entry

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

GARAGE = "Garage"

# The most entries of the Garage's queue the suite puts back after a test
# that changes it: one track at a time, about 0.33 s each
# (docs/api-design.md section 1.3).
QUEUE_LIMIT = 100


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
        # PLEX_LIBRARY is optional: absent from the file, it is unset, as the
        # app reads it, and not the suite's own from the environment.
        return AppSettings.load(
            AppEnvironment.model_validate({"PLEX_LIBRARY": None, **held})
        )
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
    # The test of the client's words for a refused command needs one: a
    # previous at the queue's first track, or a next at its last.
    if {TransportAction.PREVIOUS, TransportAction.NEXT} <= playback.actions:
        pytest.exit(
            f"The {GARAGE} allows both next and previous, and the live suite "
            "needs a command it refuses. Put its queue at the first or the last "
            "track in the Sonos app and stop it.",
            returncode=1,
        )
    # The tests that queue put the queue back as it is listed, which shuffle
    # would draw into another order.
    if playback.shuffle:
        pytest.exit(
            f"The {GARAGE} plays shuffled, and the live suite puts its queue back "
            "in the order it is listed. Turn shuffle off in the Sonos app.",
            returncode=1,
        )
    queue = live_client.read_queue(speaker_id, 0, QUEUE_LIMIT)
    if queue.total > QUEUE_LIMIT or any(
        parse_plex_queue_entry(track.uri) is None for track in queue.tracks
    ):
        pytest.exit(
            f"The live suite puts the {GARAGE}'s queue back one Plex track at a "
            f"time, and can for a queue of {QUEUE_LIMIT} tracks or fewer, all "
            f"from Plex; it holds {queue.total}. Queue an album of the Plex "
            "library from the Sonos app and stop it.",
            returncode=1,
        )
    return speaker_id


@pytest.fixture(scope="session", autouse=True)
def plex_entry(
    live_client: SoCoSonosClient, live_plex: PlexapiPlexClient, garage: str
) -> PlexQueueEntry:
    """What the address of the Plex track the Garage's queue is at names,
    once Plex is fit for the suite."""
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
    entry = parse_plex_queue_entry(track.uri if track else "")
    if entry is None:
        pytest.exit(
            f"The {GARAGE}'s queue is not at a track from Plex, and the live "
            "suite asks Plex for the picture of that track. Queue an album of "
            "the Plex library from the Sonos app and stop it.",
            returncode=1,
        )
    return entry


@pytest.fixture
def kept_queue(live_client: SoCoSonosClient, garage: str) -> Generator[QueuePage]:
    """The Garage's queue, for one test that changes it; then the Garage as
    it was found: its queue, position and play mode, stopped.

    The queue is put back as the Plex service's containers of its tracks,
    from which the speaker makes the entries it held.
    """
    found = live_client.read_queue(garage, 0, QUEUE_LIMIT)
    playback = live_client.read_playback(garage)
    try:
        yield found
    finally:
        _put_back(live_client, garage, found, playback)


def _put_back(
    sonos: SoCoSonosClient, garage: str, found: QueuePage, playback: Playback
) -> None:
    if sonos.read_playback(garage).transport_state is not TransportState.STOPPED:
        sonos.stop(garage)
    # The mode first: what is queued while shuffle is on is drawn into the
    # play order.
    sonos.set_shuffle(garage, playback.shuffle)
    sonos.clear_queue(garage)
    tracks = []
    for track in found.tracks:
        entry = parse_plex_queue_entry(track.uri)
        assert entry is not None
        tracks.append(
            PlexItem(
                PlexItemKind.TRACK,
                entry.machine_identifier,
                entry.section_id,
                entry.rating_key,
                track.title,
            )
        )
    for start in range(0, len(tracks), ADD_MANY_LIMIT):
        sonos.add_many_to_queue(garage, tracks[start : start + ADD_MANY_LIMIT])
    # The queue starts at its first track, and next moves a stopped
    # transport without playing.
    for _ in range(playback.queue_position - 1):
        sonos.next(garage)
    deadline = time.perf_counter() + 15.0
    left = sonos.read_playback(garage)
    while left != playback and time.perf_counter() < deadline:
        time.sleep(0.2)
        left = sonos.read_playback(garage)
    assert left == playback
    assert sonos.read_queue(garage, 0, QUEUE_LIMIT) == found


@pytest.fixture(scope="session")
def plex_track(plex_entry: PlexQueueEntry) -> str:
    """The rating key of the Plex track the Garage's queue is at."""
    return plex_entry.rating_key


@pytest.fixture(autouse=True)
def sonos(live_client: SoCoSonosClient) -> SoCoSonosClient:
    """The real client, in place of the suite's fake."""
    return live_client


@pytest.fixture(autouse=True)
def plex(live_plex: PlexapiPlexClient) -> PlexapiPlexClient:
    """The real client, in place of the suite's fake."""
    return live_plex
