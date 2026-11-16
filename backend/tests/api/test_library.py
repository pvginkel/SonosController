"""Tests for the library endpoints, on the in-memory Plex."""

import io
import struct
from datetime import date

import pytest
from PIL import Image

from app.services.artwork_service import ArtworkService
from tests.api.documents import json_schema, schema_of, undescribed
from tests.fakes.plex_client import FakePlexClient

BEATLES = "3001"
ABBA = "3002"
BJORK = "3003"

REVOLVER = "3101"
HELP = "3102"
DEMOS = "3103"

REVOLVER_TRACKS = ("3201", "3202")
HELP_TRACK = "3203"
DEMOS_TRACK = "3204"

ROAD_TRIP = "4001"
EMPTY = "4002"
EVERYTHING = "4003"

# "Björk" as Plex may give it: the letter, then its accent.
BJORK_DECOMPOSED = "Björk"
BJORK_COMPOSED = "Björk"

BLUE = (20, 40, 200)
RED = (200, 60, 30)
PURPLE = (90, 30, 160)

# What is no item: a key Plex does not have, and what is no key.
NO_KEYS = ["9999", "abba"]

# What no list takes as its query.
REFUSED_QUERIES = [
    "page=0",
    "page=-1",
    "page=two",
    "page=",
    "per_page=0",
    "per_page=101",
    "per_page=many",
    "sort_dir=up",
    "sort_dir=ASC",
    "sort_by=id",
    "sort_by=",
]

LISTS = [
    "/api/library/artists",
    f"/api/library/artists/{BEATLES}/albums",
    f"/api/library/albums/{REVOLVER}/tracks",
    "/api/library/playlists",
]

ARTISTS = "/api/library/artists"
ARTIST = "/api/library/artists/{artist_id}"
ALBUMS = "/api/library/artists/{artist_id}/albums"
ALBUM = "/api/library/albums/{album_id}"
TRACKS = "/api/library/albums/{album_id}/tracks"
PLAYLISTS = "/api/library/playlists"
PLAYLIST = "/api/library/playlists/{playlist_id}"

LIST_PATHS = [ARTISTS, ALBUMS, TRACKS, PLAYLISTS]
ITEM_PATHS = [ARTIST, ALBUM, PLAYLIST]

FAVOURITES = "/api/favourites"
FAVOURITE = "/api/favourites/{favourite_id}"

# The favourite toggle's library.
RADIOHEAD = "5001"
AMNESIAC = "5101"
AMNESIAC_AGAIN = "5102"
KID_A = "5103"
RADIOHEADS_TRACK = "5201"
MIX = "5301"
DINNER = "5302"

AMNESIAC_GUID = "plex://album/5d07amnesiac"

RADIOHEADS_ALBUMS = (
    f"/api/library/artists/{RADIOHEAD}/albums?sort_by=title&sort_dir=asc"
)

# Small pictures: the fake's default ones are large (card SCTL-14).
SMALL = (16, 16)


@pytest.fixture
def library(plex: FakePlexClient, artwork: ArtworkService) -> FakePlexClient:
    """Three artists, the Beatles with three albums, and three playlists of
    which one can be played."""
    plex.add_artist(BEATLES, "The Beatles", sort_title="Beatles", colour=BLUE)
    plex.add_artist(ABBA, "ABBA", size=None)
    plex.add_artist(BJORK, BJORK_DECOMPOSED)
    plex.add_album(
        REVOLVER,
        REVOLVER_TRACKS,
        colour=RED,
        artist=BEATLES,
        title="Revolver",
        released=date(1966, 8, 5),
    )
    plex.add_album(
        HELP, [HELP_TRACK], artist=BEATLES, title="Help!", released=date(1965, 8, 6)
    )
    plex.add_album(DEMOS, [], artist=BEATLES, title="Demos", size=None)
    plex.add_track(
        DEMOS,
        DEMOS_TRACK,
        title="Demo",
        artist="Tony Sheridan",
        disc=None,
        number=None,
        duration_ms=None,
    )
    plex.add_playlist(ROAD_TRIP, "Road trip", 148, colour=PURPLE)
    plex.add_playlist(EMPTY, "Empty", 0)
    plex.add_playlist(EVERYTHING, "All music", 12_019)
    return plex


