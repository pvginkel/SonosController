"""Tests for the library service, on the in-memory Plex."""

import unicodedata
from datetime import date

import pytest
from flask import Flask

from app.core_exceptions import ExternalServiceException
from app.exceptions import ItemNotFoundException
from app.schemas.library_schema import (
    AlbumListQuerySchema,
    ArtistListQuerySchema,
    PlaylistListQuerySchema,
    TrackListQuerySchema,
)
from app.services.library_service import (
    LibraryAlbum,
    LibraryArtist,
    LibraryPlaylist,
    LibraryService,
    LibraryTrack,
)
from app.utils.artwork_id import plex_artwork_id
from tests.fakes.plex_client import FakePlexClient

BEATLES = "3001"
ABBA = "3002"
BJORK = "3003"
NO_ALBUMS = "3004"

REVOLVER = "3101"
HELP = "3102"
ANTHOLOGY = "3103"
BOOTLEG = "3104"
DEMOS = "3105"

REVOLVER_TRACKS = ("3201", "3202")

# "Björk" and "Homogénic" as Plex may give them: the letter, then its accent.
BJORK_DECOMPOSED = "Björk"
BJORK_COMPOSED = "Björk"
HOMOGENIC_DECOMPOSED = "Homogénic"
HOMOGENIC_COMPOSED = "Homogénic"

# What is no item of a kind: a key Plex does not have, and what is no key.
NO_KEYS = ["9999", "abba", "30 01", "-1"]


@pytest.fixture
def library(app: Flask) -> LibraryService:
    """The container's library service, on the test's Plex."""
    service: LibraryService = app.container.library_service()
    return service


@pytest.fixture
def beatles(plex: FakePlexClient) -> FakePlexClient:
    """The Beatles with five albums, two of them without a date, and ABBA
    without albums."""
    plex.add_artist(BEATLES, "The Beatles", sort_title="Beatles")
    plex.add_artist(NO_ALBUMS, "ABBA")
    plex.add_album(
        REVOLVER,
        REVOLVER_TRACKS,
        artist=BEATLES,
        title="Revolver",
        released=date(1966, 8, 5),
    )
    plex.add_album(
        HELP, ["3203"], artist=BEATLES, title="Help!", released=date(1965, 8, 6)
    )
    plex.add_album(
        ANTHOLOGY,
        ["3204", "3205", "3206"],
        artist=BEATLES,
        title="Anthology 1",
        released=date(1995, 11, 20),
    )
    plex.add_album(BOOTLEG, ["3207"], artist=BEATLES, title="Bootleg")
    plex.add_album(DEMOS, ["3208"], artist=BEATLES, title="demos", size=None)
    return plex


def artist_ids(library: LibraryService, **query: object) -> list[str]:
    artists, _ = library.list_artists(ArtistListQuerySchema.model_validate(query))
    return [artist.id for artist in artists]


def album_ids(library: LibraryService, artist_id: str, **query: object) -> list[str]:
    albums, _ = library.list_artist_albums(
        artist_id, AlbumListQuerySchema.model_validate(query)
    )
    return [album.id for album in albums]


def track_ids(library: LibraryService, album_id: str, **query: object) -> list[str]:
    tracks, _ = library.list_album_tracks(
        album_id, TrackListQuerySchema.model_validate(query)
    )
    return [track.id for track in tracks]


def playlist_ids(library: LibraryService, **query: object) -> list[str]:
    playlists, _ = library.list_playlists(
        PlaylistListQuerySchema.model_validate(query)
    )
    return [playlist.id for playlist in playlists]


