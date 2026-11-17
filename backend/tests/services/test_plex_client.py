"""Tests for the Plex client, on the in-memory library.

What ``PlexClient`` promises its callers, shown on ``FakePlexClient``; the
live suite (``tests/live/``) shows the same of the plexapi client on the real
Plex. The plexapi client itself is tested here only where it needs no Plex:
what it refuses to ask, and a Plex that does not answer.
"""

import io
import socket
from dataclasses import replace
from datetime import date

import pytest
from PIL import Image

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckStatus
from app.services.plex_client import PlexapiPlexClient
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist, PlexTrack
from tests.fakes.plex_client import (
    ARTIST,
    MACHINE_IDENTIFIER,
    SECTION_ID,
    FakePlexClient,
)

ALBUM = "2001"
TRACKS = ("2002", "2003", "2004")
OTHER_ALBUM = "2101"
OTHER_TRACK = "2102"
BARE_ALBUM = "2201"
BARE_TRACK = "2202"

BEATLES = "3001"
ABBA = "3002"
BJORK = "3003"
BARE_ARTIST = "3004"

REVOLVER = "3101"
HELP = "3102"
ANTHOLOGY = "3103"
BOOTLEG = "3104"
DEMOS = "3105"
ARRIVAL = "3106"

REVOLVER_TRACKS = ("3201", "3202")
ANTHOLOGY_TRACKS = ("3211", "3212", "3213", "3214", "3215")

MIX = "3301"
EMPTY = "3302"
EVERYTHING = "3303"

RED = (200, 60, 30)
BLUE = (20, 40, 200)
GREEN = (30, 160, 60)

SMALL = (16, 16)

TOKEN = "a-token-no-failure-may-carry"

NOT_A_PICTURE = [
    "",
    "http://192.0.2.1/cover.jpg",
    "//192.0.2.1/library/metadata/2001/thumb/1700000000",
    "/library/sections",
    "/library/metadata/2001",
    "/library/metadata/2001/thumb/1700000000?url=http://192.0.2.1/",
    "/library/metadata/2001/thumb/1700000000/../../../../identity",
    "/playlists",
    "/playlists/3301",
    "/playlists/3301/composite",
    "/playlists/abc/composite/1700000000",
    "/playlists/3301/composite/1700000000?url=http://192.0.2.1/",
    "/playlists/3301/composite/1700000000/../../../identity",
    "/playlists/3301/thumb/1700000000",
    "/library/metadata/3301/composite/1700000000",
]

NOT_A_RATING_KEY = ["", "abc", "12/../../identity", "1,2", "12?x=1"]

# Every read of the library, as (name, call, what the client says it does).
READS = [
    ("section_id", lambda plex: plex.section_id(), "read Plex's music library"),
    (
        "read_artists",
        lambda plex: plex.read_artists(),
        "read the artists of Plex's music library",
    ),
    ("read_artist", lambda plex: plex.read_artist(BEATLES), "read Plex artist 3001"),
    (
        "read_artist_albums",
        lambda plex: plex.read_artist_albums(BEATLES, newest_first=True),
        "read the albums of Plex artist 3001",
    ),
    ("read_album", lambda plex: plex.read_album(REVOLVER), "read Plex album 3101"),
    (
        "read_album_tracks",
        lambda plex: plex.read_album_tracks(REVOLVER),
        "read the tracks of Plex album 3101",
    ),
    ("read_track", lambda plex: plex.read_track("3201"), "read Plex track 3201"),
    ("read_playlists", lambda plex: plex.read_playlists(), "read Plex's playlists"),
    (
        "read_playlist",
        lambda plex: plex.read_playlist(MIX),
        "read Plex playlist 3301",
    ),
    (
        "read_track_pictures",
        lambda plex: plex.read_track_pictures(["3201", "3202"]),
        "read the pictures of 2 Plex tracks",
    ),
    (
        "read_items",
        lambda plex: plex.read_items([BEATLES, REVOLVER, MIX]),
        "read 3 Plex items",
    ),
    (
        "find_by_guid",
        lambda plex: plex.find_by_guid(PlexAlbum, "plex://album/3101"),
        "find the Plex albums with guid plex://album/3101",
    ),
    (
        "find_by_title",
        lambda plex: plex.find_by_title(PlexArtist, "ABBA"),
        "find the Plex artists titled 'ABBA'",
    ),
    (
        "find_by_title_a_playlist",
        lambda plex: plex.find_by_title(PlexPlaylist, "Mix"),
        "find the Plex playlists titled 'Mix'",
    ),
]

# The reads of one item, by its rating key.
READS_BY_KEY = [
    ("read_artist", lambda plex, key: plex.read_artist(key)),
    (
        "read_artist_albums",
        lambda plex, key: plex.read_artist_albums(key, newest_first=True),
    ),
    ("read_album", lambda plex, key: plex.read_album(key)),
    ("read_album_tracks", lambda plex, key: plex.read_album_tracks(key)),
    ("read_track", lambda plex, key: plex.read_track(key)),
    ("read_playlist", lambda plex, key: plex.read_playlist(key)),
]


@pytest.fixture
def library(plex: FakePlexClient) -> FakePlexClient:
    """Two albums with a picture, one of them not square, and one without."""
    plex.add_album(ALBUM, TRACKS, size=(1000, 1000), colour=RED)
    plex.add_album(OTHER_ALBUM, [OTHER_TRACK], size=(1000, 600), colour=BLUE)
    plex.add_album(BARE_ALBUM, [BARE_TRACK], size=None)
    return plex


