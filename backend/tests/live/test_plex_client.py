"""The plexapi client against the real Plex: what the fake cannot prove.

Plex is only read. The token is asserted on through a flag, never in a
comparison pytest would print.
"""

import io
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from unittest import mock
from urllib.parse import urlsplit

import pytest
from PIL import Image

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckStatus
from app.services.plex_client import PlexapiPlexClient
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist

pytestmark = pytest.mark.live

_PICTURE = re.compile(r"/library/metadata/([0-9]+)/thumb/[0-9]+")
_COMPOSITE = re.compile(r"/playlists/([0-9]+)/composite/[0-9]+")

# The guid of an item Plex has matched, and of an item it has not
# (docs/api-design.md section 1.8).
_MATCHED = re.compile(r"plex://(artist|album)/[0-9a-f]+")
_LOCAL = re.compile(r"local://([0-9]+)")
_PLAYLIST_GUID = re.compile(r"com\.plexapp\.agents\.none://[0-9a-f-]+")

# A rating key far above any the library has given out.
NO_ITEM = "999999999"

REFUSED_TOKEN = "not-the-token"


@pytest.fixture(scope="module")
def picture(live_plex: PlexapiPlexClient, plex_track: str) -> str:
    """The path of the picture of that track: its album's cover."""
    path = live_plex.read_track_picture(plex_track)
    assert path is not None, "the track has no picture"
    return path


@pytest.fixture(scope="module")
def album(picture: str) -> str:
    """The rating key of the album of that track."""
    return _PICTURE.fullmatch(picture).group(1)


@pytest.fixture(scope="module")
def artists(live_plex: PlexapiPlexClient) -> list[PlexArtist]:
    found = live_plex.read_artists()
    assert found, "the music library has no artists"
    return found


@pytest.fixture(scope="module")
def artist(live_plex: PlexapiPlexClient, album: str) -> str:
    """The rating key of the artist of that album."""
    return live_plex.read_album(album).artist_rating_key


@pytest.fixture(scope="module")
def coverless(live_plex: PlexapiPlexClient, artists: list[PlexArtist]) -> PlexAlbum:
    """The first album, by artist, without a cover of its own, by an artist
    with a picture, and with tracks: Plex gives it its artist's picture, and
    its tracks no ``thumb`` but that picture as ``grandparentThumb``."""
    for found in artists:
        if found.picture is None:
            continue
        albums = live_plex.read_artist_albums(found.rating_key, newest_first=False)
        for album in albums or []:
            if album.picture == found.picture and album.track_count:
                return album
    pytest.fail("the music library has no album without a cover with tracks")


@pytest.fixture(scope="module")
def albums(live_plex: PlexapiPlexClient, artists: list[PlexArtist]) -> list[PlexAlbum]:
    """Every album of the music library, by artist."""
    return [
        found
        for one in artists
        for found in live_plex.read_artist_albums(one.rating_key, newest_first=False)
        or []
    ]


@pytest.fixture(scope="module")
def playlists(live_plex: PlexapiPlexClient) -> list[PlexPlaylist]:
    found = live_plex.read_playlists()
    assert any(playlist.track_count for playlist in found), (
        "Plex has no audio playlist with tracks"
    )
    return found


def _image(jpeg: bytes | None) -> Image.Image:
    assert jpeg is not None
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    return image


def _by_key(
    items: Iterable[PlexArtist | PlexAlbum | PlexPlaylist],
) -> list[PlexArtist | PlexAlbum | PlexPlaylist]:
    return sorted(items, key=lambda item: int(item.rating_key))


def _without_accents(title: str) -> str:
    return "".join(
        letter
        for letter in unicodedata.normalize("NFD", title)
        if not unicodedata.combining(letter)
    )


def _other(app, live_settings: AppSettings, **changed: str | None) -> PlexapiPlexClient:
    """A client on the real Plex with one setting changed."""
    return PlexapiPlexClient(
        settings=live_settings.model_copy(update=changed),
        health_service=app.container.health_service(),
    )


class TestPlexSettings:
    def test_the_token_is_present(self, live_settings):
        present = bool(live_settings.plex_token)

        assert present