class TestLibraryServiceListArtists:
    def test_an_artist_is_served_with_its_letter_and_its_picture(self, library, plex):
        plex.add_artist(BEATLES, "The Beatles", sort_title="Beatles")
        picture = plex.read_artist(BEATLES).picture

        artists, total = library.list_artists(ArtistListQuerySchema())

        assert artists == [
            LibraryArtist(
                id=BEATLES,
                title="The Beatles",
                index_letter="B",
                artwork_id=plex_artwork_id(picture),
            )
        ]
        assert total == 1

    def test_an_artist_without_a_picture_has_no_artwork_id(self, library, plex):
        plex.add_artist(ABBA, "ABBA", size=None)

        artists, _ = library.list_artists(ArtistListQuerySchema())

        assert artists == [
            LibraryArtist(id=ABBA, title="ABBA", index_letter="A", artwork_id=None)
        ]

    def test_an_empty_library_has_no_artists(self, library):
        assert library.list_artists(ArtistListQuerySchema()) == ([], 0)

    def test_they_come_by_sort_title_whatever_its_case_or_accents(
        self, library, plex
    ):
        plex.add_artist("1", "Pink")
        plex.add_artist("2", "Ólafur Arnalds")
        plex.add_artist("3", "nina simone")
        plex.add_artist("4", "The Beatles", sort_title="Beatles")
        plex.add_artist("5", "ABBA")
        plex.add_artist("6", "Zucchero")
        plex.add_artist("7", "Édith Piaf")

        assert artist_ids(library) == ["5", "4", "7", "3", "2", "1", "6"]

    def test_the_artists_under_no_letter_come_first_together(self, library, plex):
        plex.add_artist("1", "ABBA")
        plex.add_artist("2", "Сплин")
        plex.add_artist("3", "2Pac")
        plex.add_artist("4", "Zucchero")
        plex.add_artist("5", "{}")
        plex.add_artist("6", "!!!")

        ids = artist_ids(library)

        assert ids[:4] == ["6", "3", "5", "2"]
        assert ids[4:] == ["1", "4"]

    def test_every_letters_artists_are_together(self, library, plex):
        titles = [
            "Åsa",
            "Zucchero",
            "abba",
            "Øystein",
            "Air",
            "zz top",
            "Żywiołak",
            "10cc",
            "ABBA",
        ]
        for key, title in enumerate(titles, start=1):
            plex.add_artist(str(key), title)

        artists, _ = library.list_artists(ArtistListQuerySchema())

        letters = [artist.index_letter for artist in artists]
        assert letters == ["#", "#", "A", "A", "A", "A", "Z", "Z", "Z"]

    def test_a_letters_artists_are_together_where_its_folded_title_sorts_after_z(
        self, library, plex
    ):
        # The Turkish dotless ı files under I and sorts after z once folded.
        plex.add_artist("1", "Zucchero")
        plex.add_artist("2", "ıncı")
        plex.add_artist("3", "Ilse DeLange")
        plex.add_artist("4", "ABBA")

        artists, _ = library.list_artists(ArtistListQuerySchema())

        assert [artist.id for artist in artists] == ["4", "3", "2", "1"]
        assert [artist.index_letter for artist in artists] == ["A", "I", "I", "Z"]

    def test_artists_with_one_sort_title_come_by_rating_key(self, library, plex):
        plex.add_artist("10", "Nirvana")
        plex.add_artist("9", "Nirvana")
        plex.add_artist("100", "nirvana")
        plex.add_artist("11", "Nirvána")

        assert artist_ids(library) == ["9", "10", "11", "100"]
        assert artist_ids(library, sort_dir="desc") == ["100", "11", "10", "9"]

    def test_descending_is_the_same_order_backwards(self, library, plex):
        for key, title in enumerate(["Pink", "2Pac", "ABBA", "Édith Piaf"], 1):
            plex.add_artist(str(key), title)

        assert artist_ids(library) == ["2", "3", "4", "1"]
        assert artist_ids(library, sort_dir="desc") == ["1", "4", "3", "2"]

    def test_a_page_is_a_cut_of_the_one_order(self, library, plex):
        for key in range(1, 8):
            plex.add_artist(str(key), f"Artist {chr(ord('H') - key)}")

        assert artist_ids(library, per_page=3) == ["7", "6", "5"]
        assert artist_ids(library, per_page=3, page=2) == ["4", "3", "2"]
        assert artist_ids(library, per_page=3, page=3) == ["1"]
        assert artist_ids(library, per_page=3, page=4) == []
        assert library.list_artists(ArtistListQuerySchema(per_page=3, page=4))[1] == 7

    @pytest.mark.parametrize(
        ("sort_title", "letter"),
        [
            ("ABBA", "A"),
            ("abba", "A"),
            ("Zucchero", "Z"),
            ("Ólafur Arnalds", "O"),
            ("Ólafur Arnalds", "O"),
            ("édith piaf", "E"),
            ("Åsa", "A"),
            ("Ça ira", "C"),
            ("Ñu", "N"),
            ("Żywiołak", "Z"),
            ("ＡＢＣ", "A"),
            ("2Pac", "#"),
            ("!!!", "#"),
            ("...And You Will Know Us", "#"),
            (" Air", "#"),
            ("[unknown]", "#"),
            ("Øystein Sevåg", "#"),
            ("Æon", "#"),
            ("Сплин", "#"),
            ("Αλκίνοος", "#"),
        ],
    )
    def test_the_letter_is_the_sort_titles_first_without_its_accent(
        self, library, plex, sort_title, letter
    ):
        plex.add_artist(BEATLES, "An artist", sort_title=sort_title)

        assert library.artist(BEATLES).index_letter == letter

    def test_the_letter_is_the_titles_where_plex_gives_no_sort_title(
        self, library, plex
    ):
        plex.add_artist(BEATLES, "The Beatles")

        assert library.artist(BEATLES).index_letter == "T"

    def test_a_title_is_nfc(self, library, plex):
        plex.add_artist(BJORK, BJORK_DECOMPOSED)

        ((artist,), _) = library.list_artists(ArtistListQuerySchema())

        assert artist.title == BJORK_COMPOSED
        assert unicodedata.is_normalized("NFC", artist.title)
        assert artist.index_letter == "B"

    def test_every_request_reads_plex(self, library, plex):
        plex.add_artist(ABBA, "ABBA")
        assert artist_ids(library) == [ABBA]

        plex.add_artist(BEATLES, "The Beatles")

        assert artist_ids(library) == [ABBA, BEATLES]

    def test_a_plex_that_does_not_answer_fails(self, library, plex):
        plex.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.list_artists(ArtistListQuerySchema())

        assert raised.value.message == (
            "Cannot read the artists of Plex's music library because external "
            "service failed: Plex did not answer (ConnectionError)"
        )

    def test_a_plex_without_the_music_library_fails(self, library, plex):
        plex.has_library = False

        with pytest.raises(ExternalServiceException, match="has no music library"):
            library.list_artists(ArtistListQuerySchema())


