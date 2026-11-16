"""Tests for the queue service, on the in-memory household and Plex.

The service runs on the fake Sonos client, the fake Plex and the real
household mirror.
"""

import logging
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import replace

import pytest
from flask import Flask

from app.core_exceptions import ExternalServiceException
from app.exceptions import SpeakerNotFoundException
from app.schemas.queue_schema import QueueListQuerySchema
from app.services.household_mirror import HouseholdMirror
from app.services.queue_service import QueueEntry, QueueService
from app.utils.artwork_id import plex_artwork_id
from tests.fakes.plex_client import FakePlexClient
from tests.fakes.sonos_client import FakeSonosClient, app_track, queue_track

GARAGE = "RINCON_FAKE0000000101400"
LOUNGE = "RINCON_FAKE0000000201400"
KITCHEN = "RINCON_FAKE0000000301400"
NOWHERE = "RINCON_FAKE0000000991400"

ALBUM = "2001"
# The rating keys of queue_track(1) to queue_track(7).
TRACKS = [str(1000 + number) for number in range(1, 8)]

DID_NOT_ANSWER = "the speaker did not answer (ConnectTimeout)"


@pytest.fixture
def household(
    sonos: FakeSonosClient, plex: FakePlexClient, mirror: HouseholdMirror
) -> FakeSonosClient:
    """The Garage with seven Plex tracks of one album queued, the Lounge
    with two tracks of another service and the Kitchen with nothing,
    mirrored."""
    sonos.add_room(GARAGE, "Garage")
    sonos.add_room(LOUNGE, "Lounge")
    sonos.add_room(KITCHEN, "Kitchen")
    sonos.load_queue(GARAGE, [queue_track(number) for number in range(1, 8)])
    sonos.load_queue(LOUNGE, [app_track(1), app_track(2)])
    plex.add_album(ALBUM, TRACKS)
    mirror.start()
    sonos.settle()
    return sonos


@pytest.fixture
def queue(app: Flask, household: FakeSonosClient) -> QueueService:
    service: QueueService = app.container.queue_service()
    return service


def query(**fields: object) -> QueueListQuerySchema:
    return QueueListQuerySchema.model_validate(fields)


def positions(entries: list[QueueEntry]) -> list[int]:
    return [entry.position for entry in entries]


def calls_from_here(household: FakeSonosClient) -> list[str]:
    """The room of each call the test's thread makes to a speaker from here
    on."""
    thread = threading.current_thread()
    reached: list[str] = []

    def before_call(speaker_id: str) -> None:
        if threading.current_thread() is thread:
            reached.append(speaker_id)

    household.before_call = before_call
    return reached


def picture_requests(
    plex: FakePlexClient, monkeypatch: pytest.MonkeyPatch
) -> list[list[str]]:
    """The rating keys of each call for the pictures of tracks from here on."""
    asked: list[list[str]] = []
    read_track_pictures = plex.read_track_pictures

    def recorded(rating_keys: Iterable[str]) -> dict[str, str]:
        keys = list(rating_keys)
        asked.append(keys)
        return read_track_pictures(keys)

    monkeypatch.setattr(plex, "read_track_pictures", recorded)
    return asked