@pytest.fixture
def music(plex: FakePlexClient) -> FakePlexClient:
    """Three artists with albums and one without; albums with and without a
    date, two of one date; a compilation on two discs; and three playlists,
    one empty and one larger than a speaker takes."""
    plex.add_artist(BEATLES, "The Beatles", sort_title="Beatles", colour=GREEN)
    plex.add_artist(ABBA, "ABBA")
    plex.add_artist(BJORK, "Björk", sort_title="Bjork")
    plex.add_artist(BARE_ARTIST, "ZZ Top", size=None)

    plex.add_album(
        REVOLVER,
        REVOLVER_TRACKS,
        colour=RED,
        artist=BEATLES,
        title="Revolver",
        released=date(1966, 8, 5),
    )
    plex.add_album(
        HELP, ["3221"], artist=BEATLES, title="Help!", released=date(1965, 8, 6)
    )
    plex.add_album(
        ANTHOLOGY, [], artist=BEATLES, title="Anthology", released=date(1966, 8, 5)
    )
    plex.add_album(BOOTLEG, ["3231"], size=None, artist=BEATLES, title="Bootleg")
    plex.add_album(DEMOS, ["3241"], artist=BEATLES, title="Demos")
    plex.add_album(
        ARRIVAL, ["3251"], artist=ABBA, title="Arrival", released=date(1976, 10, 11)
    )

    first, second, third, fourth, fifth = ANTHOLOGY_TRACKS
    plex.add_track(ANTHOLOGY, first, title="Second of two", disc=2, number=1)
    plex.add_track(ANTHOLOGY, second, title="Tenth", disc=1, number=10)
    plex.add_track(
        ANTHOLOGY, third, title="Second", artist="Tony Sheridan", disc=1, number=2
    )
    plex.add_track(
        ANTHOLOGY, fourth, title="Bare", disc=None, number=None, duration_ms=None
    )
    plex.add_track(ANTHOLOGY, fifth, title="First", disc=1, number=1, duration_ms=95_000)

    plex.add_playlist(MIX, "Mix", 148, colour=BLUE)
    plex.add_playlist(EMPTY, "Recently Added", 0)
    plex.add_playlist(EVERYTHING, "All Music", 12_019)
    return plex


@pytest.fixture
def unanswered(app) -> PlexapiPlexClient:
    """The plexapi client on an address nothing listens at."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return PlexapiPlexClient(
        settings=AppSettings(
            sonos_seed_hosts=["192.0.2.1"],
            sonos_event_advertise_host="sonos-events.invalid",
            plex_url=f"http://127.0.0.1:{port}/",
            plex_token=TOKEN,
        ),
        health_service=app.container.health_service(),
    )


def _image(jpeg: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    return image


def _assert_colour(image: Image.Image, colour: tuple[int, int, int]) -> None:
    """The picture's colour, within what JPEG changes of it."""
    pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
    assert all(abs(got - want) <= 8 for got, want in zip(pixel, colour, strict=True))


class TestPlexClientReadiness:
    def test_the_check_is_registered_and_not_critical(self, library, client):
        response = client.get("/health/readyz")

        assert response.status_code == 200
        assert response.get_json()["checks"]["plex"] == {
            "status": "ok",
            "critical": False,
        }

    def test_a_plex_that_does_not_answer_degrades_and_stays_ready(
        self, library, client
    ):
        library.reachable = False

        response = client.get("/health/readyz")

        body = response.get_json()
        assert response.status_code == 200
        assert body["ready"] is True
        assert body["status"] == "degraded"
        assert body["checks"]["plex"] == {
            "status": "error",
            "critical": False,
            "detail": "Plex did not answer (ConnectionError)",
        }

    def test_a_plex_without_the_music_library_degrades(self, library, client):
        library.has_library = False

        body = client.get("/health/readyz").get_json()

        assert body["checks"]["plex"] == {
            "status": "error",
            "critical": False,
            "detail": "Plex has no music library named 'Music'",
        }

    def test_the_container_provides_the_plexapi_client(self, app):
        assert app.container.plex_client.provides is PlexapiPlexClient

    def test_the_plexapi_client_is_built_without_plex(self, unanswered):
        assert isinstance(unanswered, PlexapiPlexClient)

    def test_the_plexapi_client_reports_a_plex_that_does_not_answer(
        self, unanswered
    ):
        result = unanswered.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "Plex did not answer (ConnectionError)"


class TestPlexClientMachineIdentifier:
    def test_it_is_the_servers(self, library):
        assert library.machine_identifier() == MACHINE_IDENTIFIER

    def test_a_plex_that_has_never_answered_fails(self, library):
        library.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.machine_identifier()

        assert raised.value.operation == "read which server Plex is"
        assert raised.value.cause == "Plex did not answer (ConnectionError)"

        library.reachable = True
        assert library.machine_identifier() == MACHINE_IDENTIFIER

    @pytest.mark.parametrize(
        "answered",
        [
            lambda plex: plex.machine_identifier(),
            lambda plex: plex.read_track_picture(TRACKS[0]),
            lambda plex: plex.check_readiness(),
            *(read for _, read, _ in READS),
        ],
        ids=[
            "machine_identifier",
            "read_track_picture",
            "check_readiness",
            *(name for name, _, _ in READS),
        ],
    )
    def test_it_is_held_from_plexs_first_answer(self, library, answered):
        answered(library)
        library.reachable = False

        assert library.machine_identifier() == MACHINE_IDENTIFIER

    def test_a_picture_is_read_without_reading_the_server(self, library):
        assert library.read_picture(f"/library/metadata/{ALBUM}/thumb/1", 100)
        library.reachable = False

        with pytest.raises(ExternalServiceException):
            library.machine_identifier()

    def test_the_plexapi_client_fails_without_the_token_or_the_address(
        self, unanswered
    ):
        with pytest.raises(ExternalServiceException) as raised:
            unanswered.machine_identifier()

        assert raised.value.message == (
            "Cannot read which server Plex is because external service "
            "failed: Plex did not answer (ConnectionError)"
        )


