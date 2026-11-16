"""Tests for the favourite service, on the in-memory Plex and the real store."""

import logging
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core_exceptions import ExternalServiceException
from app.exceptions import ItemNotFoundException, RecordNotFoundException
from app.extensions import db
from app.models import Favourite, ItemKind
from app.schemas.favourite_schema import FavouriteListQuerySchema
from app.schemas.library_schema import PlaylistListQuerySchema
from app.services.container import ServiceContainer
from app.services.favourite_service import FavouriteService, ServedFavourite
from app.utils.after_commit import (
    clear_after_commit_callbacks,
    run_after_commit_callbacks,
)
from tests.fakes.plex_client import FakePlexClient

# What a change of the favourites sends leaves no event behind for a later
# test's connections.
pytestmark = pytest.mark.usefixtures("sender")

RADIOHEAD = "5001"
BJORK = "5002"
AMNESIAC = "5101"
AMNESIAC_AGAIN = "5102"
HOMOGENIC = "5103"
KID_A = "5104"
TRACK = "5201"
ROAD_TRIP = "5301"
# Where Plex has an item it sees as new.
MOVED = "5901"

AMNESIAC_GUID = "plex://album/5d07amnesiac"
KID_A_GUID = "local://5104"
VENTOLIN_GUID = "plex://album/5d07ventolin"

# "Homogénic" as Plex may give it, and in NFC.
HOMOGENIC_DECOMPOSED = "Homoge\u0301nic"
HOMOGENIC_COMPOSED = "Homog\u00e9nic"

# "Björk" as Plex may give it: the letter, then its accent.
BJORK_DECOMPOSED = "Björk"
BJORK_COMPOSED = "Björk"
SIGUR_ROS_DECOMPOSED = "Sigur Ro\u0301s"
SIGUR_ROS_COMPOSED = "Sigur R\u00f3s"

# Small pictures: the fake's default ones are large (card SCTL-14).
SMALL = (16, 16)

CHANGED = {"type": "favourites_changed", "payload": {}}


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
    plex.add_album(HOMOGENIC, [], size=None, artist=BJORK, title=HOMOGENIC_DECOMPOSED)
    plex.add_playlist(ROAD_TRIP, "Road trip", 148, size=SMALL)
    return plex


@pytest.fixture
def service(container: ServiceContainer) -> Generator[FavouriteService]:
    """The container's favourite service, on the test's session. What its
    calls left to run after a commit is dropped after the test: off a
    request, no teardown drops it."""
    try:
        yield container.favourite_service()
    finally:
        clear_after_commit_callbacks()


def commit(session: Session) -> None:
    """Commit as a request's teardown does: the session, then what waits for
    its commit."""
    session.commit()
    run_after_commit_callbacks()
    clear_after_commit_callbacks()


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


def store(
    session: Session,
    kind: ItemKind,
    rating_key: str,
    title: str,
    guid: str | None = None,
    artist_title: str | None = None,
    created_at: datetime | None = None,
) -> Favourite:
    """A favourite put in the store as a mark would leave it, its guid the
    fake's default for an item of its kind at this rating key."""
    default_guid = {
        ItemKind.ARTIST: f"plex://artist/{rating_key}",
        ItemKind.ALBUM: f"plex://album/{rating_key}",
        ItemKind.PLAYLIST: f"com.plexapp.agents.none://{rating_key}",
    }[kind]
    favourite = Favourite(
        item_kind=kind,
        guid=guid or default_guid,
        rating_key=rating_key,
        title=title,
        artist_title=artist_title,
    )
    if created_at is not None:
        favourite.created_at = created_at
    session.add(favourite)
    session.flush()
    return favourite


def query(**fields: object) -> FavouriteListQuerySchema:
    return FavouriteListQuerySchema.model_validate(fields)


