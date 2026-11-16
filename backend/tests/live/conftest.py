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

Before the first test it checks that the speakers answer, that the household
has the Garage, and that Plex is fit for the suite, and stops the run with how
to fix it where they are not (TEST-2). Whether the Garage is fit for the tests
that command it is the ``garage`` fixture's check, made when the first of
those tests takes it: where the Garage is in use, or set up so the suite could
not put it back, those tests are skipped, each with the state found, and the
rest run whatever the Garage is doing. A speaker in use is not a system down:
it is somebody's music, which the suite does not interrupt.
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
def garage_speaker(live_client: SoCoSonosClient) -> str:
    """The Garage's speaker id, once a speaker answers and the household has
    the room; whatever the Garage is doing."""
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
    return rooms[0].id


@pytest.fixture(scope="session", autouse=True)
def plex_fit(live_plex: PlexapiPlexClient) -> None:
    """Plex, once it is fit for the suite."""
    result = live_plex.check_readiness()
    if result.status is not CheckStatus.OK:
        pytest.exit(
            f"Plex is not fit for the suite: {result.detail}\n"
            f"Its address, token and library are PLEX_URL, PLEX_TOKEN and "
            f"PLEX_LIBRARY in {_ENV_FILE}. If one of them changed, remove the "
            "three lines and run `kc project setup` from the repo root.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def garage(live_client: SoCoSonosClient, garage_speaker: str) -> str:
    """The Garage's speaker id, for the tests that command the Garage: they
    are skipped, with the state found, where it is not fit for them.

    pytest keeps the skip of a session fixture, so the Garage is read once
    and every such test is skipped for the same reason.
    """
    unfit = _unfit(live_client, garage_speaker)
    if unfit:
        pytest.skip(f"The {GARAGE} is not fit for the tests that command it. {unfit}")
    return garage_speaker


def _unfit(sonos: SoCoSonosClient, speaker_id: str) -> str:
    """What keeps the Garage from the tests that command it, as it stands;
    empty where nothing does."""
    household = sonos.read_household()
    group = next(g for g in household.groups if speaker_id in g.member_ids)
    if group.member_ids != (speaker_id,):
        others = sorted(
            room.name
            for room in household.rooms
            if room.id in group.member_ids and room.id != speaker_id
        )
        return (
            f"It is grouped with {', '.join(others)}, which they would command "
            "with it: ungroup it in the Sonos app."
        )
    playback = sonos.read_playback(speaker_id)
    found = []
    if playback.transport_state is not TransportState.STOPPED:
        found.append(
            f"It is {playback.transport_state.value}, which they would "
            "interrupt, and they cannot put the elapsed time back: stop it."
        )
    if not playback.transport_uri.startswith("x-rincon-queue:"):
        # The title, not the address: the address of a stream on the network
        # is never printed.
        source = playback.transport_title or "a source other than its queue"
        found.append(
            f"It plays {source}, and the transport round trip needs its queue "
            "of two tracks or more as the source."
        )
    elif playback.queue_length < 2:
        found.append(
            f"Its queue holds {playback.queue_length} track(s), and the "
            "transport round trip needs two or more."
        )
    # The test of the client's words for a refused command needs one: a
    # previous at the queue's first track, or a next at its last.
    if {TransportAction.PREVIOUS, TransportAction.NEXT} <= playback.actions:
        found.append(
            f"It is at track {playback.queue_position} of "
            f"{playback.queue_length}, where it refuses neither next nor "
            "previous, and they need a command it refuses: put it at the first "
            "or the last track."
        )
    # The tests that queue put the queue back as it is listed, which shuffle
    # would draw into another order.
    if playback.shuffle:
        found.append(
            "It plays shuffled, and they put its queue back in the order it is "
            "listed: turn shuffle off."
        )
    queue = sonos.read_queue(speaker_id, 0, QUEUE_LIMIT)
    if queue.total > QUEUE_LIMIT:
        found.append(
            f"Its queue holds {queue.total} tracks, and they put it back one "
            f"Plex track at a time, for {QUEUE_LIMIT} tracks or fewer."
        )
    else:
        others = sum(
            parse_plex_queue_entry(track.uri) is None for track in queue.tracks
        )
        if others:
            found.append(
                f"{others} of its {queue.total} queued tracks are not from Plex, "
                "and they put the queue back as Plex tracks."
            )
    if found:
        found.append(
            "Queue an album of the Plex library from the Sonos app, at its first "
            "track, and stop it."
        )
    return " ".join(found)


@pytest.fixture(scope="session")
def plex_entry(live_client: SoCoSonosClient, garage_speaker: str) -> PlexQueueEntry:
    """What the address of a Plex track of the Garage's queue names, read
    whatever the Garage is doing."""
    queue = live_client.read_queue(garage_speaker, 0, QUEUE_LIMIT)
    for track in queue.tracks:
        entry = parse_plex_queue_entry(track.uri)
        if entry is not None:
            return entry
    pytest.skip(
        f"None of the first {len(queue.tracks)} tracks of the {GARAGE}'s queue "
        "is from Plex, and the test reads what a Plex entry carries. Add an "
        "album of the Plex library to its queue from the Sonos app."
    )


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
def plex_track(live_plex: PlexapiPlexClient) -> str:
    """The rating key of a track Plex has, whatever the Garage plays: the
    first of the first album, by artist, that has a cover of its own."""
    for artist in live_plex.read_artists():
        albums = live_plex.read_artist_albums(artist.rating_key, newest_first=False)
        for album in albums or []:
            # An album without a cover carries its artist's picture.
            own = f"/library/metadata/{album.rating_key}/"
            if album.picture is None or not album.picture.startswith(own):
                continue
            tracks = live_plex.read_album_tracks(album.rating_key)
            if tracks:
                return tracks[0].rating_key
    pytest.fail("the music library has no album with a cover and tracks")


@pytest.fixture(autouse=True)
def sonos(live_client: SoCoSonosClient) -> SoCoSonosClient:
    """The real client, in place of the suite's fake."""
    return live_client


@pytest.fixture(autouse=True)
def plex(live_plex: PlexapiPlexClient) -> PlexapiPlexClient:
    """The real client, in place of the suite's fake."""
    return live_plex