class TestPlexClientReadiness:
    def test_plex_answers_and_has_the_library(self, plex):
        assert plex.check_readiness().status is CheckStatus.OK

    def test_without_a_library_named_plexs_one_music_library_passes(
        self, app, live_settings
    ):
        # PLEX_LIBRARY is optional when the server has one music library.
        client = _other(app, live_settings, plex_library=None)

        assert client.check_readiness().status is CheckStatus.OK

    def test_a_library_plex_does_not_have_is_reported(self, app, live_settings):
        client = _other(app, live_settings, plex_library="No such library")

        result = client.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "Plex has no music library named 'No such library'"

    def test_a_token_plex_refuses_is_reported(self, app, live_settings):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        result = client.check_readiness()

        assert result.status is CheckStatus.ERROR
        assert result.detail == "Plex refused the token"


class TestPlexClientMachineIdentifier:
    def test_it_is_the_one_the_garages_plex_queue_entries_carry(
        self, plex, plex_entry
    ):
        assert plex.machine_identifier() == plex_entry.machine_identifier


class TestPlexClientLibrary:
    def test_the_section_id_is_the_one_the_garages_plex_queue_entries_carry(
        self, plex, plex_entry
    ):
        assert plex.section_id() == plex_entry.section_id

    def test_without_a_library_named_it_is_plexs_one_music_library(
        self, app, live_settings, plex
    ):
        client = _other(app, live_settings, plex_library=None)

        assert client.section_id() == plex.section_id()

    def test_a_library_plex_does_not_have_fails(self, app, live_settings, artist):
        client = _other(app, live_settings, plex_library="No such library")

        for read in (
            client.section_id,
            client.read_artists,
            lambda: client.read_artist_albums(artist, newest_first=True),
        ):
            with pytest.raises(ExternalServiceException) as raised:
                read()

            assert raised.value.cause == (
                "Plex has no music library named 'No such library'"
            )

    def test_a_token_plex_refuses_fails_without_the_token_or_the_address(
        self, app, live_settings, artist, album, plex_track
    ):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        for read in (
            client.section_id,
            client.read_artists,
            lambda: client.read_artist(artist),
            lambda: client.read_artist_albums(artist, newest_first=True),
            lambda: client.read_album(album),
            lambda: client.read_album_tracks(album),
            lambda: client.read_track(plex_track),
            client.read_playlists,
            lambda: client.read_playlist(album),
            lambda: client.read_track_pictures([plex_track]),
            lambda: client.read_items([artist, album]),
        ):
            with pytest.raises(ExternalServiceException) as raised:
                read()

            assert raised.value.cause == "Plex refused the token"
            named = live_settings.plex_url in raised.value.message
            assert not named

    def test_every_artists_albums_and_tracks_are_read(self, plex, artists):
        # The whole library: what Plex leaves out of an item anywhere in it
        # is read as none, and every picture is one read_picture() asks for.
        albums: list[PlexAlbum] = []
        for found in artists:
            of_artist = plex.read_artist_albums(found.rating_key, newest_first=False)
            assert of_artist, "an artist of the library has no albums"
            assert all(a.artist_rating_key == found.rating_key for a in of_artist)
            first = of_artist[0]
            tracks = plex.read_album_tracks(first.rating_key)
            assert len(tracks) == first.track_count
            # An album without a cover carries its artist's picture, and its
            # tracks the picture it carries.
            cover = first.picture
            if cover and _PICTURE.fullmatch(cover).group(1) != first.rating_key:
                assert cover == found.picture
            assert all(track.picture == cover for track in tracks)
            albums += of_artist

        assert len({a.rating_key for a in albums}) == len(albums)
        pictures = [item.picture for item in [*artists, *albums] if item.picture]
        assert all(_PICTURE.fullmatch(path) for path in pictures)
        # Section 1.9 found items without a picture, and 1.4 albums without
        # a date: the library read here is the one they were found in.
        assert any(found.picture is None for found in artists)
        assert any(a.released is None for a in albums)
        # Every artist and album carries its guid, from the list too.
        for kind, items in (("artist", artists), ("album", albums)):
            for item in items:
                matched = _MATCHED.fullmatch(item.guid)
                local = _LOCAL.fullmatch(item.guid)
                assert (matched and matched.group(1) == kind) or (
                    local and local.group(1) == item.rating_key
                )
        # Section 1.8: a guid is not unique, the same release held twice.
        assert max(Counter(a.guid for a in albums).values()) > 1


