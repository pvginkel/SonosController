"""Tests for the favourite service, on the in-memory Plex and the real store."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core_exceptions import ExternalServiceException
from app.exceptions import ItemNotFoundException, RecordNotFoundException
from app.extensions import db
from app.models import Favourite, ItemKind
from app.schemas.library_schema import PlaylistListQuerySchema
from app.services.container import ServiceContainer
from app.services.favourite_service import FavouriteService
from tests.fakes.plex_client import FakePlexClient

RADIOHEAD = "5001"
BJORK = "5002"
AMNESIAC = "5101"
AMNESIAC_AGAIN = "5102"
HOMOGENIC = "5103"
TRACK = "5201"
ROAD_TRIP = "5301"

AMNESIAC_GUID = "plex://album/5d07amnesiac"

# "Björk" as Plex may give it: the letter, then its accent.
BJORK_DECOMPOSED = "Björk"
BJORK_COMPOSED = "Björk"

# Small pictures: the fake's default ones are large (card SCTL-14).
SMALL = (16, 16)


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    """Radiohead with Amnesiac held twice, Björk with an album without a
    picture, and a playlist."""
    plex.add_artist(RADIOHEAD, "Radiohead", size=SMALL)
    plex.add_artist(BJORK, BJORK_DECOMPOSED, size=None)
    for album in (AMNESIAC, AMNESIAC_AGAIN):
        plex.add_album(
            album,
            [],
            size=SMALL,
            artist=RADIOHEAD,
            title="Amnesiac",
            guid=AMNESIAC_GUID,
        )
    plex.add_track(AMNESIAC, TRACK)
    plex.add_album(HOMOGENIC, [], size=None, artist=BJORK, title="Homogénic")
    plex.add_playlist(ROAD_TRIP, "Road trip", 148, size=SMALL)
    return plex


@pytest.fixture
def service(container: ServiceContainer) -> FavouriteService:
    return container.favourite_service()


def stored(session: Session) -> list[tuple[object, ...]]:
    """The favourites in the store, oldest first: their kind, guid, rating
    key, title and artist's title."""
    rows = session.execute(
        select(
            Favourite.item_kind,
            Favourite.guid,
            Favourite.rating_key,
            Favourite.title,
            Favourite.artist_title,
        ).order_by(Favourite.id)
    )
    return [tuple(row) for row in rows]


def wait_until_a_query_waits_for_a_lock(engine: Engine) -> None:
    """Wait until a query of the engine's database waits for a lock that
    another transaction holds."""
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        # A transaction sees pg_stat_activity as it was at its first read.
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.01)
    pytest.fail("no query came to wait for a lock")