class TestPlexClientLibrary:
    """What every read of the library has in common."""

    def test_the_section_id_is_the_music_librarys(self, music):
        assert music.section_id() == SECTION_ID

    @pytest.mark.parametrize(
        ("read", "operation"),
        [(read, operation) for _, read, operation in READS],
        ids=[name for name, _, _ in READS],
    )
    def test_a_plex_that_does_not_answer_fails(self, music, read, operation):
        music.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            read(music)

        assert raised.value.operation == operation
        assert raised.value.cause == "Plex did not answer (ConnectionError)"

    @pytest.mark.parametrize(
        ("read", "operation"),
        [
            (read, operation)
            for _, read, operation in READS[:2] + READS[3:4] + READS[-3:-1]
        ],
        ids=[
            "section_id",
            "read_artists",
            "read_artist_albums",
            "find_by_guid",
            "find_by_title",
        ],
    )
    def test_a_plex_without_the_music_library_fails(self, music, read, operation):
        music.has_library = False

        with pytest.raises(ExternalServiceException) as raised:
            read(music)

        assert raised.value.operation == operation
        assert raised.value.cause == "Plex has no music library named 'Music'"

    @pytest.mark.parametrize(
        ("read", "operation"),
        [(read, operation) for _, read, operation in READS],
        ids=[name for name, _, _ in READS],
    )
    def test_the_plexapi_client_fails_without_the_token_or_the_address(
        self, unanswered, read, operation
    ):
        with pytest.raises(ExternalServiceException) as raised:
            read(unanswered)

        assert raised.value.message == (
            f"Cannot {operation} because external service failed: Plex did not "
            "answer (ConnectionError)"
        )

    @pytest.mark.parametrize("rating_key", NOT_A_RATING_KEY)
    @pytest.mark.parametrize(
        "read",
        [read for _, read in READS_BY_KEY],
        ids=[name for name, _ in READS_BY_KEY],
    )
    def test_the_plexapi_client_asks_plex_for_a_rating_key_only(
        self, unanswered, read, rating_key
    ):
        # Plex does not answer: a call that reached it would fail.
        assert read(unanswered, rating_key) is None

    def test_the_plexapi_client_asks_for_the_pictures_of_rating_keys_only(
        self, unanswered
    ):
        # Plex does not answer: a call that reached it would fail.
        assert unanswered.read_track_pictures(NOT_A_RATING_KEY) == {}
        assert unanswered.read_track_pictures([]) == {}

    def test_the_plexapi_client_asks_for_the_items_of_rating_keys_only(
        self, unanswered
    ):
        # Plex does not answer: a call that reached it would fail.
        assert unanswered.read_items(NOT_A_RATING_KEY) == {}
        assert unanswered.read_items([]) == {}

    def test_nothing_of_the_library_is_kept(self, music):
        before = music.read_artists()

        music.add_artist("3009", "Aerosmith")
        music.add_album("3109", ["3209"], artist=ABBA, released=date(1977, 12, 12))
        music.add_playlist("3309", "New", 3)

        assert len(music.read_artists()) == len(before) + 1
        assert len(music.read_artist_albums(ABBA, newest_first=True)) == 2
        assert music.read_playlist("3309") is not None


class TestPlexClientArtists:
    def test_the_artists_come_as_plex_files_them(self, music):
        artists = music.read_artists()

        # The Beatles files under its sort title, ABBA under its title.
        assert [artist.title for artist in artists] == [
            "ABBA",
            "The Beatles",
            "Björk",
            "ZZ Top",
        ]
        assert [artist.sort_title for artist in artists] == [
            "ABBA",
            "Beatles",
            "Bjork",
            "ZZ Top",
        ]

    def test_an_artist_has_its_picture(self, music):
        artist = music.read_artist(BEATLES)

        assert artist == PlexArtist(
            rating_key=BEATLES,
            guid=artist.guid,
            title="The Beatles",
            sort_title="Beatles",
            picture=artist.picture,
        )
        assert artist.picture.startswith(f"/library/metadata/{BEATLES}/thumb/")
        _assert_colour(_image(music.read_picture(artist.picture, 100)), GREEN)

    def test_an_artist_without_a_picture_has_none(self, music):
        assert music.read_artist(BARE_ARTIST).picture is None
        [bare] = [a for a in music.read_artists() if a.rating_key == BARE_ARTIST]
        assert bare.picture is None

    def test_one_artist_is_the_one_listed(self, music):
        for artist in music.read_artists():
            assert music.read_artist(artist.rating_key) == artist

    @pytest.mark.parametrize("rating_key", ["9999", REVOLVER, "3201", MIX])
    def test_an_artist_plex_does_not_have_is_none(self, music, rating_key):
        assert music.read_artist(rating_key) is None

    def test_the_artist_of_an_album_added_without_one(self, library):
        [artist] = library.read_artists()

        assert artist.rating_key == ARTIST
        assert artist.picture is None
        assert len(library.read_artist_albums(ARTIST, newest_first=True)) == 3