@pytest.fixture
def radiohead(plex: FakePlexClient) -> FakePlexClient:
    """Björk, and Radiohead with Amnesiac held twice and Kid A; two
    playlists. Small pictures or none."""
    plex.add_artist(BJORK, BJORK_DECOMPOSED, size=None)
    plex.add_artist(RADIOHEAD, "Radiohead", size=SMALL)
    for album in (AMNESIAC, AMNESIAC_AGAIN):
        plex.add_album(
            album,
            [],
            size=SMALL,
            artist=RADIOHEAD,
            title="Amnesiac",
            guid=AMNESIAC_GUID,
        )
    plex.add_track(AMNESIAC, RADIOHEADS_TRACK)
    plex.add_album(KID_A, [], size=None, artist=RADIOHEAD, title="Kid A")
    plex.add_playlist(MIX, "Mix", 148, size=SMALL)
    plex.add_playlist(DINNER, "Dinner", 12, size=SMALL)
    return plex


def mark(client, kind: str, item_id: str):
    return client.post(FAVOURITES, json={"kind": kind, "item_id": item_id})


def favourite_ids(client, path: str) -> dict[str, int | None]:
    """The ``favourite_id`` of every item of a list, by the item's id."""
    response = client.get(path)
    assert response.status_code == 200
    return {item["id"]: item["favourite_id"] for item in response.get_json()["data"]}


def favourite_id(client, path: str) -> int | None:
    """The ``favourite_id`` one item carries."""
    response = client.get(path)
    assert response.status_code == 200
    return response.get_json()["favourite_id"]


def meta(total: int, sort_by: str, sort_dir: str = "asc", **paging: int) -> dict:
    per_page = paging.get("per_page", 20)
    return {
        "page": paging.get("page", 1),
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, -(-total // per_page)),
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }


def not_found(kind: str, item_id: str) -> dict:
    return {
        "error": f"{kind} {item_id} was not found",
        "details": {"message": "The requested resource could not be found"},
        "code": "ITEM_NOT_FOUND",
    }


def plex_failed(operation: str) -> dict:
    return {
        "error": (
            f"Cannot {operation} because external service failed: Plex did "
            "not answer (ConnectionError)"
        ),
        "details": {"message": "External service request failed"},
        "code": "EXTERNAL_SERVICE_ERROR",
    }


def without_correlation_id(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "correlationId"}


def ids(response) -> list[str]:
    assert response.status_code == 200
    return [item["id"] for item in response.get_json()["data"]]


def colour_of(client, artwork_id: str) -> tuple[int, int, int]:
    """The colour at the centre of a picture, as the artwork endpoint serves
    it as a JPEG; the endpoint serves it for LVGL too."""
    jpeg = client.get(f"/api/artwork/{artwork_id}?size=80&format=jpeg")
    assert jpeg.status_code == 200
    assert jpeg.mimetype == "image/jpeg"
    lvgl = client.get(f"/api/artwork/{artwork_id}?size=80&format=lvgl")
    assert lvgl.status_code == 200
    magic, colour_format, _, width, height = struct.unpack_from("<BBHHH", lvgl.data)
    assert (magic, colour_format, width, height) == (0x19, 0x12, 80, 80)
    assert len(lvgl.data) == 12 + 2 * 80 * 80
    return Image.open(io.BytesIO(jpeg.data)).convert("RGB").getpixel((40, 40))


def near(colour: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 4 for a, b in zip(colour, expected, strict=True))


class TestListArtists:
    def test_the_artists_by_sort_title_with_their_letters(self, client, library):
        response = client.get("/api/library/artists")

        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"] == meta(3, "sort_title")
        abba, beatles, bjork = body["data"]
        assert abba == {
            "id": ABBA,
            "title": "ABBA",
            "index_letter": "A",
            "artwork_id": None,
            "favourite_id": None,
        }
        assert beatles == {
            "id": BEATLES,
            "title": "The Beatles",
            "index_letter": "B",
            "artwork_id": beatles["artwork_id"],
            "favourite_id": None,
        }
        assert bjork == {
            "id": BJORK,
            "title": BJORK_COMPOSED,
            "index_letter": "B",
            "artwork_id": bjork["artwork_id"],
            "favourite_id": None,
        }
        assert beatles["artwork_id"] != bjork["artwork_id"]

    def test_an_artists_artwork_id_is_one_the_artwork_endpoint_serves(
        self, client, library
    ):
        beatles = client.get(f"/api/library/artists/{BEATLES}").get_json()

        assert near(colour_of(client, beatles["artwork_id"]), BLUE)

    def test_a_page_and_a_direction(self, client, library):
        response = client.get("/api/library/artists?per_page=2&page=2&sort_dir=desc")

        assert ids(response) == [ABBA]
        assert response.get_json()["meta"] == meta(
            3, "sort_title", "desc", per_page=2, page=2
        )

    def test_a_page_of_100(self, client, plex):
        for key in range(1, 121):
            plex.add_artist(str(key), f"Artist {key:03}", size=None)

        response = client.get("/api/library/artists?per_page=100&page=2")

        assert ids(response) == [str(key) for key in range(101, 121)]
        assert response.get_json()["meta"] == meta(
            120, "sort_title", per_page=100, page=2
        )

    def test_a_page_past_the_last_is_empty(self, client, library):
        response = client.get("/api/library/artists?page=5")

        assert response.get_json() == {
            "data": [],
            "meta": meta(3, "sort_title", page=5),
        }

    def test_an_empty_library_is_one_empty_page(self, client):
        response = client.get("/api/library/artists")

        assert response.get_json() == {"data": [], "meta": meta(0, "sort_title")}

    def test_it_does_not_search(self, client, library):
        response = client.get("/api/library/artists?search=abba&q=abba&title=abba")

        assert ids(response) == [ABBA, BEATLES, BJORK]

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get("/api/library/artists")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            "read the artists of Plex's music library"
        )

    def test_a_plex_without_the_music_library_is_a_bad_gateway(self, client, library):
        library.has_library = False

        response = client.get("/api/library/artists")

        assert response.status_code == 502
        assert response.get_json()["code"] == "EXTERNAL_SERVICE_ERROR"