class TestLibraryServiceArtist:
    def test_one_artist(self, library, plex):
        plex.add_artist(ABBA, "ABBA")
        plex.add_artist(BJORK, BJORK_DECOMPOSED, sort_title="Bjork", size=None)

        assert library.artist(BJORK) == LibraryArtist(
            id=BJORK, title=BJORK_COMPOSED, index_letter="B", artwork_id=None
        )
        assert library.artist(ABBA).artwork_id == plex_artwork_id(
            plex.read_artist(ABBA).picture
        )

    @pytest.mark.parametrize("artist_id", [*NO_KEYS, REVOLVER, REVOLVER_TRACKS[0]])
    def test_an_id_that_is_no_artists_is_not_found(self, library, beatles, artist_id):
        with pytest.raises(ItemNotFoundException) as raised:
            library.artist(artist_id)

        assert raised.value.message == f"Artist {artist_id} was not found"
        assert raised.value.error_code == "ITEM_NOT_FOUND"

    def test_a_plex_that_does_not_answer_fails(self, library, beatles):
        beatles.reachable = False

        with pytest.raises(ExternalServiceException, match="Plex did not answer"):
            library.artist(BEATLES)


class TestLibraryServiceListArtistAlbums:
    def test_an_album_is_served_with_its_artist_its_year_and_its_picture(
        self, library, beatles
    ):
        picture = beatles.read_album(REVOLVER).picture

        albums, total = library.list_artist_albums(
            BEATLES, AlbumListQuerySchema(per_page=1, page=2)
        )

        assert albums == [
            LibraryAlbum(
                id=REVOLVER,
                title="Revolver",
                artist_id=BEATLES,
                artist="The Beatles",
                year=1966,
                track_count=2,
                artwork_id=plex_artwork_id(picture),
            )
        ]
        assert total == 5

    def test_the_newest_is_first_unless_asked_otherwise(self, library, beatles):
        assert album_ids(library, BEATLES) == [
            ANTHOLOGY,
            REVOLVER,
            HELP,
            DEMOS,
            BOOTLEG,
        ]

    def test_ascending_by_release_is_the_oldest_first(self, library, beatles):
        assert album_ids(library, BEATLES, sort_dir="asc") == [
            HELP,
            REVOLVER,
            ANTHOLOGY,
            BOOTLEG,
            DEMOS,
        ]

    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_by_release_is_the_order_the_plex_client_gives(
        self, library, beatles, sort_dir
    ):
        beatles.add_album(
            "3110", [], artist=BEATLES, title="Revolver", released=date(1966, 8, 5)
        )
        beatles.add_album(
            "3109", [], artist=BEATLES, title="A day", released=date(1966, 8, 5)
        )
        in_plex_order = beatles.read_artist_albums(
            BEATLES, newest_first=sort_dir == "desc"
        )

        ids = album_ids(library, BEATLES, sort_by="release", sort_dir=sort_dir)

        assert ids == [album.rating_key for album in in_plex_order]

    def test_by_title_whatever_its_case_or_accents(self, library, beatles):
        beatles.add_album("3111", [], artist=BEATLES, title="Écho")

        assert album_ids(library, BEATLES, sort_by="title", sort_dir="asc") == [
            ANTHOLOGY,
            BOOTLEG,
            DEMOS,
            "3111",
            HELP,
            REVOLVER,
        ]
        assert album_ids(library, BEATLES, sort_by="title", sort_dir="desc") == [
            REVOLVER,
            HELP,
            "3111",
            DEMOS,
            BOOTLEG,
            ANTHOLOGY,
        ]

    def test_by_title_is_descending_unless_asked_otherwise(self, library, beatles):
        assert album_ids(library, BEATLES, sort_by="title")[0] == REVOLVER

    def test_albums_with_one_title_come_by_rating_key(self, library, plex):
        plex.add_artist(BEATLES, "The Beatles")
        for key in ("3120", "3119", "31100"):
            plex.add_album(key, [], artist=BEATLES, title="Greatest Hits")

        by_title = {"sort_by": "title"}
        assert album_ids(library, BEATLES, sort_dir="asc", **by_title) == [
            "3119",
            "3120",
            "31100",
        ]
        assert album_ids(library, BEATLES, sort_dir="desc", **by_title) == [
            "31100",
            "3120",
            "3119",
        ]

    def test_a_page_is_a_cut_of_the_one_order(self, library, beatles):
        assert album_ids(library, BEATLES, per_page=2) == [ANTHOLOGY, REVOLVER]
        assert album_ids(library, BEATLES, per_page=2, page=2) == [HELP, DEMOS]
        assert album_ids(library, BEATLES, per_page=2, page=3) == [BOOTLEG]
        assert album_ids(library, BEATLES, per_page=2, page=4) == []

    def test_an_album_without_a_date_has_no_year(self, library, beatles):
        albums, _ = library.list_artist_albums(BEATLES, AlbumListQuerySchema())

        years = {album.id: album.year for album in albums}
        assert years == {
            ANTHOLOGY: 1995,
            REVOLVER: 1966,
            HELP: 1965,
            DEMOS: None,
            BOOTLEG: None,
        }

    def test_an_album_without_a_cover_has_its_artists_artwork_id(
        self, library, beatles
    ):
        albums, _ = library.list_artist_albums(BEATLES, AlbumListQuerySchema())

        artists = plex_artwork_id(beatles.read_artist(BEATLES).picture)
        assert [album.id for album in albums if album.artwork_id == artists] == [DEMOS]

    def test_the_titles_are_nfc(self, library, plex):
        plex.add_artist(BJORK, BJORK_DECOMPOSED)
        plex.add_album("3130", [], artist=BJORK, title=HOMOGENIC_DECOMPOSED)

        ((album,), _) = library.list_artist_albums(BJORK, AlbumListQuerySchema())

        assert (album.title, album.artist) == (HOMOGENIC_COMPOSED, BJORK_COMPOSED)

    def test_an_artist_without_albums_has_none(self, library, beatles):
        assert library.list_artist_albums(NO_ALBUMS, AlbumListQuerySchema()) == ([], 0)

    @pytest.mark.parametrize("artist_id", [*NO_KEYS, REVOLVER])
    def test_an_id_that_is_no_artists_is_not_found(self, library, beatles, artist_id):
        with pytest.raises(ItemNotFoundException) as raised:
            library.list_artist_albums(artist_id, AlbumListQuerySchema())

        assert raised.value.message == f"Artist {artist_id} was not found"

    def test_a_plex_that_does_not_answer_fails(self, library, beatles):
        beatles.reachable = False

        with pytest.raises(ExternalServiceException, match="Plex did not answer"):
            library.list_artist_albums(BEATLES, AlbumListQuerySchema())


