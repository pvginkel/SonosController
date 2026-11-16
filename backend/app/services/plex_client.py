"""The Plex client: the backend's only way to Plex.

``PlexClient`` is what the rest of the backend sees: which server Plex is,
the picture Plex gives a track, and that picture resized by Plex.
``PlexapiPlexClient`` implements it on plexapi, and this is the only module that imports plexapi. pytest overrides
the container's provider with an in-memory fake of the same base class.

An item is addressed by its rating key, as a string. The token travels in a
request header and nowhere else: no address this module builds and no failure
it raises carries it, or Plex's address.
"""

import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from xml.etree.ElementTree import ParseError

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.server import PlexServer

from app.app_config import AppSettings
from app.core_exceptions import ExternalServiceException
from app.services.health_service import CheckResult, CheckStatus, HealthService

_REQUEST_TIMEOUT_SECONDS = 5.0
# Plex takes half a second to refuse a token it has not seen before.
_READINESS_TIMEOUT_SECONDS = 2.0

_RATING_KEY = re.compile(r"[0-9]+")

# The path Plex gives an item's picture: its rating key, then a stamp.
_PICTURE = re.compile(r"/library/metadata/[0-9]+/thumb/[0-9]+")

_PLEX_ERRORS = (PlexApiException, requests.RequestException, ParseError)


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
    def read_track_picture(self, rating_key: str) -> str | None:
        """The path of the picture Plex gives a track.

        It is the track's ``thumb``: the path of its album's picture, with the
        picture's stamp, so the tracks of an album share one path and a new
        picture has a new one. None when Plex has no track with this rating
        key, or the track has no picture.
        """

    @abstractmethod
    def read_picture(self, picture: str, size: int) -> bytes | None:
        """The picture at a path ``read_track_picture()`` gave, resized by Plex.

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
            sections = self._connected(_READINESS_TIMEOUT_SECONDS).query(
                "/library/sections", timeout=_READINESS_TIMEOUT_SECONDS
            )
        except _PLEX_ERRORS as exc:
            return CheckResult(CheckStatus.ERROR, _cause(exc))
        music = [
            section.attrib.get("title")
            for section in sections.iter("Directory")
            if section.attrib.get("type") == "artist"
        ]
        if self._library is None:
            if len(music) != 1:
                return CheckResult(
                    CheckStatus.ERROR,
                    f"Plex has {len(music)} music libraries, and PLEX_LIBRARY "
                    "names none",
                )
        elif self._library not in music:
            return CheckResult(
                CheckStatus.ERROR,
                f"Plex has no music library named {self._library!r}",
            )
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


def _cause(exc: Exception) -> str:
    """What went wrong, in words of this module's own: plexapi's and
    requests' messages name Plex's address."""
    if isinstance(exc, Unauthorized):
        return "Plex refused the token"
    if isinstance(exc, requests.RequestException):
        return f"Plex did not answer ({type(exc).__name__})"
    return f"Plex gave an answer the backend cannot use ({type(exc).__name__})"
