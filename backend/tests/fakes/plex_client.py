"""The in-memory Plex pytest runs against.

``FakePlexClient`` keeps a small library a test builds: artists, their
albums with their tracks, and playlists. It answers ``PlexClient``'s calls
from it the way the real Plex was seen to (``docs/api-design.md`` sections
1.4, 1.6 and 1.9, and the live suite): an artist without a sort title files
under its title; an album's tracks come in disc and track order; an album
without a cover carries its artist's picture; a track's picture is the one
its album carries, at a path that carries the rating key of the album, or of
its artist, and the picture's stamp; a playlist's picture is at a path of
its own, which gets a new stamp whenever the playlist is read on its own,
and a playlist without tracks has no picture and no duration; a picture
comes back as a JPEG that covers the square asked for and keeps its shape;
a path with another stamp answers the picture as it is now. Every artist,
album and playlist has a guid of its own unless a test gives it one: two
albums may share one, as the same release held twice does, and an item Plex
has not matched has ``local://<rating key>``. The read of several items, and
the finds by guid and by title, read a playlist as the list does, its
picture's stamp left as it is. Like plexapi, it holds which server it is from
Plex's first answer.

A test builds its library with ``add_artist()``, ``add_album()``,
``add_track()`` and ``add_playlist()``, and with ``move()`` has Plex see an
item as new, as after a rescan found its files moved. ``served_tracks()`` is what the Plex
service linked in Sonos gives the fake household for a container of it.
``close()`` drops the library and its pictures, which the ``plex`` fixture
does after every test.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from PIL import Image

from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckResult, CheckStatus, HealthService
from app.services.plex_client import PlexClient
from app.utils.plex_library import (
    PlexAlbum,
    PlexArtist,
    PlexPlaylist,
    PlexTrack,
    in_release_order,
)

_KIND_NOUN: dict[type[PlexArtist | PlexAlbum | PlexPlaylist], str] = {
    PlexArtist: "artists",
    PlexAlbum: "albums",
    PlexPlaylist: "playlists",
}

_THUMB = re.compile(r"/library/metadata/([0-9]+)/thumb/[0-9]+")
_COMPOSITE = re.compile(r"/playlists/([0-9]+)/composite/[0-9]+")

_UNREACHABLE = "Plex did not answer (ConnectionError)"
_NO_LIBRARY = "Plex has no music library named 'Music'"

# The server's machine identifier, which the fake household's Plex queue
# entries carry.
MACHINE_IDENTIFIER = "fakeserver"

# The music library's section id.
SECTION_ID = "5"

# The artist of an album added without one.
ARTIST = "1"

_TRACK_MS = 180_000


@dataclass
class _Artist:
    rating_key: str
    guid: str
    title: str
    sort_title: str | None


@dataclass
class _Album:
    rating_key: str
    guid: str
    artist: str
    title: str
    released: date | None


@dataclass
class _Track:
    rating_key: str
    album: str
    title: str
    artist: str | None
    disc: int | None
    number: int | None
    duration_ms: int | None


@dataclass
class _Playlist:
    rating_key: str
    guid: str
    title: str
    track_count: int
    duration_ms: int | None
    # The tracks the Plex service queues for it; None for one a test gave a
    # count alone.
    track_keys: list[str] | None


@dataclass(frozen=True)
class ServedTrack:
    """A track as the Plex service linked in Sonos gives it to a speaker."""

    rating_key: str
    title: str
    artist: str
    album: str
    duration_ms: int | None


@dataclass
class _Picture:
    image: Image.Image
    stamp: int


class FakePlexClient(PlexClient):
    def __init__(self, health_service: HealthService) -> None:
        super().__init__(health_service)
        self._artists: dict[str, _Artist] = {}
        self._albums: dict[str, _Album] = {}
        self._tracks: dict[str, _Track] = {}
        self._playlists: dict[str, _Playlist] = {}
        # By the rating key of an artist, an album or a playlist.
        self._pictures: dict[str, _Picture] = {}
        self._next_stamp = 1700000000
        # False: Plex does not answer, as when its pod is down.
        self.reachable = True
        # What Plex was asked, in order: one operation per request, also a
        # request Plex did not answer.
        self.requests: list[str] = []
        # False: Plex has no music library of the name the settings give.
        self.has_library = True
        # plexapi reads the server at the first call that needs it, and
        # holds it from then on.
        self._server_read = False

    # ------------------------------------------------------------------
    # Building a library
    # ------------------------------------------------------------------

    def add_artist(
        self,
        rating_key: str,
        title: str,
        sort_title: str | None = None,
        size: tuple[int, int] | None = (1000, 1000),
        colour: tuple[int, int, int] = (30, 120, 60),
        *,
        guid: str | None = None,
    ) -> None:
        """Add an artist. ``sort_title=None`` is an artist Plex gives no sort
        title, and ``size=None`` one without a picture. Without ``guid`` it
        has one of its own."""
        self._artists[rating_key] = _Artist(
            rating_key, guid or f"plex://artist/{rating_key}", title, sort_title
        )
        if size is not None:
            self.set_picture(rating_key, size, colour)

    def add_album(
        self,
        rating_key: str,
        track_keys: Iterable[str],
        size: tuple[int, int] | None = (1000, 1000),
        colour: tuple[int, int, int] = (200, 60, 30),
        *,
        artist: str = ARTIST,
        title: str | None = None,
        released: date | None = None,
        guid: str | None = None,
    ) -> None:
        """Add an album with its tracks, by their rating keys, on one disc in
        the order given. Its picture is ``size`` pixels of one colour;
        ``size=None`` is an album without a picture, and ``released=None``
        one without a date. Without ``artist`` it is the album of an artist
        the fake adds for it, ``ARTIST``, who has no picture. Without
        ``guid`` it has one of its own."""
        if artist == ARTIST and ARTIST not in self._artists:
            self.add_artist(ARTIST, "Artist 1", size=None)
        if artist not in self._artists:
            raise KeyError(f"The fake Plex has no artist {artist}")
        self._albums[rating_key] = _Album(
            rating_key,
            guid or f"plex://album/{rating_key}",
            artist,
            title or f"Album {rating_key}",
            released,
        )
        for number, track_key in enumerate(track_keys, start=1):
            self.add_track(rating_key, track_key, number=number)
        if size is not None:
            self.set_picture(rating_key, size, colour)

    def add_track(
        self,
        album: str,
        rating_key: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        disc: int | None = 1,
        number: int | None = 1,
        duration_ms: int | None = _TRACK_MS,
    ) -> None:
        """Add a track to an album. ``artist`` is the track's own, as on a
        compilation; ``disc``, ``number`` and ``duration_ms`` are None for a
        track Plex gives none."""
        if album not in self._albums:
            raise KeyError(f"The fake Plex has no album {album}")
        self._tracks[rating_key] = _Track(
            rating_key,
            album,
            title or f"Track {rating_key}",
            artist,
            disc,
            number,
            duration_ms,
        )

    def add_playlist(
        self,
        rating_key: str,
        title: str,
        track_count: int,
        size: tuple[int, int] = (1000, 1000),
        colour: tuple[int, int, int] = (90, 30, 160),
        *,
        track_keys: Iterable[str] | None = None,
        guid: str | None = None,
    ) -> None:
        """Add a playlist of so many tracks: the operator's or Plex's own,
        which nothing tells apart. One without tracks has no picture and no
        duration. Without ``guid`` it has one of its own.

        ``track_keys`` are its tracks, in its own order, as many as
        ``track_count``. A playlist given a count of tracks alone cannot be
        queued on the fake household.
        """
        keys = None if track_keys is None else list(track_keys)
        if keys is not None and len(keys) != track_count:
            raise ValueError(f"Playlist {rating_key} has {len(keys)} track keys")
        self._playlists[rating_key] = _Playlist(
            rating_key,
            guid or f"com.plexapp.agents.none://{rating_key}",
            title,
            track_count,
            track_count * _TRACK_MS or None,
            keys,
        )
        if track_count:
            self.set_picture(rating_key, size, colour)

    def move(
        self,
        rating_key: str,
        to: str,
        *,
        guid: str | None = None,
        title: str | None = None,
    ) -> None:
        """Have Plex see an artist, an album or a playlist as new: it is at
        the rating key ``to``, with its guid and its title, or ``guid`` and
        ``title`` where Plex gives it others (``local://<to>`` for an item
        Plex has not matched). An artist's albums and an album's tracks go
        with it, and its picture, with a new stamp."""
        if to in self._artists or to in self._albums or to in self._playlists:
            raise KeyError(f"The fake Plex has an item {to} already")
        item: _Artist | _Album | _Playlist
        if rating_key in self._artists:
            item = self._artists.pop(rating_key)
            self._artists[to] = item
            for album in self._albums.values():
                if album.artist == rating_key:
                    album.artist = to
        elif rating_key in self._albums:
            item = self._albums.pop(rating_key)
            self._albums[to] = item
            for track in self._tracks.values():
                if track.album == rating_key:
                    track.album = to
        else:
            item = self._playlists.pop(rating_key)
            self._playlists[to] = item
        item.rating_key = to
        item.guid = guid or item.guid
        item.title = title or item.title
        held = self._pictures.pop(rating_key, None)
        if held is not None:
            self.set_image(to, held.image)

    def set_picture(
        self,
        rating_key: str,
        size: tuple[int, int],
        colour: tuple[int, int, int],
    ) -> None:
        """Give an artist, an album or a playlist another picture, of one
        colour: its path gets a new stamp."""
        self.set_image(rating_key, Image.new("RGB", size, colour))

    def set_image(self, rating_key: str, image: Image.Image) -> None:
        """Give an artist, an album or a playlist this picture: its path gets
        a new stamp."""
        if not any(
            rating_key in items
            for items in (self._artists, self._albums, self._playlists)
        ):
            raise KeyError(
                f"The fake Plex has no artist, album or playlist {rating_key}"
            )
        self._pictures[rating_key] = _Picture(image, self._next_stamp)
        self._next_stamp += 1

    def close(self) -> None:
        """Drop the library and its pictures. A finished test's fakes can
        outlive it: its household mirror and its fake household hold each
        other, and this fake with them, until a garbage collection, and a
        picture of the default size is megabytes."""
        self._artists.clear()
        self._albums.clear()
        self._tracks.clear()
        self._playlists.clear()
        self._pictures.clear()

    # ------------------------------------------------------------------
    # The Plex service linked in Sonos
    # ------------------------------------------------------------------

    def served_tracks(self, kind: str, rating_key: str) -> list[ServedTrack] | None:
        """The tracks the Plex service gives a speaker for a container of an
        item of the library (``track``, ``album``, ``artist`` or
        ``playlist``), in the order ``docs/api-design.md`` sections 1.3 and
        1.4 found: an album in disc and track order, an artist's albums
        newest first, a playlist in its own order. None for an item the
        library does not have."""
        if kind == "track":
            track = self._tracks.get(rating_key)
            return None if track is None else [self._served(track)]
        if kind == "album":
            if rating_key not in self._albums:
                return None
            return self._served_album(rating_key)
        if kind == "artist":
            if rating_key not in self._artists:
                return None
            albums = [
                self._album(album)
                for album in self._albums.values()
                if album.artist == rating_key
            ]
            return [
                served
                for album in in_release_order(albums, newest_first=True)
                for served in self._served_album(album.rating_key)
            ]
        if kind == "playlist":
            playlist = self._playlists.get(rating_key)
            if playlist is None:
                return None
            if playlist.track_keys is None:
                if playlist.track_count:
                    raise AssertionError(
                        f"The fake Plex's playlist {rating_key} has no tracks to "
                        "queue: add it with track_keys"
                    )
                return []
            return [self._served(self._tracks[key]) for key in playlist.track_keys]
        raise ValueError(f"The Plex service has no {kind} containers")

    def _served_album(self, rating_key: str) -> list[ServedTrack]:
        return [self._served(track) for track in self._album_tracks(rating_key)]

    def _album_tracks(self, rating_key: str) -> list[_Track]:
        """An album's tracks in disc and track order."""
        tracks = [track for track in self._tracks.values() if track.album == rating_key]
        tracks.sort(key=lambda track: (track.disc or 0, track.number or 0))
        return tracks

    def _served(self, track: _Track) -> ServedTrack:
        album = self._albums[track.album]
        return ServedTrack(
            rating_key=track.rating_key,
            title=track.title,
            artist=track.artist or self._artists[album.artist].title,
            album=album.title,
            duration_ms=track.duration_ms,
        )

    # ------------------------------------------------------------------
    # PlexClient
    # ------------------------------------------------------------------

    def check_readiness(self) -> CheckResult:
        if not self.reachable:
            return CheckResult(CheckStatus.ERROR, _UNREACHABLE)
        self._server_read = True
        if not self.has_library:
            return CheckResult(CheckStatus.ERROR, _NO_LIBRARY)
        return CheckResult(CheckStatus.OK)

    def machine_identifier(self) -> str:
        if not self._server_read:
            self._require_reachable("read which server Plex is")
            self._server_read = True
        return MACHINE_IDENTIFIER

    def section_id(self) -> str:
        self._asked("read Plex's music library", library=True)
        return SECTION_ID

    def read_artists(self) -> list[PlexArtist]:
        self._asked("read the artists of Plex's music library", library=True)
        artists = [self._artist(artist) for artist in self._artists.values()]
        return sorted(
            artists,
            key=lambda artist: (artist.sort_title.casefold(), int(artist.rating_key)),
        )

    def read_artist(self, rating_key: str) -> PlexArtist | None:
        self._asked(f"read Plex artist {rating_key}")
        artist = self._artists.get(rating_key)
        return self._artist(artist) if artist is not None else None

    def read_artist_albums(
        self, rating_key: str, newest_first: bool
    ) -> list[PlexAlbum] | None:
        self._asked(f"read the albums of Plex artist {rating_key}", library=True)
        if rating_key not in self._artists:
            return None
        albums = [
            self._album(album)
            for album in self._albums.values()
            if album.artist == rating_key
        ]
        return in_release_order(albums, newest_first)

    def read_album(self, rating_key: str) -> PlexAlbum | None:
        self._asked(f"read Plex album {rating_key}")
        album = self._albums.get(rating_key)
        return self._album(album) if album is not None else None

    def read_album_tracks(self, rating_key: str) -> list[PlexTrack] | None:
        self._asked(f"read the tracks of Plex album {rating_key}")
        if rating_key not in self._albums:
            return None
        return [self._track(track) for track in self._album_tracks(rating_key)]

    def read_track(self, rating_key: str) -> PlexTrack | None:
        self._asked(f"read Plex track {rating_key}")
        track = self._tracks.get(rating_key)
        return self._track(track) if track is not None else None

    def read_playlists(self) -> list[PlexPlaylist]:
        self._asked("read Plex's playlists")
        return [self._playlist(playlist) for playlist in self._playlists.values()]

    def read_playlist(self, rating_key: str) -> PlexPlaylist | None:
        self._asked(f"read Plex playlist {rating_key}")
        playlist = self._playlists.get(rating_key)
        if playlist is None:
            return None
        held = self._pictures.get(rating_key)
        if held is not None:
            held.stamp = self._next_stamp
            self._next_stamp += 1
        return self._playlist(playlist)

    def read_items(
        self, rating_keys: Iterable[str]
    ) -> dict[str, PlexArtist | PlexAlbum | PlexPlaylist]:
        keys = list(dict.fromkeys(rating_keys))
        if not keys:
            return {}
        self._asked(f"read {len(keys)} Plex items")
        items: dict[str, PlexArtist | PlexAlbum | PlexPlaylist] = {}
        for key in keys:
            if key in self._artists:
                items[key] = self._artist(self._artists[key])
            elif key in self._albums:
                items[key] = self._album(self._albums[key])
            elif key in self._playlists:
                items[key] = self._playlist(self._playlists[key])
        return items

    def find_by_guid(
        self, kind: type[PlexArtist | PlexAlbum | PlexPlaylist], guid: str
    ) -> list[PlexArtist | PlexAlbum | PlexPlaylist]:
        self._asked(
            f"find the Plex {_KIND_NOUN[kind]} with guid {guid}",
            library=kind is not PlexPlaylist,
        )
        return [item for item in self._all(kind) if item.guid == guid]

    def find_by_title(
        self, kind: type[PlexArtist | PlexAlbum | PlexPlaylist], title: str
    ) -> list[PlexArtist | PlexAlbum | PlexPlaylist]:
        self._asked(
            f"find the Plex {_KIND_NOUN[kind]} titled {title!r}",
            library=kind is not PlexPlaylist,
        )
        return [item for item in self._all(kind) if item.title == title]

    def read_track_picture(self, rating_key: str) -> str | None:
        self._asked(f"read the picture of Plex track {rating_key}")
        track = self._tracks.get(rating_key)
        return self._track_picture(track) if track is not None else None

    def read_track_pictures(self, rating_keys: Iterable[str]) -> dict[str, str]:
        keys = list(dict.fromkeys(rating_keys))
        if not keys:
            return {}
        self._asked(f"read the pictures of {len(keys)} Plex tracks")
        pictures = {}
        for key in keys:
            track = self._tracks.get(key)
            picture = self._track_picture(track) if track is not None else None
            if picture is not None:
                pictures[key] = picture
        return pictures

    def read_picture(self, picture: str, size: int) -> bytes | None:
        thumb = _THUMB.fullmatch(picture)
        composite = _COMPOSITE.fullmatch(picture)
        if thumb is not None and thumb.group(1) not in self._playlists:
            rating_key = thumb.group(1)
        elif composite is not None and composite.group(1) in self._playlists:
            rating_key = composite.group(1)
        else:
            return None
        self._require_reachable(f"read Plex's picture {picture} at {size} pixels")
        held = self._pictures.get(rating_key)
        if held is None:
            return None
        width, height = held.image.size
        factor = size / min(width, height)
        resized = held.image.resize(
            (max(size, round(width * factor)), max(size, round(height * factor)))
        )
        jpeg = io.BytesIO()
        resized.save(jpeg, "JPEG")
        return jpeg.getvalue()

    def _asked(self, operation: str, library: bool = False) -> None:
        """Plex is asked something that reads the server, and with
        ``library`` the music library's section."""
        self._require_reachable(operation)
        self._server_read = True
        if library and not self.has_library:
            raise ExternalServiceException(operation, _NO_LIBRARY)

    def _require_reachable(self, operation: str) -> None:
        """Plex is sent the request of ``operation``."""
        self.requests.append(operation)
        if not self.reachable:
            raise ExternalServiceException(operation, _UNREACHABLE)

    def _all(
        self, kind: type[PlexArtist | PlexAlbum | PlexPlaylist]
    ) -> list[PlexArtist | PlexAlbum | PlexPlaylist]:
        """Every item of a kind, in the order they were added."""
        if kind is PlexArtist:
            return [self._artist(artist) for artist in self._artists.values()]
        if kind is PlexAlbum:
            return [self._album(album) for album in self._albums.values()]
        return [self._playlist(playlist) for playlist in self._playlists.values()]

    def _picture_path(self, rating_key: str) -> str | None:
        held = self._pictures.get(rating_key)
        if held is None:
            return None
        if rating_key in self._playlists:
            return f"/playlists/{rating_key}/composite/{held.stamp}"
        return f"/library/metadata/{rating_key}/thumb/{held.stamp}"

    def _artist(self, artist: _Artist) -> PlexArtist:
        return PlexArtist(
            rating_key=artist.rating_key,
            guid=artist.guid,
            title=artist.title,
            sort_title=artist.sort_title or artist.title,
            picture=self._picture_path(artist.rating_key),
        )

    def _album(self, album: _Album) -> PlexAlbum:
        return PlexAlbum(
            rating_key=album.rating_key,
            guid=album.guid,
            title=album.title,
            artist_rating_key=album.artist,
            artist=self._artists[album.artist].title,
            released=album.released,
            track_count=sum(
                1 for track in self._tracks.values() if track.album == album.rating_key
            ),
            picture=self._album_picture(album),
        )

    def _album_picture(self, album: _Album) -> str | None:
        # Plex gives an album without a cover its artist's picture.
        return self._picture_path(album.rating_key) or self._picture_path(
            album.artist
        )

    def _track_picture(self, track: _Track) -> str | None:
        return self._album_picture(self._albums[track.album])

    def _track(self, track: _Track) -> PlexTrack:
        album = self._albums[track.album]
        return PlexTrack(
            rating_key=track.rating_key,
            title=track.title,
            artist=track.artist or self._artists[album.artist].title,
            disc=track.disc,
            number=track.number,
            duration_ms=track.duration_ms,
            picture=self._track_picture(track),
        )

    def _playlist(self, playlist: _Playlist) -> PlexPlaylist:
        return PlexPlaylist(
            rating_key=playlist.rating_key,
            guid=playlist.guid,
            title=playlist.title,
            track_count=playlist.track_count,
            duration_ms=playlist.duration_ms,
            picture=self._picture_path(playlist.rating_key),
        )