class TestLibraryServiceAlbum:
    def test_one_album(self, library, beatles):
        assert library.album(HELP) == LibraryAlbum(
            id=HELP,
            title="Help!",
            artist_id=BEATLES,
            artist="The Beatles",
            year=1965,
            track_count=1,
            artwork_id=plex_artwork_id(beatles.read_album(HELP).picture),
        )

    def test_an_album_without_a_date_or_a_cover(self, library, beatles):
        album = library.album(DEMOS)

        artists = plex_artwork_id(beatles.read_artist(BEATLES).picture)
        assert (album.year, album.artwork_id) == (None, artists)

    def test_the_titles_are_nfc(self, library, plex):
        plex.add_artist(BJORK, BJORK_DECOMPOSED)
        plex.add_album("3130", [], artist=BJORK, title=HOMOGENIC_DECOMPOSED)

        album = library.album("3130")

        assert (album.title, album.artist) == (HOMOGENIC_COMPOSED, BJORK_COMPOSED)

    @pytest.mark.parametrize("album_id", [*NO_KEYS, BEATLES, REVOLVER_TRACKS[0]])
    def test_an_id_that_is_no_albums_is_not_found(self, library, beatles, album_id):
        with pytest.raises(ItemNotFoundException) as raised:
            library.album(album_id)

        assert raised.value.message == f"Album {album_id} was not found"
        assert raised.value.error_code == "ITEM_NOT_FOUND"

    def test_a_plex_that_does_not_answer_fails(self, library, beatles):
        beatles.reachable = False

        with pytest.raises(ExternalServiceException, match="Plex did not answer"):
            library.album(HELP)


