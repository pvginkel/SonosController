"""What Plex's answers say of the music library's items.

The one reading of an artist, an album, a track and a playlist in an answer
of Plex's, for the Plex client (``docs/api-design.md`` sections 1.4, 1.8 and
1.9). Plex's answer is input from outside: what Plex was seen to leave out
of an item (a sort title, a release date, a picture, a track's disc, number
or duration, an empty playlist's duration) is None or its stand-in here, and
an item without what every item was seen to have, or with a number or a date
that is none, is a ``PlexAnswerError``.
"""

import re
from dataclasses import dataclass
from datetime import date
from xml.etree.ElementTree import Element

_DIGITS = re.compile(r"[0-9]+")


class PlexAnswerError(Exception):
    """Plex answered, and not with what the backend can use. Its text says
    what is wrong in words that name no address."""


@dataclass(frozen=True)
class PlexArtist:
    rating_key: str
    title: str
    # The title Plex files the artist under: its sort title, or its title
    # where Plex gives no sort title.
    sort_title: str
    # The path of the artist's picture, for PlexClient.read_picture().
    picture: str | None


@dataclass(frozen=True)
class PlexAlbum:
    rating_key: str
    title: str
    artist_rating_key: str
    artist: str
    released: date | None
    track_count: int
    # The path of the album's picture, for PlexClient.read_picture(): its
    # cover, or its artist's picture where Plex has no cover for it.
    picture: str | None

    @property
    def year(self) -> int | None:
        return self.released.year if self.released is not None else None


@dataclass(frozen=True)
class PlexTrack:
    rating_key: str
    title: str
    # The track's own artist where Plex has one, as on a compilation, and
    # its album's otherwise.
    artist: str
    disc: int | None
    number: int | None
    duration_ms: int | None
    # The path of its album's picture, which the album's tracks share: its
    # cover, or its artist's picture where it has no cover; None where it has
    # neither. A track has no picture of its own.
    picture: str | None


@dataclass(frozen=True)
class PlexPlaylist:
    rating_key: str
    title: str
    track_count: int
    # None for a playlist without tracks.
    duration_ms: int | None
    # The path of the picture Plex composes of the playlist's tracks, for
    # PlexClient.read_picture(). Its stamp is the time the playlist was last
    # read on its own. None for a playlist without tracks.
    picture: str | None


def parse_artist(item: Element) -> PlexArtist:
    title = _text(item, "title")
    return PlexArtist(
        rating_key=parse_rating_key(item, "ratingKey"),
        title=title,
        sort_title=item.attrib.get("titleSort") or title,
        picture=item.attrib.get("thumb") or None,
    )


def parse_album(item: Element) -> PlexAlbum:
    return PlexAlbum(
        rating_key=parse_rating_key(item, "ratingKey"),
        title=_text(item, "title"),
        artist_rating_key=parse_rating_key(item, "parentRatingKey"),
        artist=_text(item, "parentTitle"),
        released=_date(item, "originallyAvailableAt"),
        track_count=_count(item, "leafCount"),
        picture=item.attrib.get("thumb") or None,
    )


def parse_track(item: Element) -> PlexTrack:
    return PlexTrack(
        rating_key=parse_rating_key(item, "ratingKey"),
        title=_text(item, "title"),
        artist=item.attrib.get("originalTitle") or _text(item, "grandparentTitle"),
        disc=_number(item, "parentIndex"),
        number=_number(item, "index"),
        duration_ms=_number(item, "duration"),
        picture=track_picture(item),
    )


def track_picture(item: Element) -> str | None:
    """The path of the picture of a track in Plex's answer: its album's.

    Plex gives a track of an album with a cover the cover's path as its
    ``thumb``. It gives a track of an album without one no ``thumb``, and
    names its artist's picture, which it gives that album, ``grandparentThumb``.
    """
    return item.attrib.get("thumb") or item.attrib.get("grandparentThumb") or None


def parse_playlist(item: Element) -> PlexPlaylist:
    return PlexPlaylist(
        rating_key=parse_rating_key(item, "ratingKey"),
        title=_text(item, "title"),
        track_count=_count(item, "leafCount"),
        duration_ms=_number(item, "duration"),
        picture=item.attrib.get("composite") or None,
    )


def parse_rating_key(item: Element, name: str) -> str:
    """The rating key, or the section id, an item carries under ``name``."""
    value = item.attrib.get(name)
    if value is None or not _DIGITS.fullmatch(value):
        raise PlexAnswerError(f"Plex's answer has a {item.tag} without {name}")
    return value


def in_release_order(albums: list[PlexAlbum], newest_first: bool) -> list[PlexAlbum]:
    """Albums by release date, then title, then rating key, in the direction
    asked; the albums without a date last, in either direction
    (``docs/api-design.md`` section 1.4)."""
    dated = sorted(
        (album for album in albums if album.released is not None),
        key=lambda album: (album.released, *_by_title(album)),
        reverse=newest_first,
    )
    undated = sorted(
        (album for album in albums if album.released is None),
        key=_by_title,
        reverse=newest_first,
    )
    return dated + undated


def _by_title(album: PlexAlbum) -> tuple[str, int]:
    return album.title.casefold(), int(album.rating_key)


def _text(item: Element, name: str) -> str:
    value = item.attrib.get(name)
    if value is None:
        raise PlexAnswerError(f"Plex's answer has a {item.tag} without {name}")
    return value


def _number(item: Element, name: str) -> int | None:
    value = item.attrib.get(name)
    if value is None:
        return None
    if not _DIGITS.fullmatch(value):
        raise PlexAnswerError(
            f"Plex's answer has a {item.tag} whose {name} is not a number"
        )
    return int(value)


def _count(item: Element, name: str) -> int:
    value = _number(item, name)
    if value is None:
        raise PlexAnswerError(f"Plex's answer has a {item.tag} without {name}")
    return value


def _date(item: Element, name: str) -> date | None:
    value = item.attrib.get(name)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PlexAnswerError(
            f"Plex's answer has a {item.tag} whose {name} is not a date"
        ) from exc