def as_stored(favourite: Favourite, subtitle: str | None = None) -> ServedFavourite:
    """A favourite served as it is stored: not available."""
    return ServedFavourite(
        id=favourite.id,
        kind=favourite.item_kind,
        item_id=None,
        title=favourite.title,
        subtitle=subtitle,
        artwork_id=None,
        available=False,
        created_at=favourite.created_at,
    )


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
        assert (album.title, album.subtitle) == (HOMOGENIC_COMPOSED, BJORK_COMPOSED)
        assert [row[3:] for row in stored(session)] == [
            (BJORK_DECOMPOSED, None),
            (HOMOGENIC_DECOMPOSED, BJORK_DECOMPOSED),
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


class TestListFavourites:
    def test_each_favourite_is_listed_with_its_item_as_plex_has_it_now(
        self, service, library
    ):
        marks = [
            service.mark(ItemKind.ARTIST, RADIOHEAD),
            service.mark(ItemKind.ALBUM, AMNESIAC),
            service.mark(ItemKind.PLAYLIST, ROAD_TRIP),
            service.mark(ItemKind.ALBUM, HOMOGENIC),
        ]

        listed, total = service.list_favourites(query())

        assert listed == [served for served, _ in marks]
        assert total == 4
        album = listed[3]
        assert (album.title, album.subtitle) == (HOMOGENIC_COMPOSED, BJORK_COMPOSED)
        assert all(favourite.available for favourite in listed)

    def test_a_playlist_carries_the_artwork_id_the_list_of_playlists_carries(
        self, service, container, library
    ):
        service.mark(ItemKind.PLAYLIST, ROAD_TRIP)
        library_service = container.library_service()
        library_service.playlist(ROAD_TRIP)

        first, _ = service.list_favourites(query())
        second, _ = service.list_favourites(query())

        (listed,), _ = library_service.list_playlists(PlaylistListQuerySchema())
        assert first[0].artwork_id == second[0].artwork_id == listed.artwork_id

    def test_a_page_asks_plex_once_whatever_its_size(self, service, session, plex):
        for number in range(30):
            rating_key = str(6000 + number)
            plex.add_artist(rating_key, f"Artist {number:02}", size=None)
            store(session, ItemKind.ARTIST, rating_key, f"Artist {number:02}")

        for page, per_page, size in [(1, 100, 30), (2, 7, 7), (4, 8, 6), (1, 1, 1)]:
            plex.requests.clear()

            listed, total = service.list_favourites(query(page=page, per_page=per_page))

            assert (len(listed), total) == (size, 30)
            assert all(favourite.available for favourite in listed)
            assert plex.requests == [f"read {size} Plex items"]

    def test_an_empty_page_asks_plex_nothing(self, service, session, plex):
        assert service.list_favourites(query()) == ([], 0)
        assert plex.requests == []

        plex.add_artist(RADIOHEAD, "Radiohead", size=None)
        store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")

        assert service.list_favourites(query(page=2)) == ([], 1)
        assert plex.requests == []

    def test_a_favourite_plex_has_nowhere_is_served_as_stored_and_kept(
        self, service, session, library, plex
    ):
        favourites = [
            # Another release is at the album's rating key.
            store(
                session,
                ItemKind.ALBUM,
                AMNESIAC,
                "Kid A",
                guid="plex://album/kid-a",
                artist_title="Radiohead",
            ),
            # An album is at the artist's rating key, with the artist's guid.
            store(session, ItemKind.ARTIST, AMNESIAC, "Amnesiac", guid=AMNESIAC_GUID),
            # A track is at the playlist's.
            store(session, ItemKind.PLAYLIST, TRACK, "Long drive"),
            # Nothing is at the album's.
            store(
                session,
                ItemKind.ALBUM,
                "9999",
                "Vespertine",
                artist_title=BJORK_DECOMPOSED,
            ),
        ]
        before = stored(session)
        plex.requests.clear()

        listed, total = service.list_favourites(query())

        assert listed == [
            as_stored(favourites[0], "Radiohead"),
            as_stored(favourites[1]),
            as_stored(favourites[2]),
            as_stored(favourites[3], BJORK_COMPOSED),
        ]
        assert total == 4
        assert stored(session) == before
        # Each is looked for by its guid and by its titles.
        assert plex.requests == [
            "read 3 Plex items",
            "find the Plex albums with guid plex://album/kid-a",
            "find the Plex albums titled 'Kid A'",
            f"find the Plex artists with guid {AMNESIAC_GUID}",
            "find the Plex artists titled 'Amnesiac'",
            f"find the Plex playlists with guid com.plexapp.agents.none://{TRACK}",
            "find the Plex playlists titled 'Long drive'",
            "find the Plex albums with guid plex://album/9999",
            "find the Plex albums titled 'Vespertine'",
        ]

    def test_the_titles_of_a_favourite_not_found_are_served_in_nfc(
        self, service, session, library
    ):
        store(session, ItemKind.ARTIST, "9998", SIGUR_ROS_DECOMPOSED)

        (listed,), _ = service.list_favourites(query())

        assert listed.title == SIGUR_ROS_COMPOSED

    def test_while_plex_fails_the_page_is_served_from_the_store(
        self, service, session, library, caplog
    ):
        artist, _ = service.mark(ItemKind.ARTIST, BJORK)
        album, _ = service.mark(ItemKind.ALBUM, HOMOGENIC)
        playlist, _ = service.mark(ItemKind.PLAYLIST, ROAD_TRIP)
        before = stored(session)
        library.reachable = False

        with caplog.at_level(logging.WARNING, "app.services.favourite_service"):
            listed, total = service.list_favourites(query())

        unavailable = {"item_id": None, "artwork_id": None, "available": False}
        assert [vars(favourite) for favourite in listed] == [
            vars(favourite) | unavailable for favourite in (artist, album, playlist)
        ]
        assert total == 3
        assert stored(session) == before
        (record,) = caplog.records
        assert record.levelno == logging.WARNING
        assert "Cannot read 3 Plex items" in record.getMessage()

    def test_the_favourites_are_ordered_by_when_each_was_marked_then_by_id(
        self, service, session
    ):
        start = datetime(2026, 11, 15, 10, tzinfo=UTC)
        later = [
            store(
                session,
                ItemKind.ARTIST,
                str(6000 + number),
                "Artist",
                created_at=start + timedelta(minutes=minutes),
            ).id
            for number, minutes in enumerate([2, 0, 1, 0])
        ]

        ascending, _ = service.list_favourites(query())
        descending, _ = service.list_favourites(query(sort_dir="desc"))

        oldest_first = [later[1], later[3], later[2], later[0]]
        assert [favourite.id for favourite in ascending] == oldest_first
        assert [favourite.id for favourite in descending] == oldest_first[::-1]

    def test_the_favourites_are_ordered_by_title_without_accents_and_in_one_case(
        self, service, session
    ):
        def mark(kind: ItemKind, title: str) -> int:
            artist_title = "Radiohead" if kind is ItemKind.ALBUM else None
            number = len(stored(session))
            return store(
                session, kind, str(6000 + number), title, artist_title=artist_title
            ).id

        zappa = mark(ItemKind.ARTIST, "Zappa")
        bjorn = mark(ItemKind.ARTIST, "Bjorn")
        bjork = mark(ItemKind.ARTIST, BJORK_DECOMPOSED)
        abba = mark(ItemKind.ARTIST, "abba")
        album = mark(ItemKind.ALBUM, "Amnesiac")
        playlist = mark(ItemKind.PLAYLIST, "Amnesiac")
        echoes = mark(ItemKind.PLAYLIST, "Échos")

        ascending, _ = service.list_favourites(query(sort_by="title"))
        descending, _ = service.list_favourites(query(sort_by="title", sort_dir="desc"))

        in_order = [abba, album, playlist, bjork, bjorn, echoes, zappa]
        assert [favourite.id for favourite in ascending] == in_order
        assert [favourite.id for favourite in descending] == in_order[::-1]


class TestFindingAgain:
    """A favourite its rating key no longer finds (``docs/api-design.md``
    section 5, decision 2)."""

    @pytest.mark.parametrize(
        ("kind", "rating_key", "noun"),
        [
            (ItemKind.ARTIST, RADIOHEAD, "artists"),
            (ItemKind.ALBUM, HOMOGENIC, "albums"),
            (ItemKind.PLAYLIST, ROAD_TRIP, "playlists"),
        ],
    )
    def test_an_item_plex_moved_is_found_by_its_guid_and_brought_up_to_date(
        self, service, session, library, kind, rating_key, noun
    ):
        service.mark(kind, rating_key)
        ((_, guid, _, title, artist_title),) = stored(session)
        library.move(rating_key, MOVED)
        library.requests.clear()

        (listed,), total = service.list_favourites(query())

        assert library.requests == [
            "read 1 Plex items",
            f"find the Plex {noun} with guid {guid}",
        ]
        assert (listed.item_id, listed.available, total) == (MOVED, True, 1)
        assert stored(session) == [(kind, guid, MOVED, title, artist_title)]
        # The next listing finds it at its rating key.
        library.requests.clear()
        assert service.list_favourites(query()) == ([listed], 1)
        assert library.requests == ["read 1 Plex items"]
        # As a mark of the item answers it.
        assert service.mark(kind, MOVED) == (listed, False)

    def test_an_item_plex_moved_and_retitled_has_its_titles_brought_up_to_date(
        self, service, session, library
    ):
        marked, _ = service.mark(ItemKind.ALBUM, HOMOGENIC)
        library.move(BJORK, "5009", title="Bjork")
        library.move(HOMOGENIC, MOVED, title="Homogenic")

        (listed,), _ = service.list_favourites(query())

        assert (listed.id, listed.item_id, listed.title, listed.subtitle) == (
            marked.id,
            MOVED,
            "Homogenic",
            "Bjork",
        )
        assert stored(session) == [
            (ItemKind.ALBUM, f"plex://album/{HOMOGENIC}", MOVED, "Homogenic", "Bjork")
        ]

    def test_of_the_items_with_its_guid_the_first_with_its_titles_is_found(
        self, service, session, library
    ):
        favourite = store(
            session,
            ItemKind.ALBUM,
            "9999",
            "Ventolin",
            guid=VENTOLIN_GUID,
            artist_title="Radiohead",
        )
        for rating_key, title, artist in [
            ("5911", "Ventolin EP", RADIOHEAD),
            ("5912", "Ventolin", BJORK),
            ("5913", "Ventolin", RADIOHEAD),
            ("5914", "Ventolin", RADIOHEAD),
        ]:
            library.add_album(
                rating_key,
                [],
                size=None,
                artist=artist,
                title=title,
                guid=VENTOLIN_GUID,
            )

        (listed,), _ = service.list_favourites(query())

        assert (listed.id, listed.item_id) == (favourite.id, "5913")
        assert stored(session) == [
            (ItemKind.ALBUM, VENTOLIN_GUID, "5913", "Ventolin", "Radiohead")
        ]

    def test_of_the_items_with_its_guid_and_none_with_its_titles_the_first_is_found(
        self, service, session, library
    ):
        store(
            session,
            ItemKind.ALBUM,
            "9999",
            "Ventolin",
            guid=VENTOLIN_GUID,
            artist_title="Radiohead",
        )
        for rating_key, title in [("5911", "Ventolin EP"), ("5912", "Ventolin (Mix)")]:
            library.add_album(
                rating_key,
                [],
                size=None,
                artist=RADIOHEAD,
                title=title,
                guid=VENTOLIN_GUID,
            )

        (listed,), _ = service.list_favourites(query())

        assert (listed.item_id, listed.title) == ("5911", "Ventolin EP")
        assert stored(session) == [
            (ItemKind.ALBUM, VENTOLIN_GUID, "5911", "Ventolin EP", "Radiohead")
        ]

    def test_an_item_plex_has_not_matched_is_found_by_its_titles_alone(
        self, service, session, library
    ):
        library.add_album(
            KID_A, [], size=None, artist=RADIOHEAD, title="Kid A", guid=KID_A_GUID
        )
        service.mark(ItemKind.ALBUM, KID_A)
        library.move(KID_A, MOVED, guid=f"local://{MOVED}")
        library.requests.clear()

        (listed,), _ = service.list_favourites(query())

        # A local:// guid is the rating key again: it is not looked for.
        assert library.requests == [
            "read 1 Plex items",
            "find the Plex albums titled 'Kid A'",
        ]
        assert listed.item_id == MOVED
        assert stored(session) == [
            (ItemKind.ALBUM, f"local://{MOVED}", MOVED, "Kid A", "Radiohead")
        ]

    def test_an_item_plex_matched_anew_is_found_by_its_titles(
        self, service, session, library
    ):
        service.mark(ItemKind.ALBUM, HOMOGENIC)
        library.move(HOMOGENIC, MOVED, guid="plex://album/homogenic")
        library.requests.clear()

        (listed,), _ = service.list_favourites(query())

        assert library.requests == [
            "read 1 Plex items",
            f"find the Plex albums with guid plex://album/{HOMOGENIC}",
            f"find the Plex albums titled {HOMOGENIC_DECOMPOSED!r}",
        ]
        assert listed.item_id == MOVED
        assert stored(session) == [
            (
                ItemKind.ALBUM,
                "plex://album/homogenic",
                MOVED,
                HOMOGENIC_DECOMPOSED,
                BJORK_DECOMPOSED,
            )
        ]

    def test_an_item_is_found_by_its_kind_and_titles_exactly(
        self, service, session, library
    ):
        def gone(kind: ItemKind, title: str, artist_title: str | None) -> Favourite:
            number = len(stored(session))
            return store(
                session,
                kind,
                str(9000 + number),
                title,
                artist_title=artist_title,
            )

        not_found = [
            gone(ItemKind.ARTIST, HOMOGENIC_DECOMPOSED, None),
            gone(ItemKind.ALBUM, HOMOGENIC_DECOMPOSED, "Radiohead"),
            gone(ItemKind.ALBUM, HOMOGENIC_DECOMPOSED.lower(), BJORK_DECOMPOSED),
            gone(ItemKind.ALBUM, HOMOGENIC_COMPOSED, BJORK_DECOMPOSED),
            gone(ItemKind.ALBUM, HOMOGENIC_DECOMPOSED, BJORK_COMPOSED),
        ]
        found = gone(ItemKind.ALBUM, HOMOGENIC_DECOMPOSED, BJORK_DECOMPOSED)

        listed, _ = service.list_favourites(query())

        assert [(f.id, f.item_id) for f in listed] == [
            *[(f.id, None) for f in not_found],
            (found.id, HOMOGENIC),
        ]

    def test_a_favourite_not_found_is_available_again_once_plex_has_its_item(
        self, service, session, library
    ):
        favourite = store(
            session,
            ItemKind.ALBUM,
            "9999",
            "Vespertine",
            guid="plex://album/vespertine",
            artist_title=BJORK_DECOMPOSED,
        )
        (before,), _ = service.list_favourites(query())
        library.add_album(
            MOVED,
            [],
            size=None,
            artist=BJORK,
            title="Vespertine",
            guid="plex://album/vespertine",
        )

        (after,), _ = service.list_favourites(query())

        assert (before.id, before.available) == (favourite.id, False)
        assert (after.id, after.item_id, after.available) == (
            favourite.id,
            MOVED,
            True,
        )

    def test_while_plex_fails_to_find_a_moved_item_nothing_changes(
        self, service, session, library, caplog
    ):
        artist, _ = service.mark(ItemKind.ARTIST, RADIOHEAD)
        album, _ = service.mark(ItemKind.ALBUM, HOMOGENIC)
        library.move(HOMOGENIC, MOVED)
        before = stored(session)
        # The read of the items needs no music library, and the find does.
        library.has_library = False

        with caplog.at_level(logging.WARNING, "app.services.favourite_service"):
            listed, total = service.list_favourites(query())

        unavailable = {"item_id": None, "artwork_id": None, "available": False}
        assert [vars(favourite) for favourite in listed] == [
            vars(favourite) | unavailable for favourite in (artist, album)
        ]
        assert total == 2
        assert stored(session) == before
        (record,) = caplog.records
        assert (
            f"Cannot find the Plex albums with guid plex://album/{HOMOGENIC}"
            in record.getMessage()
        )


class TestOneMarkPerItemAfterARefresh:
    """A favourite found again at an item another favourite marks."""

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_the_one_marked_after_the_other_goes(
        self, service, session, library, sort_dir
    ):
        start = datetime(2026, 11, 15, 10, tzinfo=UTC)
        # Marked after, its id the lower: when it was marked decides.
        store(
            session,
            ItemKind.ALBUM,
            HOMOGENIC,
            HOMOGENIC_DECOMPOSED,
            artist_title=BJORK_DECOMPOSED,
            created_at=start + timedelta(days=1),
        )
        # Marked before Plex matched the item anew.
        first = store(
            session,
            ItemKind.ALBUM,
            "9999",
            HOMOGENIC_DECOMPOSED,
            guid="local://9999",
            artist_title=BJORK_DECOMPOSED,
            created_at=start,
        )

        listed, total = service.list_favourites(query(sort_dir=sort_dir))

        assert [(f.id, f.item_id) for f in listed] == [(first.id, HOMOGENIC)]
        assert total == 1
        assert session.scalars(select(Favourite.id)).all() == [first.id]
        assert stored(session) == [
            (
                ItemKind.ALBUM,
                f"plex://album/{HOMOGENIC}",
                HOMOGENIC,
                HOMOGENIC_DECOMPOSED,
                BJORK_DECOMPOSED,
            )
        ]

    def test_the_one_found_again_goes_when_marked_after_the_other(
        self, service, session, library
    ):
        first, _ = service.mark(ItemKind.ARTIST, RADIOHEAD)
        # Marked in the same transaction, after it: its id decides.
        store(session, ItemKind.ARTIST, "9999", "Radiohead", guid="local://9999")

        listed, total = service.list_favourites(query())

        assert listed == [first]
        assert total == 1
        assert session.scalars(select(Favourite.id)).all() == [first.id]

    def test_the_one_that_goes_on_another_page_leaves_the_count(
        self, service, session, library
    ):
        start = datetime(2026, 11, 15, 10, tzinfo=UTC)
        store(
            session,
            ItemKind.PLAYLIST,
            "9999",
            "Road trip",
            guid="com.plexapp.agents.none://road-trip",
            created_at=start,
        )
        store(session, ItemKind.PLAYLIST, ROAD_TRIP, "Road trip", created_at=start)
        store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead", created_at=start)

        (listed,), total = service.list_favourites(query(per_page=1))

        assert (listed.item_id, total) == (ROAD_TRIP, 2)
        assert [row[2] for row in stored(session)] == [ROAD_TRIP, RADIOHEAD]


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
            service = FavouriteService(
                db_session=second,
                plex_client=committed_plex,
                event_sender=committed_app.container.event_sender(),
            )

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


class TestFavouritesChanged:
    """A change of the store is announced, as ``favourites_changed``, once
    its transaction has committed (``docs/api-design.md`` section 4). A test
    puts what it starts from in the store itself, so that no event of its
    own making waits to be sent when it connects."""

    def test_a_mark_that_made_a_favourite_is_announced_once_committed(
        self, service, session, library, connect
    ):
        stream = connect()

        _, created = service.mark(ItemKind.ARTIST, RADIOHEAD)

        assert created
        assert stream.events() == []
        commit(session)
        assert stream.events() == [CHANGED]

    def test_an_unmark_is_announced_once_committed(
        self, service, session, library, connect
    ):
        favourite = store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        stream = connect()

        service.unmark(favourite.id)

        assert stream.events() == []
        commit(session)
        assert stream.events() == [CHANGED]

    @pytest.mark.parametrize(
        "rating_key", [AMNESIAC, AMNESIAC_AGAIN], ids=["the item", "its other copy"]
    )
    def test_a_mark_answered_with_its_favourite_is_not_announced(
        self, service, session, library, connect, rating_key
    ):
        store(
            session,
            ItemKind.ALBUM,
            AMNESIAC,
            "Amnesiac",
            guid=AMNESIAC_GUID,
            artist_title="Radiohead",
        )
        stream = connect()

        _, created = service.mark(ItemKind.ALBUM, rating_key)
        commit(session)

        assert not created
        assert stream.events() == []

    def test_a_mark_or_an_unmark_that_failed_is_not_announced(
        self, service, session, library, connect
    ):
        stream = connect()

        with pytest.raises(ItemNotFoundException):
            service.mark(ItemKind.ALBUM, TRACK)
        with pytest.raises(RecordNotFoundException):
            service.unmark(2**31 - 1)
        library.reachable = False
        with pytest.raises(ExternalServiceException):
            service.mark(ItemKind.ARTIST, RADIOHEAD)
        commit(session)

        assert stream.events() == []

    def test_a_listing_that_brought_favourites_up_to_date_is_announced_once(
        self, service, session, library, connect
    ):
        store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        store(session, ItemKind.PLAYLIST, ROAD_TRIP, "Road trip")
        library.move(RADIOHEAD, MOVED)
        library.move(ROAD_TRIP, "5902")
        stream = connect()

        service.list_favourites(query())

        assert stream.events() == []
        commit(session)
        assert stream.events() == [CHANGED]
        # The listing made in answer finds both at their rating keys.
        listed, _ = service.list_favourites(query())
        commit(session)
        assert [favourite.item_id for favourite in listed] == [MOVED, "5902"]
        assert stream.events() == []

    def test_a_listing_that_removed_a_second_mark_of_an_item_is_announced(
        self, service, session, library, connect
    ):
        store(session, ItemKind.ARTIST, "9999", "Radiohead", guid="local://9999")
        store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        stream = connect()

        listed, total = service.list_favourites(query())
        commit(session)

        assert (len(listed), total) == (1, 1)
        assert stream.events() == [CHANGED]

    def test_a_listing_that_changed_nothing_is_not_announced(
        self, service, session, library, connect
    ):
        # At its rating key; and found nowhere.
        store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        store(session, ItemKind.ALBUM, "9999", "Vespertine", artist_title="Björk")
        stream = connect()

        listed, _ = service.list_favourites(query())
        commit(session)

        assert [favourite.available for favourite in listed] == [True, False]
        assert stream.events() == []

    def test_a_listing_while_plex_fails_is_not_announced(
        self, service, session, library, connect
    ):
        store(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        library.move(RADIOHEAD, MOVED)
        library.reachable = False
        stream = connect()

        service.list_favourites(query())
        commit(session)

        assert stream.events() == []