class TestLibraryServiceListAlbumTracks:
    @pytest.fixture
    def double_album(self, plex: FakePlexClient) -> FakePlexClient:
        """An album of two discs, added out of order, with a track that has
        an artist of its own and one Plex gives no disc, number or length."""
        plex.add_artist(BEATLES, "The Beatles")
        plex.add_album(ANTHOLOGY, [], artist=BEATLES, title="Anthology 1")
        plex.add_track(ANTHOLOGY, "3215", disc=2, number=2)
        plex.add_track(ANTHOLOGY, "3212", disc=1, number=2, duration_ms=241_000)
        plex.add_track(
            ANTHOLOGY, "3214", disc=2, number=1, artist="Tony Sheridan"
        )
        plex.add_track(ANTHOLOGY, "3211", disc=1, number=1)
        plex.add_track(
            ANTHOLOGY, "3213", disc=None, number=None, duration_ms=None
        )
        return plex

    def test_a_track_is_served_with_its_place_its_length_and_its_albums_picture(
        self, library, double_album
    ):
        picture = double_album.read_album(ANTHOLOGY).picture

        tracks, total = library.list_album_tracks(
            ANTHOLOGY, TrackListQuerySchema(per_page=1, page=3)
        )

        assert tracks == [
            LibraryTrack(
                id="3212",
                title="Track 3212",
                artist="The Beatles",
                disc=1,
                number=2,
                duration_ms=241_000,
                artwork_id=plex_artwork_id(picture),
            )
        ]
        assert total == 5

    def test_they_come_by_disc_and_then_by_number(self, library, double_album):
        assert track_ids(library, ANTHOLOGY) == [
            "3213",
            "3211",
            "3212",
            "3214",
            "3215",
        ]
        assert track_ids(library, ANTHOLOGY, sort_dir="desc") == [
            "3215",
            "3214",
            "3212",
            "3211",
            "3213",
        ]

    def test_tracks_at_one_place_come_by_rating_key(self, library, plex):
        plex.add_album(ANTHOLOGY, [])
        for key in ("3220", "3219", "32100"):
            plex.add_track(ANTHOLOGY, key, disc=1, number=1)

        assert track_ids(library, ANTHOLOGY) == ["3219", "3220", "32100"]
        assert track_ids(library, ANTHOLOGY, sort_dir="desc") == [
            "32100",
            "3220",
            "3219",
        ]

    def test_a_page_is_a_cut_of_the_one_order(self, library, double_album):
        assert track_ids(library, ANTHOLOGY, per_page=2) == ["3213", "3211"]
        assert track_ids(library, ANTHOLOGY, per_page=2, page=3) == ["3215"]
        assert track_ids(library, ANTHOLOGY, per_page=2, page=4) == []

    def test_a_track_has_its_own_artist_where_plex_has_one(self, library, double_album):
        tracks, _ = library.list_album_tracks(ANTHOLOGY, TrackListQuerySchema())

        artists = {track.id: track.artist for track in tracks}
        assert artists["3214"] == "Tony Sheridan"
        assert artists["3215"] == "The Beatles"

    def test_a_track_without_a_disc_a_number_or_a_length(self, library, double_album):
        tracks, _ = library.list_album_tracks(ANTHOLOGY, TrackListQuerySchema())

        assert (tracks[0].disc, tracks[0].number, tracks[0].duration_ms) == (
            None,
            None,
            None,
        )

    def test_the_tracks_of_an_album_share_its_artwork_id(self, library, beatles):
        tracks, _ = library.list_album_tracks(REVOLVER, TrackListQuerySchema())

        assert {track.artwork_id for track in tracks} == {
            library.album(REVOLVER).artwork_id
        }

    def test_the_tracks_of_an_album_without_a_cover_have_no_artwork_id(
        self, library, beatles
    ):
        ((track,), _) = library.list_album_tracks(DEMOS, TrackListQuerySchema())

        assert track.artwork_id is None

    def test_the_titles_are_nfc(self, library, plex):
        plex.add_artist(BJORK, BJORK_DECOMPOSED)
        plex.add_album("3130", [], artist=BJORK)
        plex.add_track("3130", "3131", title="Jogá")
        plex.add_track(
            "3130", "3132", title="Bachelorette", artist="Sjón", number=2
        )

        tracks, _ = library.list_album_tracks("3130", TrackListQuerySchema())

        assert [(track.title, track.artist) for track in tracks] == [
            ("Jogá", BJORK_COMPOSED),
            ("Bachelorette", "Sjón"),
        ]

    def test_an_album_without_tracks_has_none(self, library, plex):
        plex.add_album(ANTHOLOGY, [])

        assert library.list_album_tracks(ANTHOLOGY, TrackListQuerySchema()) == ([], 0)

    @pytest.mark.parametrize("album_id", [*NO_KEYS, BEATLES, REVOLVER_TRACKS[0]])
    def test_an_id_that_is_no_albums_is_not_found(self, library, beatles, album_id):
        with pytest.raises(ItemNotFoundException) as raised:
            library.list_album_tracks(album_id, TrackListQuerySchema())

        assert raised.value.message == f"Album {album_id} was not found"

    def test_a_plex_that_does_not_answer_fails(self, library, beatles):
        beatles.reachable = False

        with pytest.raises(ExternalServiceException, match="Plex did not answer"):
            library.list_album_tracks(REVOLVER, TrackListQuerySchema())