class TestGetArtist:
    def test_one_artist(self, client, library):
        response = client.get(f"/api/library/artists/{BJORK}")

        assert response.status_code == 200
        body = response.get_json()
        assert body == {
            "id": BJORK,
            "title": BJORK_COMPOSED,
            "index_letter": "B",
            "artwork_id": body["artwork_id"],
            "favourite_id": None,
        }
        (listed,) = (
            artist
            for artist in client.get("/api/library/artists").get_json()["data"]
            if artist["id"] == BJORK
        )
        assert body == listed

    @pytest.mark.parametrize("artist_id", [*NO_KEYS, REVOLVER, ROAD_TRIP])
    def test_an_id_that_is_no_artists_is_not_found(self, client, library, artist_id):
        response = client.get(f"/api/library/artists/{artist_id}")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(
            "Artist", artist_id
        )

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get(f"/api/library/artists/{BEATLES}")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            f"read Plex artist {BEATLES}"
        )


class TestListArtistAlbums:
    def test_the_albums_newest_first(self, client, library):
        response = client.get(f"/api/library/artists/{BEATLES}/albums")

        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"] == meta(3, "release", "desc")
        revolver, help_, demos = body["data"]
        assert revolver == {
            "id": REVOLVER,
            "title": "Revolver",
            "artist_id": BEATLES,
            "artist": "The Beatles",
            "year": 1966,
            "track_count": 2,
            "artwork_id": revolver["artwork_id"],
            "favourite_id": None,
        }
        assert (help_["id"], help_["year"], help_["track_count"]) == (HELP, 1965, 1)
        # An album without a cover has its artist's picture.
        beatles = client.get(f"/api/library/artists/{BEATLES}").get_json()
        assert demos == {
            "id": DEMOS,
            "title": "Demos",
            "artist_id": BEATLES,
            "artist": "The Beatles",
            "year": None,
            "track_count": 1,
            "artwork_id": beatles["artwork_id"],
            "favourite_id": None,
        }

    def test_an_albums_artwork_id_is_one_the_artwork_endpoint_serves(
        self, client, library
    ):
        revolver = client.get(f"/api/library/albums/{REVOLVER}").get_json()

        assert near(colour_of(client, revolver["artwork_id"]), RED)

    def test_the_oldest_first_on_request(self, client, library):
        response = client.get(f"/api/library/artists/{BEATLES}/albums?sort_dir=asc")

        assert ids(response) == [HELP, REVOLVER, DEMOS]
        assert response.get_json()["meta"] == meta(3, "release", "asc")

    def test_by_title(self, client, library):
        albums = f"/api/library/artists/{BEATLES}/albums"

        ascending = client.get(f"{albums}?sort_by=title&sort_dir=asc")
        descending = client.get(f"{albums}?sort_by=title")

        assert ids(ascending) == [DEMOS, HELP, REVOLVER]
        assert ascending.get_json()["meta"] == meta(3, "title", "asc")
        assert ids(descending) == [REVOLVER, HELP, DEMOS]
        assert descending.get_json()["meta"] == meta(3, "title", "desc")

    def test_a_page(self, client, library):
        response = client.get(
            f"/api/library/artists/{BEATLES}/albums?per_page=2&page=2"
        )

        assert ids(response) == [DEMOS]
        assert response.get_json()["meta"] == meta(
            3, "release", "desc", per_page=2, page=2
        )

    def test_the_artists_name_is_nfc(self, client, library):
        library.add_album("3110", [], artist=BJORK, title="Homogénic")

        (album,) = client.get(f"/api/library/artists/{BJORK}/albums").get_json()["data"]

        assert (album["title"], album["artist"]) == ("Homogénic", BJORK_COMPOSED)

    def test_an_artist_without_albums_is_one_empty_page(self, client, library):
        response = client.get(f"/api/library/artists/{ABBA}/albums")

        assert response.status_code == 200
        assert response.get_json() == {"data": [], "meta": meta(0, "release", "desc")}

    @pytest.mark.parametrize("artist_id", [*NO_KEYS, REVOLVER])
    def test_an_id_that_is_no_artists_is_not_found(self, client, library, artist_id):
        response = client.get(f"/api/library/artists/{artist_id}/albums")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(
            "Artist", artist_id
        )

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get(f"/api/library/artists/{BEATLES}/albums")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            f"read the albums of Plex artist {BEATLES}"
        )


