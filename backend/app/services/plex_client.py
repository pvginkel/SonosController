"""The Plex client: the backend's only way to Plex.

``PlexClient`` is what the rest of the backend sees: which server Plex is,
the music library's artists, albums, tracks and playlists, the picture Plex
gives an item, and that picture resized by Plex. ``PlexapiPlexClient``
implements it on plexapi, and this is the only module that imports plexapi.
pytest overrides the container's provider with an in-memory fake of the same
base class.

An item is addressed by its rating key, as a string. Every read is a request
to Plex: the client keeps nothing of the library. The token travels in a
request header and nowhere else: no address this module builds and no failure
it raises carries it, or Plex's address.
"""

import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from xml.etree.ElementTree import Element, ParseError

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.server import PlexServer

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckResult, CheckStatus, HealthService
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
)

_REQUEST_TIMEOUT_SECONDS = 5.0
# Plex takes half a second to refuse a token it has not seen before.
_READINESS_TIMEOUT_SECONDS = 2.0

_RATING_KEY = re.compile(r"[0-9]+")

# The path Plex gives a picture: an artist's or an album's rating key, or a
# playlist's, then a stamp.
_PICTURE = re.compile(
    r"/library/metadata/[0-9]+/thumb/[0-9]+|/playlists/[0-9]+/composite/[0-9]+"
)

_PLEX_ERRORS = (
    PlexApiException,
    requests.RequestException,
    ParseError,
    PlexAnswerError,
)


class PlexClient(ABC):
    """What the backend asks of Plex.

    Every method that reaches Plex raises ``ExternalServiceException`` when
    Plex fails, does not answer or refuses the token.
    """

    def __init__(self, health_service: HealthService) -> None:
        # Not critical: the app serves the speakers without Plex.
        health_service.register_check("plex", self.check_readiness, critical=False)

    @abstractmethod
    def check_readiness(self) -> CheckResult:
        """Readiness check: Plex answers, and has the music library."""

    @abstractmethod
    def machine_identifier(self) -> str:
        """The server's machine identifier, which the address of every queue
        entry of one of its tracks carries.

        The client holds it from Plex's first answer: it fails only while
        Plex has never answered.
        """

    @abstractmethod
    def section_id(self) -> str:
        """The section id of the music library, which the address of every
        item queued through the Plex service carries.

        The library is the one the settings name, or the server's one music
        library where they name none; a server without it fails.
        """

    @abstractmethod
    def read_artists(self) -> list[PlexArtist]:
        """The artists of the music library, in the order Plex files them."""

    @abstractmethod
    def read_artist(self, rating_key: str) -> PlexArtist | None:
        """One artist. None when Plex has no artist with this rating key."""

    @abstractmethod
    def read_artist_albums(
        self, rating_key: str, newest_first: bool
    ) -> list[PlexAlbum] | None:
        """An artist's albums, in release order.

        By release date, then title, then rating key, newest or oldest
        first; the albums without a date last, in either direction. None
        when Plex has no artist with this rating key.
        """

    @abstractmethod
    def read_album(self, rating_key: str) -> PlexAlbum | None:
        """One album. None when Plex has no album with this rating key."""

    @abstractmethod
    def read_album_tracks(self, rating_key: str) -> list[PlexTrack] | None:
        """An album's tracks, in Plex's disc and track order. None when Plex
        has no album with this rating key."""

    @abstractmethod
    def read_track(self, rating_key: str) -> PlexTrack | None:
        """One track. None when Plex has no track with this rating key."""

    @abstractmethod
    def read_playlists(self) -> list[PlexPlaylist]:
        """Plex's audio playlists, the empty ones and Plex's own included, in
        no order of this client's.

        Plex lists a smart playlist with the track count and the duration it
        stored when the playlist was last read on its own:
        ``read_playlist()`` has them as they are now.
        """

    @abstractmethod
    def read_playlist(self, rating_key: str) -> PlexPlaylist | None:
        """One audio playlist, as it is now. None when Plex has none with
        this rating key.

        Plex stamps a playlist's picture with the time the playlist was last
        read this way: every call gives the picture a new path, which
        ``read_playlists()`` gives from then on.
        """

    @abstractmethod
    def read_track_picture(self, rating_key: str) -> str | None:
        """The path of the picture Plex gives a track.

        It is the track's ``thumb``: the path of its album's picture, with the
        picture's stamp, so the tracks of an album share one path and a new
        picture has a new one. None when Plex has no track with this rating
        key, or the track has no picture.
        """

    @abstractmethod
    def read_track_pictures(self, rating_keys: Iterable[str]) -> dict[str, str]:
        """The paths of the pictures Plex gives several tracks, by rating
        key, from one request.

        Each is what ``read_track_picture()`` gives. A rating key Plex has no
        track with, and a track without a picture, are left out.
        """

    @abstractmethod
    def read_picture(self, picture: str, size: int) -> bytes | None:
        """The picture at a path Plex gave for an artist, an album, a track
        or a playlist, resized by Plex.

        A JPEG that covers a square of ``size`` pixels and keeps the picture's
        shape: its shorter side is ``size``, also where the source is smaller.
        Plex answers an item's picture as it is now, whatever stamp the path
        carries. None when Plex has no picture there, or ``picture`` is not
        the path of a picture.
        """