class TestLibraryServiceListPlaylists:
    def test_a_playlist_is_served_with_its_size_and_its_picture(self, library, plex):
        plex.add_playlist("4001", "Road trip", 148)
        (listed,) = plex.read_playlists()

        playlists, total = library.list_playlists(PlaylistListQuerySchema())

        assert playlists == [
            LibraryPlaylist(
                id="4001",
                title="Road trip",
                track_count=148,
                duration_ms=listed.duration_ms,
                artwork_id=plex_artwork_id(listed.picture),
            )
        ]
        assert listed.duration_ms is not None
        assert total == 1

    def test_only_those_of_1_to_1000_tracks_are_listed(self, library, plex):
        plex.add_playlist("4001", "Empty", 0)
        plex.add_playlist("4002", "One", 1)
        plex.add_playlist("4003", "The most", 1000)
        plex.add_playlist("4004", "One too many", 1001)
        plex.add_playlist("4005", "All music", 12_019)

        playlists, total = library.list_playlists(PlaylistListQuerySchema())

        assert [playlist.id for playlist in playlists] == ["4002", "4003"]
        assert total == 2

    def test_the_most_tracks_listed_is_the_setting(self, plex):
        library = LibraryService(plex_client=plex, playlist_max_tracks=3)
        plex.add_playlist("4001", "Three", 3)
        plex.add_playlist("4002", "Four", 4)

        assert playlist_ids(library) == ["4001"]

    def test_listing_reads_no_playlist_on_its_own(self, library, plex):
        plex.add_playlist("4001", "Road trip", 148)
        plex.add_playlist("4002", "Dinner", 12)

        first, _ = library.list_playlists(PlaylistListQuerySchema())
        second, _ = library.list_playlists(PlaylistListQuerySchema())

        # Plex gives a playlist's picture a new path whenever the playlist is
        # read on its own.
        assert first == second

    def test_they_come_by_title_whatever_its_case_or_accents(self, library, plex):
        plex.add_playlist("4001", "road trip", 10)
        plex.add_playlist("4002", "Été", 10)
        plex.add_playlist("4003", "Dinner", 10)
        plex.add_playlist("4004", "Zen", 10)
        plex.add_playlist("4005", "favourites", 10)

        assert playlist_ids(library) == ["4003", "4002", "4005", "4001", "4004"]
        assert playlist_ids(library, sort_dir="desc") == [
            "4004",
            "4001",
            "4005",
            "4002",
            "4003",
        ]

    def test_playlists_with_one_title_come_by_rating_key(self, library, plex):
        for key in ("4010", "4009", "40100"):
            plex.add_playlist(key, "Mix", 10)

        assert playlist_ids(library) == ["4009", "4010", "40100"]
        assert playlist_ids(library, sort_dir="desc") == ["40100", "4010", "4009"]

    def test_a_page_is_a_cut_of_the_listed_playlists(self, library, plex):
        for key in range(1, 6):
            plex.add_playlist(f"400{key}", f"Mix {key}", 10)
        plex.add_playlist("4000", "Mix 0", 0)

        assert playlist_ids(library, per_page=2) == ["4001", "4002"]
        assert playlist_ids(library, per_page=2, page=3) == ["4005"]
        assert playlist_ids(library, per_page=2, page=4) == []
        assert library.list_playlists(PlaylistListQuerySchema(per_page=2))[1] == 5

    def test_a_title_is_nfc(self, library, plex):
        plex.add_playlist("4001", "Été", 10)

        ((playlist,), _) = library.list_playlists(PlaylistListQuerySchema())

        assert playlist.title == "Été"

    def test_no_playlists(self, library):
        assert library.list_playlists(PlaylistListQuerySchema()) == ([], 0)

    def test_a_plex_that_does_not_answer_fails(self, library, plex):
        plex.reachable = False

        with pytest.raises(ExternalServiceException) as raised:
            library.list_playlists(PlaylistListQuerySchema())

        assert raised.value.message == (
            "Cannot read Plex's playlists because external service failed: "
            "Plex did not answer (ConnectionError)"
        )