class TestPlexClientAlbums:
    def test_newest_first(self, music):
        albums = music.read_artist_albums(BEATLES, newest_first=True)

        # Two of one date by title, the albums without a date last.
        assert [album.title for album in albums] == [
            "Revolver",
            "Anthology",
            "Help!",
            "Demos",
            "Bootleg",
        ]

    def test_oldest_first(self, music):
        albums = music.read_artist_albums(BEATLES, newest_first=False)

        assert [album.title for album in albums] == [
            "Help!",
            "Anthology",
            "Revolver",
            "Bootleg",
            "Demos",
        ]

    def test_albums_of_one_date_and_title_are_in_rating_key_order(self, music):
        music.add_album("3190", [], artist=ABBA, title="Arrival", released=date(1976, 10, 11))
        music.add_album("3180", [], artist=ABBA, title="arrival", released=date(1976, 10, 11))

        oldest = music.read_artist_albums(ABBA, newest_first=False)
        newest = music.read_artist_albums(ABBA, newest_first=True)

        assert [album.rating_key for album in oldest] == [ARRIVAL, "3180", "3190"]
        assert [album.rating_key for album in newest] == ["3190", "3180", ARRIVAL]

    def test_an_album_has_its_artist_year_track_count_and_picture(self, music):
        album = music.read_album(REVOLVER)

        assert album == PlexAlbum(
            rating_key=REVOLVER,
            guid=album.guid,
            title="Revolver",
            artist_rating_key=BEATLES,
            artist="The Beatles",
            released=date(1966, 8, 5),
            track_count=2,
            picture=album.picture,
        )
        assert album.year == 1966
        assert album.picture.startswith(f"/library/metadata/{REVOLVER}/thumb/")
        _assert_colour(_image(music.read_picture(album.picture, 100)), RED)

    def test_an_album_without_a_date_or_a_cover_has_its_artists_picture(self, music):
        album = music.read_album(BOOTLEG)

        assert album.released is None
        assert album.year is None
        assert album.picture == music.read_artist(BEATLES).picture

    def test_one_album_is_the_one_listed(self, music):
        for album in music.read_artist_albums(BEATLES, newest_first=True):
            assert music.read_album(album.rating_key) == album

    def test_an_artist_without_albums_has_none(self, music):
        assert music.read_artist_albums(BJORK, newest_first=True) == []

    @pytest.mark.parametrize("rating_key", ["9999", REVOLVER, "3201", MIX])
    def test_the_albums_of_an_artist_plex_does_not_have_are_none(
        self, music, rating_key
    ):
        assert music.read_artist_albums(rating_key, newest_first=True) is None

    @pytest.mark.parametrize("rating_key", ["9999", BEATLES, "3201", MIX])
    def test_an_album_plex_does_not_have_is_none(self, music, rating_key):
        assert music.read_album(rating_key) is None


class TestPlexClientTracks:
    def test_an_albums_tracks_come_in_disc_and_track_order(self, music):
        tracks = music.read_album_tracks(ANTHOLOGY)

        assert [(track.disc, track.number, track.title) for track in tracks] == [
            (None, None, "Bare"),
            (1, 1, "First"),
            (1, 2, "Second"),
            (1, 10, "Tenth"),
            (2, 1, "Second of two"),
        ]

    def test_a_track_has_its_own_artist_where_plex_has_one(self, music):
        artists = {
            track.title: track.artist for track in music.read_album_tracks(ANTHOLOGY)
        }

        assert artists["Second"] == "Tony Sheridan"
        assert artists["First"] == "The Beatles"

    def test_a_track_has_its_duration_and_its_albums_picture(self, music):
        track = music.read_track(ANTHOLOGY_TRACKS[4])

        assert track == PlexTrack(
            rating_key=ANTHOLOGY_TRACKS[4],
            title="First",
            artist="The Beatles",
            disc=1,
            number=1,
            duration_ms=95_000,
            picture=music.read_album(ANTHOLOGY).picture,
        )
        assert track.picture == music.read_track_picture(ANTHOLOGY_TRACKS[4])

    def test_a_track_without_a_disc_a_number_or_a_duration_has_none(self, music):
        track = music.read_track(ANTHOLOGY_TRACKS[3])

        assert (track.disc, track.number, track.duration_ms) == (None, None, None)

    def test_a_track_of_an_album_without_a_cover_has_its_artists_picture(self, music):
        [track] = music.read_album_tracks(BOOTLEG)

        assert track.picture == music.read_artist(BEATLES).picture
        assert track.picture == music.read_album(BOOTLEG).picture
        assert music.read_track(track.rating_key) == track

    def test_a_track_of_an_album_and_an_artist_without_a_picture_has_none(
        self, library
    ):
        [track] = library.read_album_tracks(BARE_ALBUM)

        assert track.picture is None
        assert library.read_track(BARE_TRACK) == track

    def test_one_track_is_the_one_listed(self, music):
        for track in music.read_album_tracks(ANTHOLOGY):
            assert music.read_track(track.rating_key) == track

    def test_the_tracks_added_with_an_album_are_numbered_in_the_order_given(
        self, music
    ):
        tracks = music.read_album_tracks(REVOLVER)

        assert [track.rating_key for track in tracks] == list(REVOLVER_TRACKS)
        assert [(track.disc, track.number) for track in tracks] == [(1, 1), (1, 2)]

    @pytest.mark.parametrize("rating_key", ["9999", BEATLES, "3201", MIX])
    def test_the_tracks_of_an_album_plex_does_not_have_are_none(
        self, music, rating_key
    ):
        assert music.read_album_tracks(rating_key) is None

    @pytest.mark.parametrize("rating_key", ["9999", BEATLES, REVOLVER, MIX])
    def test_a_track_plex_does_not_have_is_none(self, music, rating_key):
        assert music.read_track(rating_key) is None