class TestPlexClientArtists:
    def test_an_artist_is_listed_once_under_a_sort_title(self, artists):
        assert len({found.rating_key for found in artists}) == len(artists)
        assert all(found.title and found.sort_title for found in artists)
        # Plex gives most artists no sort title, and some one of their own.
        assert any(found.sort_title != found.title for found in artists)
        assert any(found.sort_title == found.title for found in artists)

    def test_one_artist_is_the_one_listed(self, plex, artists, artist):
        [listed] = [found for found in artists if found.rating_key == artist]

        assert plex.read_artist(artist) == listed

    def test_an_artists_picture_is_read(self, plex, artists):
        found = next(found for found in artists if found.picture)

        assert min(_image(plex.read_picture(found.picture, 160)).size) == 160

    def test_an_artist_plex_does_not_have_is_none(
        self, plex, album, plex_track, playlists
    ):
        for rating_key in (NO_ITEM, album, plex_track, playlists[0].rating_key):
            assert plex.read_artist(rating_key) is None


class TestPlexClientAlbums:
    def test_an_artists_albums_are_in_release_order(self, plex, artists):
        # The artist with the most albums of the first twenty.
        newest = max(
            (
                plex.read_artist_albums(found.rating_key, newest_first=True)
                for found in artists[:20]
            ),
            key=len,
        )
        oldest = plex.read_artist_albums(newest[0].artist_rating_key, newest_first=False)

        for albums, newest_first in ((newest, True), (oldest, False)):
            dates = [a.released for a in albums if a.released is not None]
            assert dates == sorted(dates, reverse=newest_first)
            dated = [a.released is not None for a in albums]
            assert dated == sorted(dated, reverse=True)
        assert len(newest) > 1
        assert sorted(a.rating_key for a in newest) == sorted(
            a.rating_key for a in oldest
        )

    def test_the_albums_without_a_date_are_last_in_either_direction(
        self, plex, artists
    ):
        # Plex itself puts them first when the oldest is first.
        for found in artists:
            oldest = plex.read_artist_albums(found.rating_key, newest_first=False)
            if len(oldest) > 1 and any(a.released is None for a in oldest):
                break
        else:
            pytest.fail("no artist has an album without a date beside another")
        newest = plex.read_artist_albums(found.rating_key, newest_first=True)

        for albums in (oldest, newest):
            assert albums[-1].released is None
            dated = [a.released is not None for a in albums]
            assert dated == sorted(dated, reverse=True)

    def test_one_album_is_the_one_listed_with_its_track_count(
        self, plex, artist, album
    ):
        albums = plex.read_artist_albums(artist, newest_first=True)
        [listed] = [a for a in albums if a.rating_key == album]

        found = plex.read_album(album)

        assert found == listed
        assert found.track_count == len(plex.read_album_tracks(album))
        assert found.year == (found.released.year if found.released else None)

    def test_an_album_plex_does_not_have_is_none(
        self, plex, artist, plex_track, playlists
    ):
        for rating_key in (NO_ITEM, artist, plex_track, playlists[0].rating_key):
            assert plex.read_album(rating_key) is None

    def test_the_albums_of_an_artist_plex_does_not_have_are_none(
        self, plex, album, plex_track, playlists
    ):
        for rating_key in (NO_ITEM, album, plex_track, playlists[0].rating_key):
            assert plex.read_artist_albums(rating_key, newest_first=True) is None