class TestQueueServiceListQueue:
    def test_the_queue_as_the_speaker_gives_it(self, queue, plex):
        listing = queue.list_queue(GARAGE, query())

        assert (listing.total, listing.page, listing.current_position) == (7, 1, 1)
        assert listing.entries[0] == QueueEntry(
            position=1,
            title="Track 1",
            artist="An Artist",
            album="An Album",
            duration_ms=181000,
            artwork_id=plex_artwork_id(plex.read_track_picture("1001")),
            track_id="1001",
        )
        assert positions(listing.entries) == [1, 2, 3, 4, 5, 6, 7]
        assert [entry.track_id for entry in listing.entries] == TRACKS
        assert [entry.title for entry in listing.entries] == [
            f"Track {number}" for number in range(1, 8)
        ]

    def test_the_tracks_of_an_album_share_its_picture(self, queue):
        listing = queue.list_queue(GARAGE, query())

        assert len({entry.artwork_id for entry in listing.entries}) == 1
        assert listing.entries[0].artwork_id is not None

    def test_the_pictures_of_a_page_are_one_call_to_plex(
        self, queue, plex, monkeypatch
    ):
        asked = picture_requests(plex, monkeypatch)

        queue.list_queue(GARAGE, query(page=2, per_page=3))

        assert asked == [["1004", "1005", "1006"]]

    def test_a_page(self, queue):
        listing = queue.list_queue(GARAGE, query(page=3, per_page=3))

        assert positions(listing.entries) == [7]
        assert (listing.total, listing.page) == (7, 3)

    def test_a_page_past_the_last_is_empty(self, queue):
        listing = queue.list_queue(GARAGE, query(page=4, per_page=3))

        assert listing.entries == []
        assert (listing.total, listing.page) == (7, 4)

    def test_an_empty_queue(self, queue):
        listing = queue.list_queue(KITCHEN, query())

        assert listing.entries == []
        assert (listing.total, listing.page, listing.current_position) == (0, 1, None)

    def test_the_current_position_follows_the_track_that_is_loaded(
        self, queue, household
    ):
        household.next(GARAGE)
        household.next(GARAGE)
        household.settle()

        assert queue.list_queue(GARAGE, query()).current_position == 3

    def test_there_is_no_current_position_while_a_stream_plays(self, queue, household):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        listing = queue.list_queue(GARAGE, query())

        assert listing.current_position is None
        # The queue stays on the speaker, and is listed.
        assert listing.total == 7

    @pytest.mark.parametrize("page", [1, 2, 3])
    def test_locate_answers_the_page_that_holds_the_current_track(
        self, queue, household, page
    ):
        for _ in range(4):
            household.next(GARAGE)
        household.settle()

        listing = queue.list_queue(
            GARAGE, query(page=page, per_page=3, locate="current")
        )

        assert listing.current_position == 5
        assert listing.page == 2
        assert positions(listing.entries) == [4, 5, 6]

    @pytest.mark.parametrize(
        ("current", "page"), [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3)]
    )
    def test_locate_at_each_end_of_a_page(self, queue, household, current, page):
        for _ in range(current - 1):
            household.next(GARAGE)
        household.settle()

        listing = queue.list_queue(GARAGE, query(page=3, per_page=3, locate="current"))

        assert listing.page == page
        assert current in positions(listing.entries)

    def test_locate_without_a_current_track_answers_the_page_asked_for(
        self, queue, household
    ):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        listing = queue.list_queue(GARAGE, query(page=2, per_page=3, locate="current"))

        assert listing.page == 2
        assert positions(listing.entries) == [4, 5, 6]

    def test_a_shuffled_queue_is_listed_in_the_order_it_plays(self, queue, household):
        household.set_shuffle(GARAGE, True)
        household.settle()

        listing = queue.list_queue(GARAGE, query())

        played = household.read_queue(GARAGE, 0, 100).tracks
        assert [entry.title for entry in listing.entries] == [
            track.title for track in played
        ]
        assert [entry.title for entry in listing.entries] != [
            f"Track {number}" for number in range(1, 8)
        ]
        assert listing.current_position == 1

    def test_every_room_of_a_group_answers_the_groups_queue(self, queue, household):
        household.join(KITCHEN, GARAGE)
        household.settle()

        assert queue.list_queue(KITCHEN, query()) == queue.list_queue(GARAGE, query())

    def test_what_changes_elsewhere_is_read_at_once(self, queue, household):
        queue.list_queue(GARAGE, query())

        # Before the speaker's events have arrived.
        household.load_queue(GARAGE, [app_track(5)])

        listing = queue.list_queue(GARAGE, query())
        assert [entry.title for entry in listing.entries] == ["App Track 5"]
        assert listing.total == 1

    def test_an_entry_that_is_not_from_plex_has_neither_id(self, queue):
        listing = queue.list_queue(LOUNGE, query())

        assert listing.entries[0] == QueueEntry(
            position=1,
            title="App Track 1",
            artist="An App Artist",
            album="An App Album",
            duration_ms=201000,
            artwork_id=None,
            track_id=None,
        )

    def test_an_entry_of_another_plex_server_has_neither_id(
        self, queue, household, plex, monkeypatch
    ):
        # The other server's track has a rating key the library has too.
        household.load_queue(
            GARAGE,
            [queue_track(1, machine_identifier="anotherserver"), queue_track(2)],
        )
        asked = picture_requests(plex, monkeypatch)

        entries = queue.list_queue(GARAGE, query()).entries

        assert [(entry.track_id, entry.artwork_id is None) for entry in entries] == [
            (None, True),
            ("1002", False),
        ]
        assert asked == [["1002"]]

    def test_a_page_without_plex_tracks_asks_plex_nothing(self, queue, plex, caplog):
        plex.reachable = False

        with caplog.at_level(logging.WARNING):
            listing = queue.list_queue(LOUNGE, query())

        assert listing.total == 2
        assert caplog.records == []

    def test_a_plex_track_without_a_picture_has_no_artwork(
        self, queue, household, plex
    ):
        plex.add_album("2002", ["1101"], size=None)
        household.load_queue(GARAGE, [queue_track(101), queue_track(102)])

        entries = queue.list_queue(GARAGE, query()).entries

        # 1101 is on an album without a cover, 1102 is a track Plex does not
        # have.
        assert [(entry.track_id, entry.artwork_id) for entry in entries] == [
            ("1101", None),
            ("1102", None),
        ]

    def test_what_an_entry_does_not_give_is_none(self, queue, household):
        household.load_queue(
            GARAGE, [replace(app_track(1), album="", duration_seconds=0)]
        )

        (entry,) = queue.list_queue(GARAGE, query()).entries

        assert (entry.album, entry.duration_ms) == (None, None)

    def test_the_texts_are_nfc(self, queue, household):
        decomposed = {
            "title": unicodedata.normalize("NFD", "Café"),
            "artist": unicodedata.normalize("NFD", "Björk"),
            "album": unicodedata.normalize("NFD", "Début"),
        }
        household.load_queue(GARAGE, [replace(queue_track(1), **decomposed)])

        (entry,) = queue.list_queue(GARAGE, query()).entries

        assert (entry.title, entry.artist, entry.album) == ("Café", "Björk", "Début")
        for text in (entry.title, entry.artist, entry.album):
            assert unicodedata.is_normalized("NFC", text)

    def test_while_plex_has_never_answered_plex_tracks_have_neither_id(
        self, queue, household, plex, caplog
    ):
        household.load_queue(GARAGE, [queue_track(1), app_track(2)])
        plex.reachable = False

        with caplog.at_level(logging.WARNING, "app.services.queue_service"):
            entries = queue.list_queue(GARAGE, query()).entries

        assert [
            (entry.title, entry.track_id, entry.artwork_id) for entry in entries
        ] == [
            ("Track 1", None, None),
            ("App Track 2", None, None),
        ]
        assert [record.levelname for record in caplog.records] == ["WARNING"]
        assert caplog.records[0].getMessage() == (
            f"The queue of speaker {GARAGE} is served without the track ids and "
            "the artwork of its Plex tracks: Cannot read which server Plex is "
            "because external service failed: Plex did not answer (ConnectionError)"
        )

    def test_plex_that_fails_to_give_the_pictures_fails_the_listing(self, queue, plex):
        # Plex answered once: which server it is is known.
        plex.machine_identifier()
        plex.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            queue.list_queue(GARAGE, query())

        assert raised.value.message == (
            "Cannot read the pictures of 7 Plex tracks because external service "
            "failed: Plex did not answer (ConnectionError)"
        )

    def test_a_speaker_that_does_not_answer_fails_the_listing(self, queue, household):
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException) as raised:
            queue.list_queue(GARAGE, query())
        household.answer_again(GARAGE)

        assert raised.value.message == (
            f"Cannot read the queue of speaker {GARAGE} because external service "
            f"failed: {DID_NOT_ANSWER}"
        )

    def test_a_silent_coordinator_fails_the_listing_of_its_group(
        self, queue, household
    ):
        household.join(KITCHEN, GARAGE)
        household.settle()
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException):
            queue.list_queue(KITCHEN, query())
        household.answer_again(GARAGE)

    def test_an_unknown_speaker_is_not_found_without_asking_a_speaker(
        self, queue, household
    ):
        reached = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException) as raised:
            queue.list_queue(NOWHERE, query())

        assert raised.value.error_code == "SPEAKER_NOT_FOUND"
        assert reached == []


