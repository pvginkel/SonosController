"""Artwork ids: the API's name for a picture.

``docs/api-design.md`` section 3.7: an id names one picture, two objects with
the same picture carry the same id, and a picture that changes gets another.
An id is opaque to a client. Here it is one of two things, in base64 for a
path segment:

- **Plex's picture**, ``plex:<path>``: the path Plex gives an item's picture,
  which carries the item's rating key and the picture's stamp. An album and
  its tracks have one path.
- **A speaker's picture**, ``sonos:<digest>:<speaker id>``: the room that
  coordinates the group that plays the track, and a digest of the track's
  address and of the address its picture is served at. The id is as long as
  the speaker's id allows, whatever the track's address is.
"""

import base64
import binascii
import hashlib
from dataclasses import dataclass

_PLEX = "plex"
_SONOS = "sonos"


@dataclass(frozen=True)
class PlexPicture:
    """An id of Plex's picture at ``path``."""

    path: str


@dataclass(frozen=True)
class SpeakerPicture:
    """An id of a picture that ``speaker_id`` serves, or served."""

    speaker_id: str


def plex_artwork_id(path: str) -> str:
    """The id of the picture Plex has at a path."""
    return _encode(f"{_PLEX}:{path}")


def speaker_artwork_id(speaker_id: str, track_uri: str, picture: str) -> str:
    """The id of the picture a speaker serves at ``picture`` for a track."""
    digest = hashlib.sha256(f"{track_uri}\n{picture}".encode()).hexdigest()[:32]
    return _encode(f"{_SONOS}:{digest}:{speaker_id}")


def parse_artwork_id(artwork_id: str) -> PlexPicture | SpeakerPicture | None:
    """What an id names; None for a text that is not an id this module made."""
    try:
        text = base64.urlsafe_b64decode(
            artwork_id + "=" * (-len(artwork_id) % 4)
        ).decode()
    except (binascii.Error, ValueError):
        return None
    # One picture has one id: base64 has several texts for the same bytes.
    if _encode(text) != artwork_id:
        return None
    kind, _, rest = text.partition(":")
    if kind == _PLEX and rest:
        return PlexPicture(path=rest)
    if kind == _SONOS:
        digest, _, speaker_id = rest.partition(":")
        if digest and speaker_id:
            return SpeakerPicture(speaker_id=speaker_id)
    return None


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