class TestGetAlbum:
    def test_one_album(self, client, library):
        response = client.get(f"/api/library/albums/{REVOLVER}")

        assert response.status_code == 200
        (listed, *_) = client.get(f"/api/library/artists/{BEATLES}/albums").get_json()[
            "data"
        ]
        assert response.get_json() == listed
        assert listed["id"] == REVOLVER

    @pytest.mark.parametrize("album_id", [*NO_KEYS, BEATLES, HELP_TRACK])
    def test_an_id_that_is_no_albums_is_not_found(self, client, library, album_id):
        response = client.get(f"/api/library/albums/{album_id}")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(
            "Album", album_id
        )

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get(f"/api/library/albums/{REVOLVER}")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            f"read Plex album {REVOLVER}"
        )


class TestListAlbumTracks:
    def test_the_tracks_in_the_albums_order(self, client, library):
        response = client.get(f"/api/library/albums/{REVOLVER}/tracks")

        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"] == meta(2, "position")
        first, second = body["data"]
        assert first == {
            "id": REVOLVER_TRACKS[0],
            "title": f"Track {REVOLVER_TRACKS[0]}",
            "artist": "The Beatles",
            "disc": 1,
            "number": 1,
            "duration_ms": 180_000,
            "artwork_id": first["artwork_id"],
        }
        assert (second["id"], second["number"]) == (REVOLVER_TRACKS[1], 2)

    def test_the_tracks_share_the_albums_artwork_id(self, client, library):
        album = client.get(f"/api/library/albums/{REVOLVER}").get_json()

        tracks = client.get(f"/api/library/albums/{REVOLVER}/tracks").get_json()

        assert {track["artwork_id"] for track in tracks["data"]} == {
            album["artwork_id"]
        }

    def test_a_track_with_its_own_artist_on_an_album_without_a_cover(
        self, client, library
    ):
        response = client.get(f"/api/library/albums/{DEMOS}/tracks")

        # Its picture is the one its album carries: its artist's.
        demos = client.get(f"/api/library/albums/{DEMOS}").get_json()
        beatles = client.get(f"/api/library/artists/{BEATLES}").get_json()
        assert demos["artwork_id"] == beatles["artwork_id"] is not None
        assert response.get_json()["data"] == [
            {
                "id": DEMOS_TRACK,
                "title": "Demo",
                "artist": "Tony Sheridan",
                "disc": None,
                "number": None,
                "duration_ms": None,
                "artwork_id": beatles["artwork_id"],
            }
        ]

    def test_a_track_whose_album_and_artist_have_no_picture_has_none(
        self, client, library
    ):
        library.add_album("3104", ["3205"], artist=ABBA, size=None)

        response = client.get("/api/library/albums/3104/tracks")

        assert [track["artwork_id"] for track in response.get_json()["data"]] == [None]

    def test_a_page_and_a_direction(self, client, library):
        response = client.get(
            f"/api/library/albums/{REVOLVER}/tracks?per_page=1&sort_dir=desc"
        )

        assert ids(response) == [REVOLVER_TRACKS[1]]
        assert response.get_json()["meta"] == meta(2, "position", "desc", per_page=1)

    @pytest.mark.parametrize("album_id", [*NO_KEYS, BEATLES, HELP_TRACK])
    def test_an_id_that_is_no_albums_is_not_found(self, client, library, album_id):
        response = client.get(f"/api/library/albums/{album_id}/tracks")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(
            "Album", album_id
        )

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get(f"/api/library/albums/{REVOLVER}/tracks")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            f"read the tracks of Plex album {REVOLVER}"
        )