class TestPlexClientPlaylists:
    def test_every_playlist_is_listed(self, music):
        playlists = {playlist.title: playlist for playlist in music.read_playlists()}

        # The empty one and the one a speaker refuses too: which are shown
        # is the library's to say.
        assert {title: p.track_count for title, p in playlists.items()} == {
            "Mix": 148,
            "Recently Added": 0,
            "All Music": 12_019,
        }

    def test_a_playlist_has_its_duration_and_its_picture(self, music):
        playlist = music.read_playlist(MIX)

        assert playlist == PlexPlaylist(
            rating_key=MIX,
            guid=playlist.guid,
            title="Mix",
            track_count=148,
            duration_ms=148 * 180_000,
            picture=playlist.picture,
        )
        assert playlist.picture.startswith(f"/playlists/{MIX}/composite/")
        assert playlist.picture.removeprefix(f"/playlists/{MIX}/composite/").isdigit()
        _assert_colour(_image(music.read_picture(playlist.picture, 100)), BLUE)

    def test_a_playlist_without_tracks_has_no_duration_and_no_picture(self, music):
        playlist = music.read_playlist(EMPTY)

        assert playlist.track_count == 0
        assert playlist.duration_ms is None
        assert playlist.picture is None

    def test_one_playlist_is_the_one_listed_with_a_new_stamp_on_its_picture(
        self, music
    ):
        listed = {playlist.rating_key: playlist for playlist in music.read_playlists()}

        read = {key: music.read_playlist(key) for key in listed}

        assert read[EMPTY] == listed[EMPTY]
        for key in (MIX, EVERYTHING):
            assert read[key].picture != listed[key].picture
            assert read[key].picture.startswith(f"/playlists/{key}/composite/")
            assert read[key] == PlexPlaylist(
                rating_key=key,
                guid=listed[key].guid,
                title=listed[key].title,
                track_count=listed[key].track_count,
                duration_ms=listed[key].duration_ms,
                picture=read[key].picture,
            )
        # The list has the stamp of the last read of the playlist on its own.
        assert {p.rating_key: p for p in music.read_playlists()} == read

    @pytest.mark.parametrize("rating_key", ["9999", BEATLES, REVOLVER, "3201"])
    def test_a_playlist_plex_does_not_have_is_none(self, music, rating_key):
        assert music.read_playlist(rating_key) is None


class TestPlexClientGuid:
    def test_every_artist_album_and_playlist_has_a_guid_of_its_own(self, music):
        items = [
            *music.read_artists(),
            *music.read_artist_albums(BEATLES, newest_first=True),
            *music.read_artist_albums(ABBA, newest_first=True),
            *music.read_playlists(),
        ]

        guids = [item.guid for item in items]
        assert all(guids)
        assert len(set(guids)) == len(guids)

    def test_the_list_and_the_read_of_one_item_carry_its_guid(self, music):
        for artist in music.read_artists():
            assert music.read_artist(artist.rating_key).guid == artist.guid
        for playlist in music.read_playlists():
            assert music.read_playlist(playlist.rating_key).guid == playlist.guid

    def test_a_test_gives_an_item_its_guid(self, music):
        music.add_artist("3009", "Aerosmith", guid="plex://artist/aerosmith")
        music.add_album("3109", [], artist=ABBA, guid="plex://album/abba-gold")
        music.add_playlist("3309", "New", 0, guid="com.plexapp.agents.none://new")

        assert music.read_artist("3009").guid == "plex://artist/aerosmith"
        assert music.read_album("3109").guid == "plex://album/abba-gold"
        assert music.read_playlist("3309").guid == "com.plexapp.agents.none://new"

    def test_a_release_held_twice_has_one_guid(self, music):
        # A guid is not unique (docs/api-design.md section 1.8).
        guid = music.read_album(ARRIVAL).guid
        music.add_album("3108", ["3252"], artist=ABBA, title="Arrival", guid=guid)

        assert music.read_album("3108").guid == guid
        assert music.read_album("3108").rating_key != ARRIVAL

    def test_an_item_plex_has_not_matched_has_a_local_guid(self, music):
        music.add_album("3109", ["3261"], artist=BARE_ARTIST, guid="local://3109")

        assert music.read_album("3109").guid == "local://3109"