class TestPlexClientTracks:
    def test_an_albums_tracks_are_in_disc_and_track_order(
        self, plex, album, plex_track
    ):
        tracks = plex.read_album_tracks(album)

        assert plex_track in [track.rating_key for track in tracks]
        places = [(track.disc, track.number) for track in tracks]
        assert all(disc is not None and number is not None for disc, number in places)
        assert places == sorted(places)

    def test_one_track_is_the_one_listed_with_its_albums_picture(
        self, plex, album, plex_track, picture
    ):
        [listed] = [
            track
            for track in plex.read_album_tracks(album)
            if track.rating_key == plex_track
        ]

        found = plex.read_track(plex_track)

        assert found == listed
        assert found.picture == picture
        assert found.title and found.artist and found.duration_ms

    def test_the_tracks_of_an_album_without_a_cover_have_its_artists_picture(
        self, plex, coverless
    ):
        tracks = plex.read_album_tracks(coverless.rating_key)

        assert len(tracks) == coverless.track_count
        assert all(track.picture == coverless.picture for track in tracks)
        assert plex.read_track(tracks[0].rating_key) == tracks[0]
        artist = plex.read_artist(coverless.artist_rating_key)
        assert _PICTURE.fullmatch(coverless.picture).group(1) == artist.rating_key

    def test_the_tracks_of_an_album_plex_does_not_have_are_none(
        self, plex, artist, plex_track, playlists
    ):
        # Plex answers the children of an artist with its albums, and those
        # of a track with 400.
        for rating_key in (NO_ITEM, artist, plex_track, playlists[0].rating_key):
            assert plex.read_album_tracks(rating_key) is None

    def test_a_track_plex_does_not_have_is_none(self, plex, artist, album, playlists):
        for rating_key in (NO_ITEM, artist, album, playlists[0].rating_key):
            assert plex.read_track(rating_key) is None


class TestPlexClientPlaylists:
    def test_one_playlist_is_the_one_listed_with_a_new_stamp_on_its_picture(
        self, plex, playlists
    ):
        assert len({found.rating_key for found in playlists}) == len(playlists)
        read = {found.rating_key: plex.read_playlist(found.rating_key) for found in playlists}

        for found in playlists:
            one = read[found.rating_key]
            assert one.title == found.title
            if one.picture:
                assert _COMPOSITE.fullmatch(one.picture).group(1) == one.rating_key
                assert one.picture != found.picture
        # Plex stores what the read of a playlist on its own found: the list
        # has it from then on, a smart playlist's track count included.
        assert {found.rating_key: found for found in plex.read_playlists()} == read

    def test_every_playlist_has_a_guid_of_its_own(self, playlists):
        assert all(_PLAYLIST_GUID.fullmatch(found.guid) for found in playlists)
        assert len({found.guid for found in playlists}) == len(playlists)

    def test_a_playlist_with_tracks_has_a_duration_and_a_picture(
        self, plex, playlists
    ):
        found = next(found for found in playlists if found.track_count)

        assert found.duration_ms
        assert _COMPOSITE.fullmatch(found.picture).group(1) == found.rating_key
        assert min(_image(plex.read_picture(found.picture, 160)).size) == 160
        # Plex ignores a composite's stamp as it does a thumb's.
        another = f"/playlists/{found.rating_key}/composite/1"
        assert min(_image(plex.read_picture(another, 160)).size) == 160

    def test_a_playlist_without_tracks_has_no_duration_and_no_picture(
        self, plex, playlists
    ):
        # The playlists as they are now: the list has a smart playlist as
        # Plex stored it.
        read = [plex.read_playlist(found.rating_key) for found in playlists]
        empty = [found for found in read if not found.track_count]

        assert empty, "Plex has no audio playlist without tracks"
        for found in empty:
            assert found.duration_ms is None
            assert found.picture is None
            path = f"/playlists/{found.rating_key}/composite/1"
            assert plex.read_picture(path, 100) is None

    def test_a_playlist_plex_does_not_have_is_none(
        self, plex, artist, album, plex_track
    ):
        # Plex answers a track's rating key here with the track.
        for rating_key in (NO_ITEM, artist, album, plex_track):
            assert plex.read_playlist(rating_key) is None

    def test_a_playlists_picture_is_not_at_an_items_path(self, plex, playlists):
        found = next(found for found in playlists if found.track_count)

        path = f"/library/metadata/{found.rating_key}/thumb/1"
        assert plex.read_picture(path, 100) is None