class TestListPlaylists:
    def test_the_playlists_that_can_be_played(self, client, library):
        response = client.get("/api/library/playlists")

        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"] == meta(1, "title")
        (road_trip,) = body["data"]
        assert road_trip == {
            "id": ROAD_TRIP,
            "title": "Road trip",
            "track_count": 148,
            "duration_ms": 148 * 180_000,
            "artwork_id": road_trip["artwork_id"],
            "favourite_id": None,
        }
        assert road_trip["artwork_id"] is not None

    def test_a_playlist_of_1_and_one_of_1000_tracks_are_listed(self, client, plex):
        plex.add_playlist("4010", "A: none", 0)
        plex.add_playlist("4011", "B: one", 1)
        plex.add_playlist("4012", "C: the most", 1000)
        plex.add_playlist("4013", "D: one too many", 1001)

        response = client.get("/api/library/playlists")

        assert ids(response) == ["4011", "4012"]
        assert response.get_json()["meta"] == meta(2, "title")

    def test_by_title_a_page_and_a_direction(self, client, library):
        library.add_playlist("4010", "dinner", 12)
        library.add_playlist("4011", "Été", 30)

        ascending = client.get("/api/library/playlists")
        descending = client.get("/api/library/playlists?sort_dir=desc&per_page=2")

        assert ids(ascending) == ["4010", "4011", ROAD_TRIP]
        assert ids(descending) == [ROAD_TRIP, "4011"]
        assert descending.get_json()["meta"] == meta(3, "title", "desc", per_page=2)

    def test_a_playlists_artwork_id_is_one_the_artwork_endpoint_serves(
        self, client, library
    ):
        (road_trip,) = client.get("/api/library/playlists").get_json()["data"]

        assert near(colour_of(client, road_trip["artwork_id"]), PURPLE)

    def test_no_playlists_is_one_empty_page(self, client):
        response = client.get("/api/library/playlists")

        assert response.get_json() == {"data": [], "meta": meta(0, "title")}

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get("/api/library/playlists")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            "read Plex's playlists"
        )