class TestPlexClientItems:
    def test_an_artist_an_album_and_a_playlist_together(self, music):
        [mix] = [p for p in music.read_playlists() if p.rating_key == MIX]

        items = music.read_items([BEATLES, REVOLVER, MIX])

        assert items == {
            BEATLES: music.read_artist(BEATLES),
            REVOLVER: music.read_album(REVOLVER),
            MIX: mix,
        }

    def test_a_playlist_is_the_one_listed_and_keeps_the_stamp_on_its_picture(
        self, music
    ):
        listed = {p.rating_key: p for p in music.read_playlists()}

        items = music.read_items(list(listed))

        assert items == listed
        assert {p.rating_key: p for p in music.read_playlists()} == listed

    def test_a_playlist_read_on_its_own_has_the_stamp_that_read_gave_it(self, music):
        read = music.read_playlist(MIX)

        assert music.read_items([MIX]) == {MIX: read}

    def test_what_is_no_artist_album_or_playlist_is_left_out(self, music):
        items = music.read_items(["9999", REVOLVER_TRACKS[0], ARRIVAL])

        assert list(items) == [ARRIVAL]

    def test_an_item_asked_twice_is_there_once(self, music):
        assert list(music.read_items([ABBA, ABBA])) == [ABBA]

    def test_items_plex_does_not_have_are_left_out(self, music):
        assert music.read_items(["9998", "9999"]) == {}

    def test_no_items_are_not_asked_for(self, music):
        music.reachable = False

        assert music.read_items([]) == {}


@pytest.fixture
def few(plex: FakePlexClient) -> FakePlexClient:
    """Two artists, three albums with tracks and two playlists, their
    pictures small (the fake's default ones are large, card SCTL-14)."""
    plex.add_artist(BEATLES, "The Beatles", sort_title="Beatles", size=SMALL)
    plex.add_artist(BJORK, "Björk", size=None)
    plex.add_album(
        REVOLVER, REVOLVER_TRACKS, size=SMALL, artist=BEATLES, title="Revolver"
    )
    plex.add_album(HELP, ["3221"], size=None, artist=BEATLES, title="Help!")
    plex.add_album(ARRIVAL, ["3251"], size=SMALL, artist=BJORK, title="Arrival")
    plex.add_playlist(MIX, "Mix", 2, size=SMALL)
    plex.add_playlist(EMPTY, "Recently Added", 0)
    return plex


class TestPlexClientFind:
    def test_an_artist_an_album_and_a_playlist_are_found_by_guid(self, few):
        beatles = few.read_artist(BEATLES)
        revolver = few.read_album(REVOLVER)
        [mix] = [p for p in few.read_playlists() if p.rating_key == MIX]

        assert few.find_by_guid(PlexArtist, beatles.guid) == [beatles]
        assert few.find_by_guid(PlexAlbum, revolver.guid) == [revolver]
        assert few.find_by_guid(PlexPlaylist, mix.guid) == [mix]

    def test_every_album_of_a_release_held_twice_is_found(self, few):
        arrival = few.read_album(ARRIVAL)
        few.add_album("3108", ["3252"], size=None, artist=BJORK, guid=arrival.guid)

        found = few.find_by_guid(PlexAlbum, arrival.guid)

        assert found == [arrival, few.read_album("3108")]

    def test_a_guid_finds_the_items_of_its_kind_only(self, few):
        beatles = few.read_artist(BEATLES)
        few.add_album("3109", [], size=None, artist=BJORK, guid=beatles.guid)

        assert few.find_by_guid(PlexArtist, beatles.guid) == [beatles]
        assert few.find_by_guid(PlexPlaylist, beatles.guid) == []
        assert few.find_by_guid(PlexArtist, "plex://artist/none") == []

    def test_an_artist_an_album_and_a_playlist_are_found_by_title(self, few):
        [mix] = [p for p in few.read_playlists() if p.rating_key == MIX]

        assert few.find_by_title(PlexArtist, "The Beatles") == [
            few.read_artist(BEATLES)
        ]
        assert few.find_by_title(PlexAlbum, "Revolver") == [few.read_album(REVOLVER)]
        assert few.find_by_title(PlexPlaylist, "Mix") == [mix]

    def test_every_item_so_titled_is_found(self, few):
        few.add_album("3108", [], size=None, artist=BEATLES, title="Arrival")

        found = few.find_by_title(PlexAlbum, "Arrival")

        assert found == [few.read_album(ARRIVAL), few.read_album("3108")]

    @pytest.mark.parametrize(
        "title", ["the beatles", "Beatles", "The Beatle", "Bjork", "Revolver"]
    )
    def test_a_title_is_found_exactly_its_case_and_accents_included(self, few, title):
        # "Revolver" is an album's: an artist so titled is none.
        assert few.find_by_title(PlexArtist, title) == []

    def test_a_playlist_found_keeps_the_stamp_on_its_picture(self, few):
        listed = {p.rating_key: p for p in few.read_playlists()}

        assert few.find_by_guid(PlexPlaylist, listed[MIX].guid) == [listed[MIX]]
        assert few.find_by_title(PlexPlaylist, "Mix") == [listed[MIX]]
        assert {p.rating_key: p for p in few.read_playlists()} == listed

    def test_a_playlist_is_found_without_the_music_library(self, few):
        few.has_library = False

        found = few.find_by_title(PlexPlaylist, "Mix")

        assert [p.rating_key for p in found] == [MIX]


