"""Tests for the reading of the library's items in Plex's answers.

The answers are Plex's as the real one gave them on 2026-11-16, cut to what
is read, with made-up titles, rating keys and guids.
"""

from datetime import date
from xml.etree.ElementTree import Element, fromstring

import pytest

from app.utils.plex_library import (
    PlexAlbum,
    PlexAnswerError,
    PlexArtist,
    PlexPlaylist,
    PlexTrack,
    in_release_order,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_rating_key,
    parse_track,
    track_picture,
)

ARTIST = (
    '<Directory ratingKey="3001" type="artist" '
    'guid="plex://artist/5d07bbfd403c6402904a0001" title="The Beatles" '
    'titleSort="Beatles" thumb="/library/metadata/3001/thumb/1700000001" />'
)
ALBUM = (
    '<Directory ratingKey="3101" parentRatingKey="3001" type="album" '
    'guid="plex://album/5d07c1d7403c640290870001" title="Revolver" '
    'parentTitle="The Beatles" year="1966" '
    'originallyAvailableAt="1966-08-05" leafCount="14" '
    'thumb="/library/metadata/3101/thumb/1700000002" />'
)
TRACK = (
    '<Track ratingKey="3201" parentRatingKey="3101" type="track" '
    'title="Taxman" grandparentTitle="The Beatles" parentTitle="Revolver" '
    'parentIndex="1" index="1" duration="159000" '
    'thumb="/library/metadata/3101/thumb/1700000002" '
    'grandparentThumb="/library/metadata/3001/thumb/1700000001" />'
)
PLAYLIST = (
    '<Playlist ratingKey="3301" type="playlist" '
    'guid="com.plexapp.agents.none://00000000-0000-4000-8000-000000003301" '
    'title="Mix" smart="0" playlistType="audio" leafCount="148" duration="37610000" '
    'composite="/playlists/3301/composite/1700000003" />'
)


def _item(xml: str, without: tuple[str, ...] = (), **changed: str) -> Element:
    """An item of an answer, with attributes left out or changed."""
    item = fromstring(xml)
    for name in without:
        del item.attrib[name]
    item.attrib.update(changed)
    return item


def _album(rating_key: str, title: str, released: date | None) -> PlexAlbum:
    return PlexAlbum(
        rating_key=rating_key,
        guid=f"plex://album/{rating_key}",
        title=title,
        artist_rating_key="3001",
        artist="The Beatles",
        released=released,
        track_count=1,
        picture=None,
    )


class TestParseArtist:
    def test_an_artist(self):
        assert parse_artist(_item(ARTIST)) == PlexArtist(
            rating_key="3001",
            guid="plex://artist/5d07bbfd403c6402904a0001",
            title="The Beatles",
            sort_title="Beatles",
            picture="/library/metadata/3001/thumb/1700000001",
        )

    def test_an_artist_without_a_sort_title_files_under_its_title(self):
        artist = parse_artist(_item(ARTIST, without=("titleSort",)))

        assert artist.sort_title == "The Beatles"

    def test_an_artist_without_a_picture_has_none(self):
        assert parse_artist(_item(ARTIST, without=("thumb",))).picture is None
        assert parse_artist(_item(ARTIST, thumb="")).picture is None

    def test_an_artist_plex_has_not_matched_has_a_local_guid(self):
        artist = parse_artist(_item(ARTIST, guid="local://3001"))

        assert artist.guid == "local://3001"

    @pytest.mark.parametrize("name", ["ratingKey", "guid", "title"])
    def test_an_artist_without_what_every_artist_has_is_no_answer(self, name):
        with pytest.raises(PlexAnswerError) as raised:
            parse_artist(_item(ARTIST, without=(name,)))

        assert str(raised.value) == f"Plex's answer has a Directory without {name}"


class TestParseAlbum:
    def test_an_album(self):
        album = parse_album(_item(ALBUM))

        assert album == PlexAlbum(
            rating_key="3101",
            guid="plex://album/5d07c1d7403c640290870001",
            title="Revolver",
            artist_rating_key="3001",
            artist="The Beatles",
            released=date(1966, 8, 5),
            track_count=14,
            picture="/library/metadata/3101/thumb/1700000002",
        )
        assert album.year == 1966

    def test_an_album_without_a_date_has_no_year(self):
        album = parse_album(_item(ALBUM, without=("originallyAvailableAt", "year")))

        assert album.released is None
        assert album.year is None

    def test_an_album_without_a_picture_has_none(self):
        assert parse_album(_item(ALBUM, without=("thumb",))).picture is None

    def test_an_album_plex_has_not_matched_has_a_local_guid(self):
        album = parse_album(_item(ALBUM, guid="local://3101"))

        assert album.guid == "local://3101"

    @pytest.mark.parametrize(
        "name",
        ["ratingKey", "guid", "title", "parentRatingKey", "parentTitle", "leafCount"],
    )
    def test_an_album_without_what_every_album_has_is_no_answer(self, name):
        with pytest.raises(PlexAnswerError) as raised:
            parse_album(_item(ALBUM, without=(name,)))

        assert str(raised.value) == f"Plex's answer has a Directory without {name}"

    def test_a_date_that_is_none_is_no_answer(self):
        with pytest.raises(PlexAnswerError) as raised:
            parse_album(_item(ALBUM, originallyAvailableAt="summer of 1966"))

        assert str(raised.value) == (
            "Plex's answer has a Directory whose originallyAvailableAt is not a date"
        )

    @pytest.mark.parametrize("count", ["", "many", "-1", "1.5", "١٤"])
    def test_a_track_count_that_is_no_number_is_no_answer(self, count):
        with pytest.raises(PlexAnswerError) as raised:
            parse_album(_item(ALBUM, leafCount=count))

        assert str(raised.value) == (
            "Plex's answer has a Directory whose leafCount is not a number"
        )