class TestGetPlaylist:
    def test_one_playlist(self, client, library):
        response = client.get(f"/api/library/playlists/{ROAD_TRIP}")

        assert response.status_code == 200
        body = response.get_json()
        assert body == {
            "id": ROAD_TRIP,
            "title": "Road trip",
            "track_count": 148,
            "duration_ms": 148 * 180_000,
            "artwork_id": body["artwork_id"],
            "favourite_id": None,
        }
        assert near(colour_of(client, body["artwork_id"]), PURPLE)

    def test_a_playlist_the_list_leaves_out_is_served(self, client, library):
        empty = client.get(f"/api/library/playlists/{EMPTY}")
        everything = client.get(f"/api/library/playlists/{EVERYTHING}")

        assert empty.get_json() == {
            "id": EMPTY,
            "title": "Empty",
            "track_count": 0,
            "duration_ms": None,
            "artwork_id": None,
            "favourite_id": None,
        }
        assert everything.status_code == 200
        assert everything.get_json()["track_count"] == 12_019

    def test_every_request_gives_the_picture_a_new_id_and_the_older_still_answers(
        self, client, library
    ):
        first = client.get(f"/api/library/playlists/{ROAD_TRIP}").get_json()
        second = client.get(f"/api/library/playlists/{ROAD_TRIP}").get_json()

        assert first["artwork_id"] != second["artwork_id"]
        assert near(colour_of(client, first["artwork_id"]), PURPLE)
        assert near(colour_of(client, second["artwork_id"]), PURPLE)
        (listed,) = client.get("/api/library/playlists").get_json()["data"]
        assert listed["artwork_id"] == second["artwork_id"]

    @pytest.mark.parametrize("playlist_id", [*NO_KEYS, BEATLES, REVOLVER])
    def test_an_id_that_is_no_playlists_is_not_found(
        self, client, library, playlist_id
    ):
        response = client.get(f"/api/library/playlists/{playlist_id}")

        assert response.status_code == 404
        assert without_correlation_id(response.get_json()) == not_found(
            "Playlist", playlist_id
        )

    def test_a_plex_that_does_not_answer_is_a_bad_gateway(self, client, library):
        library.reachable = False

        response = client.get(f"/api/library/playlists/{ROAD_TRIP}")

        assert response.status_code == 502
        assert without_correlation_id(response.get_json()) == plex_failed(
            f"read Plex playlist {ROAD_TRIP}"
        )


# A mark and an unmark send favourites_changed; the test's own event sender
# leaves none behind for a later test's connections.
@pytest.mark.usefixtures("sender")
class TestLibraryFavourites:
    @pytest.mark.parametrize(
        ("kind", "item_id", "list_path", "item_path", "unmarked"),
        [
            (
                "artist",
                RADIOHEAD,
                ARTISTS,
                ARTIST.format(artist_id=RADIOHEAD),
                [BJORK],
            ),
            (
                "album",
                KID_A,
                RADIOHEADS_ALBUMS,
                ALBUM.format(album_id=KID_A),
                [AMNESIAC, AMNESIAC_AGAIN],
            ),
            (
                "playlist",
                MIX,
                PLAYLISTS,
                PLAYLIST.format(playlist_id=MIX),
                [DINNER],
            ),
        ],
    )
    def test_the_toggle_reads_the_mark_a_mark_answers_listed_and_alone(
        self, client, radiohead, kind, item_id, list_path, item_path, unmarked
    ):
        assert favourite_ids(client, list_path) == dict.fromkeys(
            [item_id, *unmarked], None
        )
        assert favourite_id(client, item_path) is None

        marked = mark(client, kind, item_id)

        assert marked.status_code == 201
        favourite = marked.get_json()["id"]
        assert favourite_ids(client, list_path) == {
            item_id: favourite,
            **dict.fromkeys(unmarked, None),
        }
        assert favourite_id(client, item_path) == favourite
        again = mark(client, kind, item_id)
        assert (again.status_code, again.get_json()["id"]) == (200, favourite)

        assert (
            client.delete(FAVOURITE.format(favourite_id=favourite)).status_code == 204
        )

        assert favourite_ids(client, list_path) == dict.fromkeys(
            [item_id, *unmarked], None
        )
        assert favourite_id(client, item_path) is None

    def test_both_copies_of_a_release_held_twice_carry_the_one_mark(
        self, client, radiohead
    ):
        favourite = mark(client, "album", AMNESIAC).get_json()["id"]

        assert favourite_ids(client, RADIOHEADS_ALBUMS) == {
            AMNESIAC: favourite,
            AMNESIAC_AGAIN: favourite,
            KID_A: None,
        }
        assert favourite_id(client, ALBUM.format(album_id=AMNESIAC_AGAIN)) == favourite
        other_copy = mark(client, "album", AMNESIAC_AGAIN)
        assert (other_copy.status_code, other_copy.get_json()["id"]) == (200, favourite)

        assert (
            client.delete(FAVOURITE.format(favourite_id=favourite)).status_code == 204
        )

        assert favourite_ids(client, RADIOHEADS_ALBUMS) == dict.fromkeys(
            [AMNESIAC, AMNESIAC_AGAIN, KID_A], None
        )

    def test_a_track_carries_no_mark(self, client, radiohead):
        mark(client, "album", AMNESIAC)

        (track,) = client.get(TRACKS.format(album_id=AMNESIAC)).get_json()["data"]

        assert track["id"] == RADIOHEADS_TRACK
        assert "favourite_id" not in track