class TestPlexClientMovedItems:
    """The fake's ``move()``: Plex sees an item as new."""

    def test_an_album_moved_is_at_its_new_rating_key_with_its_tracks(self, few):
        before = few.read_album(REVOLVER)
        tracks = few.read_album_tracks(REVOLVER)

        few.move(REVOLVER, "3109")

        assert few.read_album(REVOLVER) is None
        moved = few.read_album("3109")
        assert moved.picture != before.picture
        assert moved == replace(before, rating_key="3109", picture=moved.picture)
        assert few.read_album_tracks("3109") == [
            replace(track, picture=moved.picture) for track in tracks
        ]
        assert few.read_picture(moved.picture, 16) is not None

    def test_an_artist_moved_takes_its_albums(self, few):
        albums = few.read_artist_albums(BEATLES, newest_first=True)

        few.move(BEATLES, "3009")

        assert few.read_artist(BEATLES) is None
        assert few.read_artist("3009").guid == f"plex://artist/{BEATLES}"
        moved = few.read_artist_albums("3009", newest_first=True)
        assert [a.rating_key for a in moved] == [a.rating_key for a in albums]
        assert {a.artist_rating_key for a in moved} == {"3009"}

    def test_a_playlist_moved_is_at_its_new_rating_key(self, few):
        before = few.read_playlist(MIX)

        few.move(MIX, "3309")

        assert few.read_playlist(MIX) is None
        moved = few.read_playlist("3309")
        assert moved == replace(before, rating_key="3309", picture=moved.picture)

    def test_an_item_moved_has_the_guid_and_the_title_given(self, few):
        few.move(ARRIVAL, "3109", guid="local://3109", title="Arrival (Deluxe)")

        moved = few.read_album("3109")
        assert (moved.guid, moved.title) == ("local://3109", "Arrival (Deluxe)")

    def test_an_item_is_not_moved_onto_another(self, few):
        with pytest.raises(KeyError):
            few.move(ARRIVAL, REVOLVER)


class TestPlexClientTrackPictures:
    def test_the_pictures_of_several_tracks(self, music):
        first, second = REVOLVER_TRACKS

        pictures = music.read_track_pictures([first, "3251", second])

        assert pictures == {
            first: music.read_track_picture(first),
            second: music.read_track_picture(second),
            "3251": music.read_track_picture("3251"),
        }
        assert pictures[first] == pictures[second] != pictures["3251"]

    def test_a_rating_key_that_is_no_tracks_is_left_out(self, music):
        first = REVOLVER_TRACKS[0]

        pictures = music.read_track_pictures(["9999", first, REVOLVER, BEATLES, MIX])

        assert list(pictures) == [first]

    def test_a_track_without_a_picture_is_left_out(self, music):
        music.add_album("3107", ["3261"], size=None, artist=BARE_ARTIST)

        assert music.read_track_pictures(["3261", "3241"]) == {
            "3241": music.read_track_picture("3241")
        }

    def test_a_track_of_an_album_without_a_cover_has_its_artists_picture(self, music):
        assert music.read_track_pictures(["3231"]) == {
            "3231": music.read_artist(BEATLES).picture
        }

    def test_a_track_asked_twice_is_there_once(self, music):
        first = REVOLVER_TRACKS[0]

        assert list(music.read_track_pictures([first, first])) == [first]

    def test_tracks_plex_does_not_have_have_no_pictures(self, music):
        assert music.read_track_pictures(["9998", "9999"]) == {}

    def test_no_tracks_are_not_asked_for(self, music):
        music.reachable = False

        assert music.read_track_pictures([]) == {}


class TestPlexClientTrackPicture:
    def test_a_tracks_picture_is_its_albums(self, library):
        picture = library.read_track_picture(TRACKS[0])

        assert picture is not None
        assert picture.startswith(f"/library/metadata/{ALBUM}/thumb/")
        assert picture.removeprefix(f"/library/metadata/{ALBUM}/thumb/").isdigit()

    def test_the_tracks_of_an_album_share_one_picture(self, library):
        pictures = {library.read_track_picture(track) for track in TRACKS}

        assert len(pictures) == 1
        assert library.read_track_picture(OTHER_TRACK) not in pictures

    def test_a_new_picture_has_a_new_path(self, library):
        before = library.read_track_picture(TRACKS[0])

        library.set_picture(ALBUM, (1000, 1000), BLUE)

        after = library.read_track_picture(TRACKS[0])
        assert after != before
        assert after.startswith(f"/library/metadata/{ALBUM}/thumb/")

    def test_a_track_plex_does_not_have_has_no_picture(self, library):
        assert library.read_track_picture("9999") is None

    def test_a_track_of_an_album_without_a_cover_has_its_artists_picture(
        self, library
    ):
        library.add_artist("2301", "An Artist", size=(100, 100))
        library.add_album("2302", ["2303"], size=None, artist="2301")

        picture = library.read_track_picture("2303")

        assert picture == library.read_artist("2301").picture
        assert picture.startswith("/library/metadata/2301/thumb/")

    def test_a_track_of_an_album_and_an_artist_without_a_picture_has_none(
        self, library
    ):
        assert library.read_track_picture(BARE_TRACK) is None

    def test_an_album_is_not_a_track(self, library):
        assert library.read_track_picture(ALBUM) is None

    def test_a_plex_that_does_not_answer_fails(self, library):
        library.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.read_track_picture(TRACKS[0])

        assert raised.value.operation == "read the picture of Plex track 2002"
        assert raised.value.cause == "Plex did not answer (ConnectionError)"

    def test_the_plexapi_client_fails_without_the_token_or_the_address(
        self, unanswered
    ):
        with pytest.raises(ExternalServiceException) as raised:
            unanswered.read_track_picture(TRACKS[0])

        assert raised.value.message == (
            "Cannot read the picture of Plex track 2002 because external "
            "service failed: Plex did not answer (ConnectionError)"
        )

    @pytest.mark.parametrize("rating_key", ["", "abc", "12/../../identity", "1,2"])
    def test_the_plexapi_client_asks_plex_for_a_rating_key_only(
        self, unanswered, rating_key
    ):
        # Plex does not answer: a call that reached it would fail.
        assert unanswered.read_track_picture(rating_key) is None


