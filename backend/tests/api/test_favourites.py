"""Tests for the favourites endpoints, on the in-memory Plex and the real
store."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Favourite, ItemKind
from app.services.artwork_service import ArtworkService
from tests.api.documents import json_schema, schema_of, undescribed
from tests.conftest import EventStream
from tests.fakes.plex_client import FakePlexClient

# What a change of the favourites sends leaves no event behind for a later
# test's connections.
pytestmark = pytest.mark.usefixtures("sender")

FAVOURITES = "/api/favourites"
FAVOURITE = "/api/favourites/{favourite_id}"

RADIOHEAD = "5001"
BJORK = "5002"
AMNESIAC = "5101"
AMNESIAC_AGAIN = "5102"
HOMOGENIC = "5103"
TRACK = "5201"
ROAD_TRIP = "5301"
EMPTY = "5302"
# Where Plex has an item it sees as new.
MOVED = "5901"

AMNESIAC_GUID = "plex://album/5d07amnesiac"

# "Björk" as Plex may give it: the letter, then its accent.
BJORK_DECOMPOSED = "Björk"
BJORK_COMPOSED = "Björk"

# Small pictures: the fake's default ones are large (card SCTL-14).
SMALL = (16, 16)

# What the list does not take as its query.
REFUSED_QUERIES = [
    "page=0",
    "page=two",
    "per_page=0",
    "per_page=101",
    "sort_dir=up",
    "sort_by=id",
    "sort_by=kind",
    "sort_by=",
]

CHANGED = {"type": "favourites_changed", "payload": {}}

FIELDS = [
    "artwork_id",
    "available",
    "created_at",
    "id",
    "item_id",
    "kind",
    "subtitle",
    "title",
]


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    """Radiohead with Amnesiac held twice, Björk with an album without a
    picture, a playlist and an empty one."""
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
    plex.add_album(HOMOGENIC, [], size=None, artist=BJORK, title="Homogenic")
    plex.add_playlist(ROAD_TRIP, "Road trip", 148, size=SMALL)
    plex.add_playlist(EMPTY, "Empty", 0)
    return plex


def mark(client, kind: str, item_id: str):
    return client.post(FAVOURITES, json={"kind": kind, "item_id": item_id})


def unmark(client, favourite_id: int | str):
    return client.delete(FAVOURITE.format(favourite_id=favourite_id))


def favourite(item_id: str, kind: str, title: str, **fields: object) -> dict:
    """What a favourite answers for an item of the library, its own fields
    aside."""
    return {
        "kind": kind,
        "item_id": item_id,
        "title": title,
        "subtitle": None,
        "available": True,
        **fields,
    }


def own(body: dict) -> dict:
    """The fields of a favourite that are its own, not its item's."""
    return {key: body[key] for key in ("id", "created_at", "artwork_id")}


def without_correlation_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "correlationId"}


def stored(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Favourite))


def listed(client, query: str = "") -> list[dict]:
    response = client.get(f"{FAVOURITES}?{query}")
    assert response.status_code == 200
    return response.get_json()["data"]