class TestLibraryQueries:
    @pytest.mark.parametrize("path", LISTS)
    @pytest.mark.parametrize("query", REFUSED_QUERIES)
    def test_a_query_a_list_does_not_take_is_refused(
        self, client, library, path, query
    ):
        response = client.get(f"{path}?{query}")

        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("path", "sort_by"),
        [
            ("/api/library/artists", "title"),
            (f"/api/library/artists/{BEATLES}/albums", "year"),
            (f"/api/library/albums/{REVOLVER}/tracks", "title"),
            ("/api/library/playlists", "track_count"),
        ],
    )
    def test_a_list_sorts_by_nothing_else(self, client, library, path, sort_by):
        assert client.get(f"{path}?sort_by={sort_by}").status_code == 400

    @pytest.mark.parametrize("path", LISTS)
    def test_the_whole_query_spelled_out_is_the_default(self, client, library, path):
        default = client.get(path).get_json()
        spelled_out = default["meta"]

        response = client.get(
            f"{path}?page=1&per_page=20"
            f"&sort_by={spelled_out['sort_by']}&sort_dir={spelled_out['sort_dir']}"
        )

        assert response.status_code == 200
        assert response.get_json() == default

    def test_a_failure_names_no_address_and_no_token(
        self, client, library, test_app_settings
    ):
        library.reachable = False
        paths = [
            *LISTS,
            f"/api/library/artists/{BEATLES}",
            f"/api/library/albums/{REVOLVER}",
            f"/api/library/playlists/{ROAD_TRIP}",
        ]

        for path in paths:
            response = client.get(path)

            assert response.status_code == 502
            text = response.get_data(as_text=True)
            assert "192.0.2.1" not in text
            assert "32400" not in text
            assert test_app_settings.plex_token not in text