class TestParseTrack:
    def test_a_track(self):
        assert parse_track(_item(TRACK)) == PlexTrack(
            rating_key="3201",
            title="Taxman",
            artist="The Beatles",
            disc=1,
            number=1,
            duration_ms=159000,
            picture="/library/metadata/3101/thumb/1700000002",
        )

    def test_a_tracks_own_artist_goes_before_its_albums(self):
        track = parse_track(_item(TRACK, originalTitle="Tony Sheridan"))

        assert track.artist == "Tony Sheridan"

    def test_a_track_of_an_album_without_a_cover_has_its_artists_picture(self):
        # Plex gives such a track no thumb, and its album its artist's picture.
        track = parse_track(_item(TRACK, without=("thumb",)))

        assert track.picture == "/library/metadata/3001/thumb/1700000001"

    def test_a_track_without_a_disc_a_number_a_duration_or_a_picture_has_none(self):
        track = parse_track(
            _item(
                TRACK,
                without=("parentIndex", "index", "duration", "thumb", "grandparentThumb"),
            )
        )

        assert (track.disc, track.number, track.duration_ms, track.picture) == (
            None,
            None,
            None,
            None,
        )

    @pytest.mark.parametrize("name", ["ratingKey", "title", "grandparentTitle"])
    def test_a_track_without_what_every_track_has_is_no_answer(self, name):
        with pytest.raises(PlexAnswerError) as raised:
            parse_track(_item(TRACK, without=(name,)))

        assert str(raised.value) == f"Plex's answer has a Track without {name}"

    @pytest.mark.parametrize("name", ["parentIndex", "index", "duration"])
    def test_a_number_that_is_none_is_no_answer(self, name):
        with pytest.raises(PlexAnswerError) as raised:
            parse_track(_item(TRACK, **{name: "one"}))

        assert str(raised.value) == (
            f"Plex's answer has a Track whose {name} is not a number"
        )


class TestTrackPicture:
    def test_it_is_the_cover_of_the_tracks_album(self):
        assert track_picture(_item(TRACK)) == "/library/metadata/3101/thumb/1700000002"

    def test_a_track_of_an_album_without_a_cover_has_its_artists_picture(self):
        # Plex gives such a track no thumb, and its album its artist's picture.
        picture = track_picture(_item(TRACK, without=("thumb",)))

        assert picture == "/library/metadata/3001/thumb/1700000001"

    @pytest.mark.parametrize(
        "item",
        [
            _item(TRACK, without=("thumb", "grandparentThumb")),
            _item(TRACK, thumb="", grandparentThumb=""),
        ],
    )
    def test_a_track_whose_album_and_artist_have_no_picture_has_none(self, item):
        assert track_picture(item) is None


class TestParsePlaylist:
    def test_a_playlist(self):
        assert parse_playlist(_item(PLAYLIST)) == PlexPlaylist(
            rating_key="3301",
            guid="com.plexapp.agents.none://00000000-0000-4000-8000-000000003301",
            title="Mix",
            track_count=148,
            duration_ms=37610000,
            picture="/playlists/3301/composite/1700000003",
        )

    def test_a_playlist_without_tracks_has_no_duration_and_no_picture(self):
        playlist = parse_playlist(
            _item(PLAYLIST, without=("duration", "composite"), leafCount="0")
        )

        assert playlist.track_count == 0
        assert playlist.duration_ms is None
        assert playlist.picture is None

    @pytest.mark.parametrize("name", ["ratingKey", "guid", "title", "leafCount"])
    def test_a_playlist_without_what_every_playlist_has_is_no_answer(self, name):
        with pytest.raises(PlexAnswerError) as raised:
            parse_playlist(_item(PLAYLIST, without=(name,)))

        assert str(raised.value) == f"Plex's answer has a Playlist without {name}"


class TestParseRatingKey:
    def test_a_rating_key(self):
        assert parse_rating_key(_item(ARTIST), "ratingKey") == "3001"

    @pytest.mark.parametrize("key", ["", "abc", "30/01", "3001,3002", "٣٠٠١"])
    def test_a_rating_key_that_is_none_is_no_answer(self, key):
        with pytest.raises(PlexAnswerError) as raised:
            parse_rating_key(_item(ARTIST, ratingKey=key), "ratingKey")

        assert str(raised.value) == "Plex's answer has a Directory without ratingKey"


class TestInReleaseOrder:
    ALBUMS = [
        _album("3105", "Demos", None),
        _album("3101", "Revolver", date(1966, 8, 5)),
        _album("3104", "Bootleg", None),
        _album("3103", "anthology", date(1966, 8, 5)),
        _album("3102", "Help!", date(1965, 8, 6)),
        _album("3110", "Revolver", date(1966, 8, 5)),
        _album("399", "Revolver", date(1966, 8, 5)),
    ]

    def test_oldest_first_is_by_date_then_title_then_rating_key(self):
        albums = in_release_order(self.ALBUMS, newest_first=False)

        # 399 is before 3101 as a number, not as a text.
        assert [album.rating_key for album in albums] == [
            "3102",
            "3103",
            "399",
            "3101",
            "3110",
            "3104",
            "3105",
        ]

    def test_newest_first_is_the_reverse_with_the_undated_still_last(self):
        albums = in_release_order(self.ALBUMS, newest_first=True)

        assert [album.rating_key for album in albums] == [
            "3110",
            "3101",
            "399",
            "3103",
            "3102",
            "3105",
            "3104",
        ]

    def test_no_albums(self):
        assert in_release_order([], newest_first=True) == []