class TestPlexClientPicture:
    def test_a_picture_is_a_jpeg_of_the_size_asked_for(self, library):
        picture = library.read_track_picture(TRACKS[0])

        image = _image(library.read_picture(picture, 400))

        assert image.size == (400, 400)
        _assert_colour(image, RED)

    def test_a_picture_that_is_not_square_covers_the_square(self, library):
        picture = library.read_track_picture(OTHER_TRACK)

        image = _image(library.read_picture(picture, 300))

        assert image.size == (500, 300)
        _assert_colour(image, BLUE)

    def test_a_small_picture_is_scaled_up(self, library):
        picture = library.read_track_picture(TRACKS[0])

        assert _image(library.read_picture(picture, 1024)).size == (1024, 1024)

    def test_another_stamp_answers_the_picture_as_it_is_now(self, library):
        before = library.read_track_picture(TRACKS[0])
        library.set_picture(ALBUM, (1000, 1000), BLUE)

        _assert_colour(_image(library.read_picture(before, 100)), BLUE)

    def test_a_test_gives_an_album_a_picture_of_its_own(self, library):
        before = library.read_track_picture(TRACKS[0])
        halves = Image.new("RGB", (400, 200), RED)
        halves.paste(BLUE, (200, 0, 400, 200))

        library.set_image(ALBUM, halves)

        picture = library.read_track_picture(TRACKS[0])
        assert picture != before
        image = _image(library.read_picture(picture, 100)).convert("RGB")
        assert image.size == (200, 100)
        for at, colour in (((50, 50), RED), ((150, 50), BLUE)):
            assert all(
                abs(got - want) <= 8
                for got, want in zip(image.getpixel(at), colour, strict=True)
            )

    def test_a_closed_fake_serves_no_picture(self, library):
        picture = library.read_track_picture(TRACKS[0])

        library.close()

        assert library.read_picture(picture, 100) is None

    def test_a_playlists_picture_is_read(self, music):
        picture = music.read_playlist(MIX).picture

        image = _image(music.read_picture(picture, 160))

        assert image.size == (160, 160)
        _assert_colour(image, BLUE)

    def test_a_playlists_new_picture_has_a_new_path(self, music):
        [before] = [p.picture for p in music.read_playlists() if p.rating_key == MIX]

        music.set_picture(MIX, (1000, 1000), RED)

        [after] = [p.picture for p in music.read_playlists() if p.rating_key == MIX]
        assert after != before
        _assert_colour(_image(music.read_picture(before, 100)), RED)

    def test_a_playlist_without_a_picture_has_none(self, music):
        assert music.read_picture(f"/playlists/{EMPTY}/composite/1", 100) is None
        assert music.read_picture("/playlists/9999/composite/1", 100) is None
        # A playlist's picture is not at an item's path.
        assert music.read_picture(f"/library/metadata/{MIX}/thumb/1", 100) is None

    def test_an_artist_without_a_picture_has_none(self, music):
        assert (
            music.read_picture(f"/library/metadata/{BARE_ARTIST}/thumb/1", 100) is None
        )

    def test_a_picture_is_set_on_an_item_that_has_one_only(self, music):
        with pytest.raises(KeyError):
            music.set_picture("3201", (10, 10), RED)

    def test_an_item_without_a_picture_has_none(self, library):
        assert library.read_picture(f"/library/metadata/{BARE_ALBUM}/thumb/1", 100) is None
        # A track has no picture of its own.
        assert library.read_picture(f"/library/metadata/{TRACKS[0]}/thumb/1", 100) is None
        assert library.read_picture("/library/metadata/9999/thumb/1", 100) is None

    @pytest.mark.parametrize("picture", NOT_A_PICTURE)
    def test_a_path_that_is_no_pictures_has_none(self, library, picture):
        assert library.read_picture(picture, 100) is None

    @pytest.mark.parametrize("picture", NOT_A_PICTURE)
    def test_the_plexapi_client_asks_plex_for_a_picture_path_only(
        self, unanswered, picture
    ):
        # Plex does not answer: a call that reached it would fail.
        assert unanswered.read_picture(picture, 100) is None

    def test_a_plex_that_does_not_answer_fails(self, library):
        picture = library.read_track_picture(TRACKS[0])
        library.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.read_picture(picture, 100)

        assert raised.value.operation == (
            f"read Plex's picture {picture} at 100 pixels"
        )
        assert raised.value.cause == "Plex did not answer (ConnectionError)"

    @pytest.mark.parametrize(
        "picture",
        [
            "/library/metadata/2001/thumb/1700000000",
            "/playlists/3301/composite/1700000000",
        ],
        ids=["thumb", "composite"],
    )
    def test_the_plexapi_client_fails_without_the_token_or_the_address(
        self, unanswered, picture
    ):
        # A path the client lets Plex fetch: it asks, and Plex does not answer.
        with pytest.raises(ExternalServiceException) as raised:
            unanswered.read_picture(picture, 100)

        assert raised.value.message == (
            f"Cannot read Plex's picture {picture} at 100 pixels because "
            "external service failed: Plex did not answer (ConnectionError)"
        )