def meta(total: int, sort_by: str = "created_at", **paging: object) -> dict:
    per_page = paging.get("per_page", 20)
    return {
        "page": paging.get("page", 1),
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, -(-total // per_page)),
        "sort_by": sort_by,
        "sort_dir": paging.get("sort_dir", "asc"),
    }


def unavailable(body: dict) -> dict:
    """A favourite as it is listed while its item cannot be given."""
    return body | {"item_id": None, "artwork_id": None, "available": False}


def put(
    session: Session,
    kind: ItemKind,
    rating_key: str,
    title: str,
    guid: str | None = None,
    artist_title: str | None = None,
) -> Favourite:
    """A favourite put in the store as a mark would leave it, its guid the
    fake's default for an item of its kind at this rating key, without the
    mark's event."""
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
    session.add(favourite)
    session.flush()
    return favourite


def connect_to(app: Flask) -> EventStream:
    """A connection to the event stream of an app other than the session's,
    as the stream's route opens one; the app's disposal closes it."""
    connection = app.container.sse_connection_manager().on_connect(
        f"test-{uuid.uuid4()}", "local-user", local_admin=True
    )
    return EventStream(connection)


class TestMarkFavourite:
    def test_an_artist_is_marked(self, client, session, library):
        response = mark(client, "artist", RADIOHEAD)

        assert response.status_code == 201
        body = response.get_json()
        assert body == favourite(RADIOHEAD, "artist", "Radiohead") | own(body)
        assert sorted(body) == FIELDS
        assert type(body["id"]) is int
        created_at = datetime.fromisoformat(body["created_at"])
        assert created_at.utcoffset() == timedelta(0)
        artist = client.get(f"/api/library/artists/{RADIOHEAD}").get_json()
        assert body["artwork_id"] == artist["artwork_id"] is not None
        assert stored(session) == 1

    def test_an_album_is_marked_with_its_artist(self, client, library):
        response = mark(client, "album", AMNESIAC)

        assert response.status_code == 201
        body = response.get_json()
        assert body == favourite(
            AMNESIAC, "album", "Amnesiac", subtitle="Radiohead"
        ) | own(body)
        album = client.get(f"/api/library/albums/{AMNESIAC}").get_json()
        assert body["artwork_id"] == album["artwork_id"] is not None

    def test_a_playlist_is_marked_with_the_artwork_id_the_list_carries(
        self, client, library
    ):
        listed = client.get("/api/library/playlists").get_json()["data"]

        response = mark(client, "playlist", ROAD_TRIP)

        assert response.status_code == 201
        body = response.get_json()
        assert body == favourite(ROAD_TRIP, "playlist", "Road trip") | own(body)
        assert client.get("/api/library/playlists").get_json()["data"] == listed
        assert [playlist["artwork_id"] for playlist in listed] == [body["artwork_id"]]

    def test_an_items_artwork_id_is_one_the_artwork_endpoint_serves(
        self, client, library, artwork: ArtworkService
    ):
        artwork_id = mark(client, "album", AMNESIAC).get_json()["artwork_id"]

        for format in ("jpeg", "lvgl"):
            answer = client.get(f"/api/artwork/{artwork_id}?size=16&format={format}")
            assert answer.status_code == 200

    @pytest.mark.parametrize(
        ("kind", "item_id"), [("album", HOMOGENIC), ("playlist", EMPTY)]
    )
    def test_an_item_without_a_picture_has_no_artwork_id(
        self, client, library, kind, item_id
    ):
        response = mark(client, kind, item_id)

        assert response.status_code == 201
        assert response.get_json()["artwork_id"] is None

    def test_the_names_are_nfc(self, client, library):
        artist = mark(client, "artist", BJORK).get_json()
        album = mark(client, "album", HOMOGENIC).get_json()

        assert artist["title"] == BJORK_COMPOSED
        assert album["subtitle"] == BJORK_COMPOSED

    def test_marking_a_marked_item_answers_its_favourite(
        self, client, session, library
    ):
        first = mark(client, "album", AMNESIAC).get_json()

        response = mark(client, "album", AMNESIAC)

        assert response.status_code == 200
        assert response.get_json() == first
        assert stored(session) == 1

    def test_marking_the_other_copy_of_a_release_answers_its_favourite(
        self, client, session, library
    ):
        first = mark(client, "album", AMNESIAC).get_json()

        response = mark(client, "album", AMNESIAC_AGAIN)

        assert response.status_code == 200
        other = client.get(f"/api/library/albums/{AMNESIAC_AGAIN}").get_json()
        assert response.get_json() == first | {
            "item_id": AMNESIAC_AGAIN,
            "artwork_id": other["artwork_id"],
        }
        assert stored(session) == 1

    @pytest.mark.parametrize(
        ("kind", "item_id", "word"),
        [
            ("artist", AMNESIAC, "Artist"),
            ("album", RADIOHEAD, "Album"),
            ("album", TRACK, "Album"),
            ("playlist", AMNESIAC, "Playlist"),
            ("artist", "9999", "Artist"),
            ("album", "abba", "Album"),
            ("playlist", "", "Playlist"),
        ],
    )
    def test_an_item_plex_has_not_of_the_kind_is_not_found(
        self, client, session, library, kind, item_id, word
    ):
        response = mark(client, kind, item_id)

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == {
            "error": f"{word} {item_id} was not found",
            "details": {"message": "The requested resource could not be found"},
            "code": "ITEM_NOT_FOUND",
        }
        assert stored(session) == 0

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(
        self, client, session, library, test_app_settings
    ):
        library.reachable = False

        response = mark(client, "artist", RADIOHEAD)

        assert response.status_code == 502
        assert response.get_json()["code"] == "EXTERNAL_SERVICE_ERROR"
        text = response.get_data(as_text=True)
        assert "192.0.2.1" not in text
        assert test_app_settings.plex_token not in text
        assert stored(session) == 0

    @pytest.mark.parametrize(
        "body",
        [
            None,
            [],
            {},
            {"item_id": AMNESIAC},
            {"kind": "album"},
            {"kind": "track", "item_id": TRACK},
            {"kind": "song", "item_id": AMNESIAC},
            {"kind": "Album", "item_id": AMNESIAC},
            {"kind": "album", "item_id": 5101},
            {"kind": "album", "item_id": None},
            {"kind": "album", "item_id": AMNESIAC, "title": "Amnesiac"},
            {"kind": "album", "item_id": AMNESIAC, "id": 1},
        ],
    )
    def test_a_body_the_route_does_not_take_is_refused(
        self, client, session, library, body
    ):
        response = client.post(FAVOURITES, json=body)

        assert response.status_code == 400
        assert stored(session) == 0


class TestListFavourites:
    def test_the_favourites_are_listed_as_marked(self, client, library):
        marked = [
            mark(client, "artist", RADIOHEAD).get_json(),
            mark(client, "album", HOMOGENIC).get_json(),
            mark(client, "playlist", ROAD_TRIP).get_json(),
        ]

        response = client.get(FAVOURITES)

        assert response.status_code == 200
        assert response.get_json() == {"data": marked, "meta": meta(3)}
        assert marked[1]["subtitle"] == BJORK_COMPOSED

    def test_no_favourites_are_an_empty_page(self, client, library):
        response = client.get(FAVOURITES)

        assert response.status_code == 200
        assert response.get_json() == {"data": [], "meta": meta(0)}

    def test_the_whole_query_spelled_out_is_the_default(self, client, library):
        mark(client, "artist", RADIOHEAD)

        spelled_out = client.get(
            f"{FAVOURITES}?page=1&per_page=20&sort_by=created_at&sort_dir=asc"
        )

        assert spelled_out.status_code == 200
        assert spelled_out.get_json() == client.get(FAVOURITES).get_json()

    @pytest.mark.parametrize("sort_by", ["created_at", "title"])
    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_the_pages_cut_one_sequence(self, client, session, sort_by, sort_dir):
        # Stored in one transaction, they share their created_at.
        ids = []
        for number, title in enumerate(["b", "a", "b", "c", "a"]):
            favourite = Favourite(
                item_kind=ItemKind.ARTIST,
                guid=f"plex://artist/{6000 + number}",
                rating_key=str(6000 + number),
                title=title,
            )
            session.add(favourite)
            session.flush()
            ids.append(favourite.id)
        in_order = {
            "created_at": ids,
            "title": [ids[1], ids[4], ids[0], ids[2], ids[3]],
        }[sort_by]
        order = f"sort_by={sort_by}&sort_dir={sort_dir}"

        whole = listed(client, f"{order}&per_page=100")
        pages = [
            client.get(f"{FAVOURITES}?{order}&per_page=2&page={page}").get_json()
            for page in (1, 2, 3)
        ]

        expected = in_order if sort_dir == "asc" else in_order[::-1]
        assert [favourite["id"] for favourite in whole] == expected
        assert [favourite for page in pages for favourite in page["data"]] == whole
        assert [page["meta"] for page in pages] == [
            meta(5, sort_by, sort_dir=sort_dir, per_page=2, page=page)
            for page in (1, 2, 3)
        ]

    def test_a_playlists_artwork_id_is_the_one_the_list_of_playlists_carries(
        self, client, library
    ):
        mark(client, "playlist", ROAD_TRIP)
        client.get(f"/api/library/playlists/{ROAD_TRIP}")

        first, second = listed(client), listed(client)

        playlists = client.get("/api/library/playlists").get_json()["data"]
        assert first == second
        assert first[0]["artwork_id"] == playlists[0]["artwork_id"] is not None

    def test_an_items_artwork_id_is_one_the_artwork_endpoint_serves(
        self, client, library, artwork: ArtworkService
    ):
        mark(client, "album", AMNESIAC)
        (album,) = listed(client)

        for format in ("jpeg", "lvgl"):
            answer = client.get(
                f"/api/artwork/{album['artwork_id']}?size=16&format={format}"
            )
            assert answer.status_code == 200

    def test_a_favourite_plex_does_not_have_is_listed_unavailable_and_kept(
        self, client, session, library
    ):
        gone = Favourite(
            item_kind=ItemKind.ALBUM,
            guid="plex://album/vespertine",
            rating_key="9999",
            title="Vespertine",
            artist_title=BJORK_DECOMPOSED,
        )
        session.add(gone)
        session.flush()

        (body,) = listed(client)

        assert body == {
            "id": gone.id,
            "kind": "album",
            "item_id": None,
            "title": "Vespertine",
            "subtitle": BJORK_COMPOSED,
            "artwork_id": None,
            "available": False,
            "created_at": body["created_at"],
        }
        assert datetime.fromisoformat(body["created_at"]) == gone.created_at
        assert stored(session) == 1

    def test_a_favourite_whose_item_plex_moved_is_listed_where_it_is_now(
        self, client, session, library
    ):
        marked = mark(client, "artist", RADIOHEAD).get_json()
        library.move(RADIOHEAD, MOVED)

        (body,) = listed(client)

        assert body == marked | {"item_id": MOVED, "artwork_id": body["artwork_id"]}
        assert body["artwork_id"] != marked["artwork_id"]
        answer = client.get(f"/api/artwork/{body['artwork_id']}?size=16&format=jpeg")
        assert answer.status_code == 200
        assert session.scalars(select(Favourite.rating_key)).all() == [MOVED]
        assert mark(client, "artist", MOVED).status_code == 200

    def test_of_two_favourites_found_to_mark_one_item_the_later_is_not_listed(
        self, client, session, library
    ):
        first = Favourite(
            item_kind=ItemKind.ARTIST,
            guid="local://9999",
            rating_key="9999",
            title="Radiohead",
        )
        session.add(first)
        session.flush()
        mark(client, "artist", RADIOHEAD)

        response = client.get(FAVOURITES)

        assert [body["id"] for body in response.get_json()["data"]] == [first.id]
        assert response.get_json()["meta"] == meta(1)
        assert stored(session) == 1

    def test_while_plex_does_not_answer_the_favourites_are_listed_from_the_store(
        self, client, session, library
    ):
        marked = [
            mark(client, "artist", BJORK).get_json(),
            mark(client, "album", HOMOGENIC).get_json(),
            mark(client, "playlist", ROAD_TRIP).get_json(),
        ]
        library.reachable = False

        response = client.get(FAVOURITES)

        assert response.status_code == 200
        assert response.get_json() == {
            "data": [unavailable(body) for body in marked],
            "meta": meta(3),
        }
        assert stored(session) == 3

    @pytest.mark.parametrize("query", REFUSED_QUERIES)
    def test_a_query_the_list_does_not_take_is_refused(self, client, library, query):
        response = client.get(f"{FAVOURITES}?{query}")

        assert response.status_code == 400


class TestUnmarkFavourite:
    def test_a_mark_is_removed(self, client, session, library):
        album = mark(client, "album", AMNESIAC).get_json()
        playlist = mark(client, "playlist", ROAD_TRIP).get_json()

        response = unmark(client, album["id"])

        assert response.status_code == 204
        assert response.data == b""
        assert session.scalars(select(Favourite.id)).all() == [playlist["id"]]
        again = mark(client, "album", AMNESIAC)
        assert again.status_code == 201
        assert again.get_json()["id"] not in (album["id"], playlist["id"])

    def test_it_asks_nothing_of_plex(self, client, session, library):
        artist = mark(client, "artist", RADIOHEAD).get_json()
        library.reachable = False

        response = unmark(client, artist["id"])

        assert response.status_code == 204
        assert stored(session) == 0

    def test_an_id_the_store_does_not_hold_is_not_found(self, client, library):
        artist = mark(client, "artist", RADIOHEAD).get_json()
        assert unmark(client, artist["id"]).status_code == 204

        for favourite_id in (artist["id"], 2**40):
            response = unmark(client, favourite_id)

            assert response.status_code == 404
            assert without_correlation_id(response.get_json()) == {
                "error": f"Favourite {favourite_id} was not found",
                "details": {"message": "The requested resource could not be found"},
                "code": "RECORD_NOT_FOUND",
            }

    @pytest.mark.parametrize("favourite_id", ["abc", "-1", "1.5"])
    def test_an_id_that_is_no_number_is_not_found(self, client, favourite_id):
        assert unmark(client, favourite_id).status_code == 404


class TestMarkingAtOnce:
    def test_two_marks_of_one_item_leave_one_favourite_which_both_answer(
        self,
        committed_app: Flask,
        committed_plex: FakePlexClient,
        monkeypatch: pytest.MonkeyPatch,
        aside: ThreadPoolExecutor,
    ):
        committed_plex.add_album(AMNESIAC, [], size=None, title="Amnesiac")
        together = threading.Barrier(2, timeout=5.0)
        read_items = committed_plex.read_items

        def read_together(rating_keys):
            items = read_items(rating_keys)
            together.wait()
            return items

        monkeypatch.setattr(committed_plex, "read_items", read_together)

        stream = connect_to(committed_app)

        marks = [
            aside.submit(mark, committed_app.test_client(), "album", AMNESIAC)
            for _ in range(2)
        ]
        answers = [marked.result(timeout=10.0) for marked in marks]

        assert sorted(answer.status_code for answer in answers) == [200, 201]
        assert answers[0].get_json() == answers[1].get_json()
        with committed_app.container.session_maker()() as session:
            assert stored(session) == 1
        # Only the mark that made the favourite changed the favourites.
        assert stream.events() == [CHANGED]


class TestFavouritesChanged:
    """``favourites_changed``, sent to every connection for a request that
    changed the favourites, once it has committed (``docs/api-design.md``
    section 4). A test puts what it starts from in the store itself, so that
    no event of its own making waits to be sent when it connects."""

    def test_a_mark_that_made_a_favourite_is_sent_to_every_connection(
        self, client, library, connect
    ):
        first, second = connect(), connect()

        assert mark(client, "playlist", ROAD_TRIP).status_code == 201

        assert first.events() == [CHANGED]
        assert second.events() == [CHANGED]

    def test_an_unmark_is_sent(self, client, session, library, connect):
        marked = put(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        stream = connect()

        assert unmark(client, marked.id).status_code == 204

        assert stream.events() == [CHANGED]

    @pytest.mark.parametrize(
        ("method", "path", "body", "status"),
        [
            pytest.param(
                "post",
                FAVOURITES,
                {"kind": "album", "item_id": AMNESIAC},
                200,
                id="a mark of a marked item",
            ),
            pytest.param(
                "post",
                FAVOURITES,
                {"kind": "album", "item_id": AMNESIAC_AGAIN},
                200,
                id="a mark of the other copy of a release",
            ),
            pytest.param(
                "post",
                FAVOURITES,
                {"kind": "album", "item_id": TRACK},
                404,
                id="a mark of a track",
            ),
            pytest.param(
                "post",
                FAVOURITES,
                {"kind": "album"},
                400,
                id="a mark without an item",
            ),
            pytest.param(
                "delete",
                FAVOURITE.format(favourite_id=2**31 - 1),
                None,
                404,
                id="an unmark of an id the store does not hold",
            ),
            pytest.param(
                "get",
                FAVOURITES,
                None,
                200,
                id="a listing that finds each at its rating key",
            ),
        ],
    )
    def test_a_request_that_changed_nothing_sends_nothing(
        self, client, session, library, connect, method, path, body, status
    ):
        put(
            session,
            ItemKind.ALBUM,
            AMNESIAC,
            "Amnesiac",
            guid=AMNESIAC_GUID,
            artist_title="Radiohead",
        )
        stream = connect()

        assert client.open(path, method=method, json=body).status_code == status

        assert stream.events() == []

    def test_a_mark_while_plex_does_not_answer_sends_nothing(
        self, client, library, connect
    ):
        library.reachable = False
        stream = connect()

        assert mark(client, "artist", RADIOHEAD).status_code == 502

        assert stream.events() == []

    def test_a_listing_that_brought_favourites_up_to_date_is_sent_once(
        self, client, session, library, connect
    ):
        put(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        put(session, ItemKind.PLAYLIST, ROAD_TRIP, "Road trip")
        library.move(RADIOHEAD, MOVED)
        library.move(ROAD_TRIP, "5902")
        stream = connect()

        listed(client)

        assert stream.events() == [CHANGED]
        # The listing made in answer finds both where they are now.
        assert [body["item_id"] for body in listed(client)] == [MOVED, "5902"]
        assert stream.events() == []

    def test_a_listing_that_removed_a_second_mark_of_an_item_is_sent(
        self, client, session, library, connect
    ):
        put(session, ItemKind.ARTIST, "9999", "Radiohead", guid="local://9999")
        put(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        stream = connect()

        listed(client)

        assert stream.events() == [CHANGED]
        assert stored(session) == 1

    def test_a_listing_while_plex_does_not_answer_sends_nothing(
        self, client, session, library, connect
    ):
        put(session, ItemKind.ARTIST, RADIOHEAD, "Radiohead")
        library.move(RADIOHEAD, MOVED)
        library.reachable = False
        stream = connect()

        listed(client)

        assert stream.events() == []

    def test_a_change_is_sent_once_it_is_committed(
        self,
        committed_app: Flask,
        committed_plex: FakePlexClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        committed_plex.add_artist(RADIOHEAD, "Radiohead", size=None)
        manager = committed_app.container.sse_connection_manager()
        open_session = committed_app.container.session_maker()
        send_event = manager.send_event
        found: list[int] = []

        def fetched_on_sending(*args, **kwargs):
            # What a client that fetches on the event finds: the store as
            # another connection to it sees it.
            with open_session() as session:
                found.append(stored(session))
            send_event(*args, **kwargs)

        monkeypatch.setattr(manager, "send_event", fetched_on_sending)
        stream = connect_to(committed_app)
        client = committed_app.test_client()

        marked = mark(client, "artist", RADIOHEAD).get_json()
        unmark(client, marked["id"])

        assert stream.events() == [CHANGED, CHANGED]
        assert found == [1, 0]


class TestFavouritesDocument:
    def test_the_favourites_are_these_routes(self, openapi_document):
        favourites = {
            path: sorted(operations)
            for path, operations in openapi_document["paths"].items()
            if path.startswith(FAVOURITES)
        }

        assert favourites == {FAVOURITES: ["get", "post"], FAVOURITE: ["delete"]}

    def test_listing_says_what_it_serves(self, openapi_document):
        operation = openapi_document["paths"][FAVOURITES]["get"]

        assert operation["summary"] == (
            "The household's favourites, each with its item as Plex has it now."
        )
        description = " ".join(operation["description"].split())
        assert "is kept" in description
        assert "listed where the item is now" in description
        assert "the one marked later is removed" in description
        assert "While Plex fails" in description
        for parameter in operation["parameters"]:
            assert parameter["description"], parameter["name"]

    def test_listing_is_documented_with_its_errors_in_the_envelope(
        self, openapi_document
    ):
        responses = openapi_document["paths"][FAVOURITES]["get"]["responses"]

        assert sorted(responses) == ["200", "400"]
        assert responses["200"]["description"]
        schema = responses["400"]["content"]["application/json"]["schema"]
        assert "ApiErrorResponseSchema" in schema["$ref"]
        assert responses["400"]["description"]

    def test_listing_takes_the_paging_and_its_two_orders(self, openapi_document):
        operation = openapi_document["paths"][FAVOURITES]["get"]
        query = {
            parameter["name"]: schema_of(openapi_document, parameter["schema"])
            for parameter in operation["parameters"]
            if parameter["in"] == "query"
        }

        assert sorted(query) == ["page", "per_page", "sort_by", "sort_dir"]
        assert query["sort_by"]["enum"] == ["created_at", "title"]
        assert query["sort_by"]["default"] == "created_at"
        assert query["sort_dir"]["enum"] == ["asc", "desc"]
        assert query["sort_dir"]["default"] == "asc"
        assert (query["per_page"]["minimum"], query["per_page"]["maximum"]) == (1, 100)
        assert query["page"]["minimum"] == 1
        assert "requestBody" not in operation

    def test_listing_answers_a_page_of_the_favourites_marking_answers(
        self, openapi_document
    ):
        paths = openapi_document["paths"]
        page = json_schema(
            openapi_document, paths[FAVOURITES]["get"]["responses"]["200"]
        )
        created = json_schema(
            openapi_document, paths[FAVOURITES]["post"]["responses"]["201"]
        )

        assert sorted(page["properties"]) == ["data", "meta"]
        assert sorted(page["required"]) == ["data", "meta"]
        assert undescribed(openapi_document, page) == []
        item = schema_of(openapi_document, page["properties"]["data"]["items"])
        assert item == created
        available = " ".join(item["properties"]["available"]["description"].split())
        assert "while Plex fails" in available

    @pytest.mark.parametrize(
        ("path", "method", "statuses", "code"),
        [
            (FAVOURITES, "post", ["200", "201", "400", "404", "502"], "ITEM_NOT_FOUND"),
            (FAVOURITE, "delete", ["204", "404"], "RECORD_NOT_FOUND"),
        ],
    )
    def test_every_error_status_is_documented_with_the_envelope(
        self, openapi_document, path, method, statuses, code
    ):
        responses = openapi_document["paths"][path][method]["responses"]

        assert sorted(responses) == statuses
        for status in responses:
            assert responses[status]["description"]
            if status.startswith(("4", "5")):
                schema = responses[status]["content"]["application/json"]["schema"]
                assert "ApiErrorResponseSchema" in schema["$ref"]
        assert f"`{code}`" in responses["404"]["description"]

    def test_marking_says_what_it_does(self, openapi_document):
        operation = openapi_document["paths"][FAVOURITES]["post"]

        assert operation["summary"] == (
            "Mark an artist, an album or a playlist of the library as a favourite."
        )
        description = " ".join(operation["description"].split())
        assert "marked already is no error" in description
        assert "two copies of a release" in description
        assert "track" in operation["responses"]["400"]["description"]

    @pytest.mark.parametrize(
        ("path", "method"),
        [(FAVOURITES, "get"), (FAVOURITES, "post"), (FAVOURITE, "delete")],
    )
    def test_each_route_says_what_it_announces(self, openapi_document, path, method):
        description = openapi_document["paths"][path][method]["description"]

        assert "announced on the event stream as `favourites_changed`" in " ".join(
            description.split()
        )

    def test_marking_takes_a_described_item(self, openapi_document):
        body = openapi_document["paths"][FAVOURITES]["post"]["requestBody"]
        schema = json_schema(openapi_document, body)

        assert body["required"] is True
        assert sorted(schema["properties"]) == ["item_id", "kind"]
        assert sorted(schema["required"]) == ["item_id", "kind"]
        assert schema["additionalProperties"] is False
        assert undescribed(openapi_document, schema) == []
        kind = schema_of(openapi_document, schema["properties"]["kind"])
        assert kind["enum"] == ["artist", "album", "playlist"]
        assert schema["properties"]["item_id"]["type"] == "string"

    def test_marking_answers_a_described_favourite(self, openapi_document):
        responses = openapi_document["paths"][FAVOURITES]["post"]["responses"]
        created = json_schema(openapi_document, responses["201"])

        assert json_schema(openapi_document, responses["200"]) == created
        assert sorted(created["properties"]) == FIELDS
        assert sorted(created["required"]) == FIELDS
        assert undescribed(openapi_document, created) == []
        fields = created["properties"]
        assert fields["id"]["type"] == "integer"
        assert fields["created_at"]["format"] == "date-time"
        assert schema_of(openapi_document, fields["kind"])["enum"] == [
            "artist",
            "album",
            "playlist",
        ]
        assert "`GET /api/artwork/{artwork_id}`" in fields["artwork_id"]["description"]

    def test_unmarking_names_the_favourite_by_its_id(self, openapi_document):
        operation = openapi_document["paths"][FAVOURITE]["delete"]

        (favourite_id,) = operation["parameters"]
        assert favourite_id["name"] == "favourite_id"
        assert favourite_id["in"] == "path"
        assert favourite_id["schema"]["type"] == "integer"
        assert favourite_id["description"]
        assert "requestBody" not in operation
        assert "content" not in operation["responses"]["204"]