class PlexapiPlexClient(PlexClient):
    """Plex, through plexapi."""

    def __init__(self, settings: AppSettings, health_service: HealthService) -> None:
        super().__init__(health_service)
        self._url = settings.plex_url.rstrip("/")
        self._token = settings.plex_token
        self._library = settings.plex_library
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._server: PlexServer | None = None

    def check_readiness(self) -> CheckResult:
        try:
            self._music_section(_READINESS_TIMEOUT_SECONDS)
        except _PLEX_ERRORS as exc:
            return CheckResult(CheckStatus.ERROR, _cause(exc))
        return CheckResult(CheckStatus.OK)

    def machine_identifier(self) -> str:
        operation = "read which server Plex is"
        with _plex_call(operation):
            identifier: str | None = self._connected(
                _REQUEST_TIMEOUT_SECONDS
            ).machineIdentifier
        if not identifier:
            raise ExternalServiceException(
                operation, "Plex's answer names no machine identifier"
            )
        return identifier

    def section_id(self) -> str:
        with _plex_call("read Plex's music library"):
            return self._music_section(_REQUEST_TIMEOUT_SECONDS)

    def read_artists(self) -> list[PlexArtist]:
        with _plex_call("read the artists of Plex's music library"):
            section = self._music_section(_REQUEST_TIMEOUT_SECONDS)
            answer = self._query(f"/library/sections/{section}/all?type=8")
            return [parse_artist(item) for item in _items(answer, "artist")]

    def read_artist(self, rating_key: str) -> PlexArtist | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        with _plex_call(f"read Plex artist {rating_key}"):
            item = self._item(rating_key, "artist")
            return parse_artist(item) if item is not None else None

    def read_artist_albums(
        self, rating_key: str, newest_first: bool
    ) -> list[PlexAlbum] | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        sort = "originallyAvailableAt:desc" if newest_first else "originallyAvailableAt"
        with _plex_call(f"read the albums of Plex artist {rating_key}"):
            section = self._music_section(_REQUEST_TIMEOUT_SECONDS)
            listed = self._query(
                f"/library/sections/{section}/all"
                f"?type=9&artist.id={rating_key}&sort={sort}"
            )
            keys = [
                parse_rating_key(item, "ratingKey")
                for item in _items(listed, "album")
            ]
            if not keys:
                # Plex lists no albums for a rating key that is no artist's.
                if self._item(rating_key, "artist") is None:
                    return None
                return []
            # The list leaves out an album's track count, and the album
            # itself carries it.
            albums = [
                parse_album(item)
                for item in _items(self._several(keys), "album")
            ]
        # Plex puts the albums without a date first when the oldest is first.
        return in_release_order(albums, newest_first)

    def read_album(self, rating_key: str) -> PlexAlbum | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        with _plex_call(f"read Plex album {rating_key}"):
            item = self._item(rating_key, "album")
            return parse_album(item) if item is not None else None

    def read_album_tracks(self, rating_key: str) -> list[PlexTrack] | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        with _plex_call(f"read the tracks of Plex album {rating_key}"):
            # Plex answers an artist's children with its albums and a
            # track's with 400, and plexapi raises a refused token as the
            # error it raises for a 400.
            if self._item(rating_key, "album") is None:
                return None
            try:
                answer = self._query(f"/library/metadata/{rating_key}/children")
            except NotFound:
                return None
            return [parse_track(item) for item in _items(answer, "track")]

    def read_track(self, rating_key: str) -> PlexTrack | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        with _plex_call(f"read Plex track {rating_key}"):
            item = self._item(rating_key, "track")
            return parse_track(item) if item is not None else None

    def read_playlists(self) -> list[PlexPlaylist]:
        with _plex_call("read Plex's playlists"):
            answer = self._query("/playlists?playlistType=audio")
            return [parse_playlist(item) for item in _audio_playlists(answer)]

    def read_playlist(self, rating_key: str) -> PlexPlaylist | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        with _plex_call(f"read Plex playlist {rating_key}"):
            try:
                answer = self._query(f"/playlists/{rating_key}")
            except NotFound:
                return None
            # Plex answers the rating key of a track here with the track.
            for item in _audio_playlists(answer):
                return parse_playlist(item)
            return None

    def read_track_picture(self, rating_key: str) -> str | None:
        if not _RATING_KEY.fullmatch(rating_key):
            return None
        with _plex_call(f"read the picture of Plex track {rating_key}"):
            server = self._connected(_REQUEST_TIMEOUT_SECONDS)
            try:
                item = server.query(
                    f"/library/metadata/{rating_key}",
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except NotFound:
                return None
        track = item.find("Track")
        if track is None:
            return None
        thumb: str | None = track.attrib.get("thumb")
        return thumb

    def read_track_pictures(self, rating_keys: Iterable[str]) -> dict[str, str]:
        keys = list(
            dict.fromkeys(key for key in rating_keys if _RATING_KEY.fullmatch(key))
        )
        if not keys:
            return {}
        with _plex_call(f"read the pictures of {len(keys)} Plex tracks"):
            return {
                parse_rating_key(item, "ratingKey"): item.attrib["thumb"]
                for item in _items(self._several(keys), "track")
                if item.attrib.get("thumb")
            }

    def read_picture(self, picture: str, size: int) -> bytes | None:
        # Plex fetches whatever address `url` names, one outside itself too.
        if not _PICTURE.fullmatch(picture):
            return None
        operation = f"read Plex's picture {picture} at {size} pixels"
        params: dict[str, str | int] = {
            "url": picture,
            "width": size,
            "height": size,
            "minSize": 1,
            "upscale": 1,
        }
        with _plex_call(operation):
            response = self._session.get(
                f"{self._url}/photo/:/transcode",
                params=params,
                headers={"X-Plex-Token": self._token},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise ExternalServiceException(operation, "Plex refused the token")
        if response.status_code != 200:
            raise ExternalServiceException(
                operation, f"Plex answered {response.status_code}"
            )
        return response.content

    def _music_section(self, timeout: float) -> str:
        """The section id of the music library, as Plex has it now."""
        sections = self._connected(timeout).query("/library/sections", timeout=timeout)
        music = _items(sections, "artist")
        if self._library is None:
            if len(music) != 1:
                raise PlexAnswerError(
                    f"Plex has {len(music)} music libraries, and PLEX_LIBRARY "
                    "names none"
                )
            return parse_rating_key(music[0], "key")
        for section in music:
            if section.attrib.get("title") == self._library:
                return parse_rating_key(section, "key")
        raise PlexAnswerError(f"Plex has no music library named {self._library!r}")

    def _query(self, path: str) -> Element:
        answer: Element = self._connected(_REQUEST_TIMEOUT_SECONDS).query(
            path, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        return answer

    def _item(self, rating_key: str, kind: str) -> Element | None:
        """The item with a rating key, when it is of this kind."""
        for item in _items(self._several([rating_key]), kind):
            return item
        return None

    def _several(self, rating_keys: list[str]) -> Element:
        """The items with these rating keys, from one request, in the order
        asked. Plex leaves out a rating key it does not have, and answers 404
        when it has none of them."""
        try:
            return self._query(f"/library/metadata/{','.join(rating_keys)}")
        except NotFound:
            return Element("MediaContainer")

    def _connected(self, timeout: float) -> PlexServer:
        """The server. plexapi asks Plex for it when it builds one, so it is
        built at the first call that needs it, not with the app.

        The server keeps the timeout it was built with: every query names its
        own.
        """
        with self._lock:
            if self._server is None:
                self._server = PlexServer(
                    self._url, self._token, session=self._session, timeout=timeout
                )
            return self._server


@contextmanager
def _plex_call(operation: str) -> Iterator[None]:
    """Turn Plex's failure into an ExternalServiceException (BE-3)."""
    try:
        yield
    except _PLEX_ERRORS as exc:
        raise ExternalServiceException(operation, _cause(exc)) from exc


def _items(answer: Element, kind: str) -> list[Element]:
    """The items of one kind in an answer: Plex tells an artist, an album
    and a library by ``type``, whatever the element."""
    return [item for item in answer if item.attrib.get("type") == kind]


def _audio_playlists(answer: Element) -> list[Element]:
    return [
        item
        for item in _items(answer, "playlist")
        if item.attrib.get("playlistType") == "audio"
    ]


def _cause(exc: Exception) -> str:
    """What went wrong, in words of this module's own: plexapi's and
    requests' messages name Plex's address."""
    if isinstance(exc, PlexAnswerError):
        return str(exc)
    if isinstance(exc, Unauthorized):
        return "Plex refused the token"
    if isinstance(exc, requests.RequestException):
        return f"Plex did not answer ({type(exc).__name__})"
    return f"Plex gave an answer the backend cannot use ({type(exc).__name__})"