class TestPlexClientItems:
    def test_an_artist_an_album_and_a_playlist_from_one_request(
        self, plex, artist, album
    ):
        # The playlists as they are now: the other tests read them on their
        # own, which gives their pictures new stamps.
        listed = {found.rating_key: found for found in plex.read_playlists()}
        playlist = next(found for found in listed.values() if found.picture)
        # The server is read at the first call that needs it.
        plex.machine_identifier()

        with mock.patch.object(
            plex._session, "send", wraps=plex._session.send
        ) as send:
            items = plex.read_items([artist, album, playlist.rating_key])

        # The path alone: the token is in a header, never compared.
        paths = [urlsplit(call.args[0].url).path for call in send.call_args_list]
        assert paths == [f"/library/metadata/{artist},{album},{playlist.rating_key}"]
        assert items == {
            artist: plex.read_artist(artist),
            album: plex.read_album(album),
            playlist.rating_key: playlist,
        }

    def test_a_playlist_is_the_one_listed_and_keeps_the_stamp_on_its_picture(
        self, plex
    ):
        listed = {found.rating_key: found for found in plex.read_playlists()}
        assert any(found.picture for found in listed.values())

        items = plex.read_items(list(listed))

        assert items == listed
        assert {found.rating_key: found for found in plex.read_playlists()} == listed

    def test_what_is_no_artist_album_or_playlist_is_left_out(
        self, plex, artist, plex_track
    ):
        items = plex.read_items([NO_ITEM, plex_track, artist])

        assert list(items) == [artist]

    def test_items_plex_does_not_have_are_left_out(self, plex):
        # Plex answers 404 when it has none of the rating keys.
        assert plex.read_items([NO_ITEM, "999999998"]) == {}


class TestPlexClientFind:
    def test_an_artist_an_album_and_a_playlist_are_found_by_guid(
        self, plex, artist, album
    ):
        one_artist = plex.read_artist(artist)
        one_album = plex.read_album(album)
        listed = {found.rating_key: found for found in plex.read_playlists()}
        playlist = next(found for found in listed.values() if found.picture)

        assert one_artist in plex.find_by_guid(PlexArtist, one_artist.guid)
        assert one_album in plex.find_by_guid(PlexAlbum, one_album.guid)
        assert plex.find_by_guid(PlexPlaylist, playlist.guid) == [playlist]
        # Found as the list gives it: the stamp on its picture is kept.
        assert {found.rating_key: found for found in plex.read_playlists()} == listed

    def test_every_album_with_a_guid_is_found_and_no_other(self, plex, albums):
        # Section 1.8: a guid is not unique, the same release held twice.
        guid, count = Counter(found.guid for found in albums).most_common(1)[0]
        assert count > 1, "the music library holds no release twice"

        found = plex.find_by_guid(PlexAlbum, guid)

        assert _by_key(found) == _by_key(a for a in albums if a.guid == guid)
        # Plex's filter alone gives the guids that contain the one asked.
        assert plex.find_by_guid(PlexAlbum, guid[:-1]) == []

    def test_artists_are_found_by_title_exactly_case_and_accents_included(
        self, plex, artists
    ):
        accented = next(
            (one for one in artists if _without_accents(one.title) != one.title),
            None,
        )
        assert accented is not None, "the music library has no artist with an accent"

        for title in (
            accented.title,
            accented.title.swapcase(),
            accented.title[:-1],
            _without_accents(accented.title),
        ):
            assert _by_key(plex.find_by_title(PlexArtist, title)) == _by_key(
                one for one in artists if one.title == title
            ), title
        assert accented in plex.find_by_title(PlexArtist, accented.title)

    def test_every_album_so_titled_is_found(self, plex, albums):
        title, count = Counter(found.title for found in albums).most_common(1)[0]
        assert count > 1, "the music library has no two albums of one title"
        # A title Plex's address would read otherwise, unless encoded.
        marked = next(
            (found.title for found in albums if set(found.title) & set("&?/#+%=")),
            None,
        )
        assert marked is not None, "the music library has no title with & ? / # + % ="
        # Plex reads a comma in a filter's value as "or".
        with_comma = next(
            (found.title for found in albums if "," in found.title), None
        )
        assert with_comma is not None, "the music library has no title with a comma"

        for asked in (title, marked, with_comma, title.swapcase()):
            assert _by_key(plex.find_by_title(PlexAlbum, asked)) == _by_key(
                found for found in albums if found.title == asked
            ), asked

    def test_a_playlist_is_found_by_title_as_the_list_gives_it(self, plex):
        listed = {found.rating_key: found for found in plex.read_playlists()}

        for playlist in listed.values():
            for asked in (playlist.title, playlist.title.swapcase()):
                assert _by_key(plex.find_by_title(PlexPlaylist, asked)) == _by_key(
                    found for found in listed.values() if found.title == asked
                ), asked
        assert {found.rating_key: found for found in plex.read_playlists()} == listed