class TestFavouriteService:
    def test_an_artist_an_album_and_a_playlist_are_marked_and_stored(
        self, service, session, library
    ):
        marks = [
            service.mark(ItemKind.ARTIST, RADIOHEAD),
            service.mark(ItemKind.ALBUM, AMNESIAC),
            service.mark(ItemKind.PLAYLIST, ROAD_TRIP),
        ]

        assert [created for _, created in marks] == [True, True, True]
        assert [
            (f.kind, f.item_id, f.title, f.subtitle, f.available) for f, _ in marks
        ] == [
            (ItemKind.ARTIST, RADIOHEAD, "Radiohead", None, True),
            (ItemKind.ALBUM, AMNESIAC, "Amnesiac", "Radiohead", True),
            (ItemKind.PLAYLIST, ROAD_TRIP, "Road trip", None, True),
        ]
        assert stored(session) == [
            (
                ItemKind.ARTIST,
                f"plex://artist/{RADIOHEAD}",
                RADIOHEAD,
                "Radiohead",
                None,
            ),
            (ItemKind.ALBUM, AMNESIAC_GUID, AMNESIAC, "Amnesiac", "Radiohead"),
            (
                ItemKind.PLAYLIST,
                f"com.plexapp.agents.none://{ROAD_TRIP}",
                ROAD_TRIP,
                "Road trip",
                None,
            ),
        ]
        ids = session.scalars(select(Favourite.id).order_by(Favourite.id)).all()
        assert [f.id for f, _ in marks] == ids
        created_at = session.scalars(select(Favourite.created_at)).first()
        assert {f.created_at for f, _ in marks} == {created_at}

    def test_the_names_are_served_in_nfc_and_stored_as_plex_gives_them(
        self, service, session, library
    ):
        artist, _ = service.mark(ItemKind.ARTIST, BJORK)
        album, _ = service.mark(ItemKind.ALBUM, HOMOGENIC)

        assert artist.title == BJORK_COMPOSED
        assert (album.title, album.subtitle) == ("Homogénic", BJORK_COMPOSED)
        assert [row[3:] for row in stored(session)] == [
            (BJORK_DECOMPOSED, None),
            ("Homogénic", BJORK_DECOMPOSED),
        ]

    def test_the_artwork_id_is_the_one_the_library_carries(
        self, service, container, library
    ):
        artist, _ = service.mark(ItemKind.ARTIST, RADIOHEAD)
        album, _ = service.mark(ItemKind.ALBUM, AMNESIAC)
        playlist, _ = service.mark(ItemKind.PLAYLIST, ROAD_TRIP)
        without, _ = service.mark(ItemKind.ALBUM, HOMOGENIC)

        library_service = container.library_service()
        assert artist.artwork_id == library_service.artist(RADIOHEAD).artwork_id
        assert album.artwork_id == library_service.album(AMNESIAC).artwork_id
        (listed,), _ = library_service.list_playlists(PlaylistListQuerySchema())
        assert playlist.artwork_id == listed.artwork_id
        assert None not in (artist.artwork_id, album.artwork_id, playlist.artwork_id)
        assert without.artwork_id is None

    def test_marking_a_marked_item_answers_its_favourite(
        self, service, session, library
    ):
        first, _ = service.mark(ItemKind.ALBUM, AMNESIAC)

        again, created = service.mark(ItemKind.ALBUM, AMNESIAC)

        assert not created
        assert again == first
        assert len(stored(session)) == 1

    def test_marking_the_other_copy_of_a_release_answers_its_favourite(
        self, service, session, library
    ):
        first, _ = service.mark(ItemKind.ALBUM, AMNESIAC)

        other, created = service.mark(ItemKind.ALBUM, AMNESIAC_AGAIN)

        assert not created
        assert (other.id, other.item_id) == (first.id, AMNESIAC_AGAIN)
        assert stored(session) == [
            (ItemKind.ALBUM, AMNESIAC_GUID, AMNESIAC, "Amnesiac", "Radiohead")
        ]

    @pytest.mark.parametrize(
        ("kind", "item_id", "word"),
        [
            (ItemKind.ARTIST, AMNESIAC, "Artist"),
            (ItemKind.ALBUM, RADIOHEAD, "Album"),
            (ItemKind.ALBUM, TRACK, "Album"),
            (ItemKind.PLAYLIST, AMNESIAC, "Playlist"),
            (ItemKind.ARTIST, "9999", "Artist"),
            (ItemKind.ALBUM, "abba", "Album"),
        ],
    )
    def test_an_item_plex_has_not_of_the_kind_is_not_found(
        self, service, session, library, kind, item_id, word
    ):
        with pytest.raises(ItemNotFoundException) as raised:
            service.mark(kind, item_id)

        assert raised.value.message == f"{word} {item_id} was not found"
        assert raised.value.error_code == "ITEM_NOT_FOUND"
        assert stored(session) == []

    def test_a_plex_that_fails_stores_nothing(self, service, session, library):
        library.reachable = False

        with pytest.raises(ExternalServiceException):
            service.mark(ItemKind.ARTIST, RADIOHEAD)

        assert stored(session) == []

    def test_a_favourite_is_unmarked(self, service, session, library):
        album, _ = service.mark(ItemKind.ALBUM, AMNESIAC)
        playlist, _ = service.mark(ItemKind.PLAYLIST, ROAD_TRIP)

        service.unmark(album.id)

        assert session.scalars(select(Favourite.id)).all() == [playlist.id]
        again, created = service.mark(ItemKind.ALBUM, AMNESIAC_AGAIN)
        assert created
        assert again.id not in (album.id, playlist.id)

    def test_unmarking_asks_nothing_of_plex(self, service, session, library):
        artist, _ = service.mark(ItemKind.ARTIST, RADIOHEAD)
        library.reachable = False

        service.unmark(artist.id)

        assert stored(session) == []

    def test_an_id_the_store_does_not_hold_is_not_found(
        self, service, session, library
    ):
        artist, _ = service.mark(ItemKind.ARTIST, RADIOHEAD)
        service.unmark(artist.id)

        for favourite_id in (artist.id, 2**40):
            with pytest.raises(RecordNotFoundException) as raised:
                service.unmark(favourite_id)

            assert raised.value.message == f"Favourite {favourite_id} was not found"
            assert raised.value.error_code == "RECORD_NOT_FOUND"


class TestMarkingAtOnce:
    def test_a_mark_that_meets_one_not_yet_committed_answers_it_once_committed(
        self,
        committed_app: Flask,
        committed_plex: FakePlexClient,
        aside: ThreadPoolExecutor,
    ):
        committed_plex.add_artist(RADIOHEAD, "Radiohead", size=None)
        with committed_app.app_context():
            engine = db.engine
        open_session = committed_app.container.session_maker()
        first, second = open_session(), open_session()
        try:
            # Another mark of the item, its favourite stored and not yet
            # committed.
            first.add(
                Favourite(
                    item_kind=ItemKind.ARTIST,
                    guid=f"plex://artist/{RADIOHEAD}",
                    rating_key=RADIOHEAD,
                    title="Radiohead",
                )
            )
            first.flush()
            service = FavouriteService(db_session=second, plex_client=committed_plex)

            marking = aside.submit(service.mark, ItemKind.ARTIST, RADIOHEAD)
            wait_until_a_query_waits_for_a_lock(engine)
            first.commit()
            served, created = marking.result(timeout=5.0)
            second.commit()

            assert not created
            ids = first.scalars(select(Favourite.id)).all()
            assert ids == [served.id]
        finally:
            first.close()
            second.close()