class TestLibraryDocument:
    def test_the_library_is_these_seven_routes(self, openapi_document):
        library = {
            path: sorted(operations)
            for path, operations in openapi_document["paths"].items()
            if path.startswith("/api/library")
        }

        assert library == dict.fromkeys([*LIST_PATHS, *ITEM_PATHS], ["get"])

    @pytest.mark.parametrize("path", [*LIST_PATHS, *ITEM_PATHS])
    def test_every_route_says_what_it_serves(self, openapi_document, path):
        operation = openapi_document["paths"][path]["get"]

        assert operation["summary"]
        for parameter in operation["parameters"]:
            assert parameter["description"], parameter["name"]
        for answer in operation["responses"].values():
            assert answer["description"]

    @pytest.mark.parametrize(
        ("path", "statuses"),
        [
            (ARTISTS, ["200", "400", "502"]),
            (ARTIST, ["200", "404", "502"]),
            (ALBUMS, ["200", "400", "404", "502"]),
            (ALBUM, ["200", "404", "502"]),
            (TRACKS, ["200", "400", "404", "502"]),
            (PLAYLISTS, ["200", "400", "502"]),
            (PLAYLIST, ["200", "404", "502"]),
        ],
    )
    def test_every_error_status_is_documented_with_the_envelope(
        self, openapi_document, path, statuses
    ):
        responses = openapi_document["paths"][path]["get"]["responses"]

        assert sorted(responses) == statuses
        for status in statuses[1:]:
            schema = responses[status]["content"]["application/json"]["schema"]
            assert "ApiErrorResponseSchema" in schema["$ref"]
        if "404" in statuses:
            assert "`ITEM_NOT_FOUND`" in responses["404"]["description"]

    @pytest.mark.parametrize(
        ("path", "sort_by", "default", "sort_dir"),
        [
            (ARTISTS, ["sort_title"], "sort_title", "asc"),
            (ALBUMS, ["release", "title"], "release", "desc"),
            (TRACKS, ["position"], "position", "asc"),
            (PLAYLISTS, ["title"], "title", "asc"),
        ],
    )
    def test_a_list_takes_the_paging_and_its_own_orders(
        self, openapi_document, path, sort_by, default, sort_dir
    ):
        operation = openapi_document["paths"][path]["get"]
        query = {
            parameter["name"]: schema_of(openapi_document, parameter["schema"])
            for parameter in operation["parameters"]
            if parameter["in"] == "query"
        }

        assert sorted(query) == ["page", "per_page", "sort_by", "sort_dir"]
        orders = query["sort_by"].get("enum") or [query["sort_by"]["const"]]
        assert orders == sort_by
        assert query["sort_by"]["default"] == default
        assert query["sort_dir"]["enum"] == ["asc", "desc"]
        assert query["sort_dir"]["default"] == sort_dir
        assert (query["per_page"]["minimum"], query["per_page"]["maximum"]) == (1, 100)
        assert query["page"]["minimum"] == 1

    @pytest.mark.parametrize("path", ITEM_PATHS)
    def test_one_item_takes_its_id_alone(self, openapi_document, path):
        operation = openapi_document["paths"][path]["get"]

        (parameter,) = operation["parameters"]
        assert parameter["in"] == "path"
        assert "rating key" in parameter["description"]

    @pytest.mark.parametrize(
        ("path", "fields"),
        [
            (ARTISTS, ["artwork_id", "favourite_id", "id", "index_letter", "title"]),
            (
                ALBUMS,
                [
                    "artist",
                    "artist_id",
                    "artwork_id",
                    "favourite_id",
                    "id",
                    "title",
                    "track_count",
                    "year",
                ],
            ),
            (
                TRACKS,
                [
                    "artist",
                    "artwork_id",
                    "disc",
                    "duration_ms",
                    "id",
                    "number",
                    "title",
                ],
            ),
            (
                PLAYLISTS,
                [
                    "artwork_id",
                    "duration_ms",
                    "favourite_id",
                    "id",
                    "title",
                    "track_count",
                ],
            ),
        ],
    )
    def test_a_list_answers_a_page_of_items_whose_fields_are_described(
        self, openapi_document, path, fields
    ):
        answer = openapi_document["paths"][path]["get"]["responses"]["200"]
        page = json_schema(openapi_document, answer)

        assert sorted(page["properties"]) == ["data", "meta"]
        assert sorted(page["required"]) == ["data", "meta"]
        item = schema_of(openapi_document, page["properties"]["data"]["items"])
        assert sorted(item["properties"]) == fields
        assert sorted(item["required"]) == fields
        assert undescribed(openapi_document, item) == []
        meta_schema = schema_of(openapi_document, page["properties"]["meta"])
        assert sorted(meta_schema["properties"]) == [
            "page",
            "per_page",
            "sort_by",
            "sort_dir",
            "total",
            "total_pages",
        ]

    @pytest.mark.parametrize(
        ("item_path", "list_path"),
        [(ARTIST, ARTISTS), (ALBUM, ALBUMS), (PLAYLIST, PLAYLISTS)],
    )
    def test_one_item_is_what_its_list_holds(
        self, openapi_document, item_path, list_path
    ):
        paths = openapi_document["paths"]
        item = json_schema(
            openapi_document, paths[item_path]["get"]["responses"]["200"]
        )
        page = json_schema(
            openapi_document, paths[list_path]["get"]["responses"]["200"]
        )

        assert item == schema_of(openapi_document, page["properties"]["data"]["items"])

    @pytest.mark.parametrize("path", ITEM_PATHS)
    def test_an_item_carries_the_id_of_the_favourite_that_marks_it_or_null(
        self, openapi_document, path
    ):
        answer = openapi_document["paths"][path]["get"]["responses"]["200"]
        item = json_schema(openapi_document, answer)

        field = item["properties"]["favourite_id"]
        assert field["anyOf"] == [{"type": "integer"}, {"type": "null"}]
        assert "`POST /api/favourites`" in field["description"]
        assert "`null`" in field["description"]

    def test_an_index_letter_is_a_letter_or_the_hash(self, openapi_document):
        answer = openapi_document["paths"][ARTIST]["get"]["responses"]["200"]
        artist = json_schema(openapi_document, answer)

        assert artist["properties"]["index_letter"]["pattern"] == "^[A-Z#]$"

    def test_there_is_no_search(self, openapi_document):
        assert not [path for path in openapi_document["paths"] if "search" in path]
        for path in [*LIST_PATHS, *ITEM_PATHS]:
            operation = openapi_document["paths"][path]["get"]
            names = {parameter["name"] for parameter in operation["parameters"]}
            assert names <= {
                "artist_id",
                "album_id",
                "playlist_id",
                "page",
                "per_page",
                "sort_by",
                "sort_dir",
            }