class TestQueueServiceClear:
    def test_it_empties_the_queue(self, queue, household, mirror):
        queue.clear(GARAGE)

        assert household.read_queue(GARAGE, 0, 100).total == 0
        household.settle()
        assert mirror.playback(GARAGE).queue_length == 0
        assert queue.list_queue(GARAGE, query()).current_position is None

    def test_a_room_of_a_group_clears_the_groups_queue(self, queue, household):
        household.join(KITCHEN, GARAGE)
        household.settle()
        reached = calls_from_here(household)

        queue.clear(KITCHEN)

        assert reached == [KITCHEN]
        assert household.read_queue(GARAGE, 0, 100).total == 0

    def test_the_other_queues_stay(self, queue, household):
        queue.clear(GARAGE)

        assert household.read_queue(LOUNGE, 0, 100).total == 2

    def test_an_empty_queue_is_cleared(self, queue, household):
        queue.clear(KITCHEN)

        assert household.read_queue(KITCHEN, 0, 100).total == 0

    def test_a_queue_is_cleared_while_a_stream_plays(self, queue, household, mirror):
        household.start_stream(GARAGE, "Radio One")
        household.settle()

        queue.clear(GARAGE)
        household.settle()

        assert household.read_queue(GARAGE, 0, 100).total == 0
        assert mirror.playback(GARAGE).track.artist == "Radio One"

    def test_a_speaker_that_does_not_answer_fails(self, queue, household):
        household.silence_room(GARAGE)

        with pytest.raises(ExternalServiceException) as raised:
            queue.clear(GARAGE)
        household.answer_again(GARAGE)

        assert raised.value.message == (
            f"Cannot clear the queue of speaker {GARAGE} because external service "
            f"failed: {DID_NOT_ANSWER}"
        )

    def test_an_unknown_speaker_is_not_found_without_asking_a_speaker(
        self, queue, household
    ):
        reached = calls_from_here(household)

        with pytest.raises(SpeakerNotFoundException):
            queue.clear(NOWHERE)

        assert reached == []