class TestLibraryServicePlaylist:
    def test_one_playlist(self, library, plex):
        plex.add_playlist("4001", "Été", 148)

        playlist = library.playlist("4001")

        (listed,) = plex.read_playlists()
        assert playlist == LibraryPlaylist(
            id="4001",
            title="Été",
            track_count=148,
            duration_ms=listed.duration_ms,
            artwork_id=plex_artwork_id(listed.picture),
        )

    @pytest.mark.parametrize("track_count", [0, 1001, 12_019])
    def test_a_playlist_the_list_leaves_out_is_served(
        self, library, plex, track_count
    ):
        plex.add_playlist("4001", "Not listed", track_count)

        assert library.list_playlists(PlaylistListQuerySchema()) == ([], 0)
        assert library.playlist("4001").track_count == track_count

    def test_a_playlist_without_tracks_has_no_length_and_no_picture(
        self, library, plex
    ):
        plex.add_playlist("4001", "Empty", 0)

        playlist = library.playlist("4001")

        assert (playlist.duration_ms, playlist.artwork_id) == (None, None)

    def test_every_read_gives_its_picture_a_new_id_which_the_list_then_carries(
        self, library, plex
    ):
        plex.add_playlist("4001", "Road trip", 148)

        first = library.playlist("4001").artwork_id
        second = library.playlist("4001").artwork_id

        assert first != second
        ((listed,), _) = library.list_playlists(PlaylistListQuerySchema())
        assert listed.artwork_id == second

    @pytest.mark.parametrize("playlist_id", [*NO_KEYS, BEATLES, REVOLVER])
    def test_an_id_that_is_no_playlists_is_not_found(
        self, library, beatles, playlist_id
    ):
        beatles.add_playlist("4001", "Road trip", 148)

        with pytest.raises(ItemNotFoundException) as raised:
            library.playlist(playlist_id)

        assert raised.value.message == f"Playlist {playlist_id} was not found"
        assert raised.value.error_code == "ITEM_NOT_FOUND"

    def test_a_plex_that_does_not_answer_fails(self, library, plex):
        plex.add_playlist("4001", "Road trip", 148)
        plex.reachable = False

        with pytest.raises(ExternalServiceException, match="Plex did not answer"):
            library.playlist("4001")