class TestPlexClientTrackPictures:
    def test_the_pictures_of_an_albums_tracks_from_one_request(
        self, plex, album, picture
    ):
        keys = [track.rating_key for track in plex.read_album_tracks(album)]

        assert plex.read_track_pictures(keys) == dict.fromkeys(keys, picture)

    def test_the_tracks_of_an_album_without_a_cover_have_its_artists_picture(
        self, plex, coverless
    ):
        keys = [track.rating_key for track in plex.read_album_tracks(coverless.rating_key)]

        assert plex.read_track_pictures(keys) == dict.fromkeys(keys, coverless.picture)

    def test_what_is_no_track_is_left_out(
        self, plex, artist, album, plex_track, picture, playlists
    ):
        asked = [NO_ITEM, plex_track, album, artist, playlists[0].rating_key, plex_track]

        assert plex.read_track_pictures(asked) == {plex_track: picture}

    def test_tracks_plex_does_not_have_have_no_pictures(self, plex):
        # Plex answers 404 when it has none of the rating keys.
        assert plex.read_track_pictures([NO_ITEM, "999999998"]) == {}


class TestPlexClientTrackPicture:
    def test_a_tracks_picture_is_its_albums(self, plex_track, picture):
        path = _PICTURE.fullmatch(picture)

        assert path is not None
        # The album's rating key, not the track's.
        assert path.group(1) != plex_track

    def test_a_track_of_an_album_without_a_cover_has_its_artists_picture(
        self, plex, coverless
    ):
        [track, *_] = plex.read_album_tracks(coverless.rating_key)

        assert plex.read_track_picture(track.rating_key) == coverless.picture

    def test_a_track_plex_does_not_have_has_no_picture(self, plex):
        assert plex.read_track_picture(NO_ITEM) is None

    def test_an_album_is_not_a_track(self, plex, picture):
        album = _PICTURE.fullmatch(picture).group(1)

        assert plex.read_track_picture(album) is None

    def test_a_token_plex_refuses_fails_without_the_token_or_the_address(
        self, app, live_settings, plex_track
    ):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        with pytest.raises(ExternalServiceException) as raised:
            client.read_track_picture(plex_track)

        assert raised.value.cause == "Plex refused the token"
        named = live_settings.plex_url in raised.value.message
        assert not named


class TestPlexClientPicture:
    @pytest.mark.parametrize("size", [16, 400, 1024])
    def test_a_picture_is_a_jpeg_that_covers_the_square(self, plex, picture, size):
        image = _image(plex.read_picture(picture, size))

        assert min(image.size) == size

    def test_another_stamp_answers_the_picture_as_it_is_now(self, plex, picture):
        album = _PICTURE.fullmatch(picture).group(1)

        image = _image(plex.read_picture(f"/library/metadata/{album}/thumb/1", 100))

        assert min(image.size) == 100

    def test_an_item_without_a_picture_has_none(self, plex, plex_track):
        assert plex.read_picture(f"/library/metadata/{NO_ITEM}/thumb/1", 100) is None
        # A track has no picture of its own.
        assert plex.read_picture(f"/library/metadata/{plex_track}/thumb/1", 100) is None

    def test_a_token_plex_refuses_fails_without_the_token_or_the_address(
        self, app, live_settings, picture
    ):
        client = _other(app, live_settings, plex_token=REFUSED_TOKEN)

        with pytest.raises(ExternalServiceException) as raised:
            client.read_picture(picture, 100)

        assert raised.value.cause == "Plex refused the token"
        named = live_settings.plex_url in raised.value.message
        assert not named
