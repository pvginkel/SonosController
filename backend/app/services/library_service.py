"""The music library, as the API serves it (``docs/api-design.md`` section
3.5).

Every call reads Plex through the Plex client, and nothing of the library is
kept. An item is addressed by its rating key. What leaves here is ready to
serve: every string is Unicode NFC, a picture is the artwork id the artwork
endpoint takes, and a list is in an order that ends in the rating key, so
that its pages cut one sequence.

A title is ordered and filed by its letters alone: without accents and in
one case.
"""

import unicodedata
from dataclasses import dataclass

from app.exceptions import ItemNotFoundException
from app.schemas.common import PaginationQuerySchema
from app.schemas.library_schema import (
    AlbumListQuerySchema,
    ArtistListQuerySchema,
    PlaylistListQuerySchema,
    TrackListQuerySchema,
)
from app.services.plex_client import PlexClient
from app.utils.artwork_id import plex_artwork_id
from app.utils.plex_library import PlexAlbum, PlexArtist, PlexPlaylist, PlexTrack

# What an artist files under whose sort title starts with no letter of A to Z.
NO_LETTER = "#"


@dataclass(frozen=True)
class LibraryArtist:
    id: str
    title: str
    index_letter: str
    artwork_id: str | None


@dataclass(frozen=True)
class LibraryAlbum:
    id: str
    title: str
    artist_id: str
    artist: str
    year: int | None
    track_count: int
    artwork_id: str | None


@dataclass(frozen=True)
class LibraryTrack:
    id: str
    title: str
    artist: str
    disc: int | None
    number: int | None
    duration_ms: int | None
    artwork_id: str | None


@dataclass(frozen=True)
class LibraryPlaylist:
    id: str
    title: str
    track_count: int
    duration_ms: int | None
    artwork_id: str | None


class LibraryService:
    """The artists, albums, tracks and playlists of Plex's music library.

    Every method raises ``ExternalServiceException`` when Plex fails.
    """

    def __init__(self, plex_client: PlexClient, playlist_max_tracks: int) -> None:
        self._plex = plex_client
        self._playlist_max_tracks = playlist_max_tracks

    def list_artists(
        self, query: ArtistListQuerySchema
    ) -> tuple[list[LibraryArtist], int]:
        """A page of the artists, and how many artists there are.

        By sort title: the artists that file under no letter first, then A
        to Z, each letter's artists together.
        """
        artists = sorted(
            self._plex.read_artists(),
            key=_artist_order,
            reverse=query.sort_dir == "desc",
        )
        return [_artist(artist) for artist in _page(artists, query)], len(artists)

    def artist(self, artist_id: str) -> LibraryArtist:
        """One artist.

        Raises:
            ItemNotFoundException: Plex has no artist with this rating key.
        """
        artist = self._plex.read_artist(artist_id)
        if artist is None:
            raise ItemNotFoundException("Artist", artist_id)
        return _artist(artist)

    def list_artist_albums(
        self, artist_id: str, query: AlbumListQuerySchema
    ) -> tuple[list[LibraryAlbum], int]:
        """A page of an artist's albums, and how many albums the artist has.

        By ``release`` it is the Plex client's release order, the one an
        artist is queued in: newest first for ``desc``, the albums without a
        date last in either direction.

        Raises:
            ItemNotFoundException: Plex has no artist with this rating key.
        """
        descending = query.sort_dir == "desc"
        albums = self._plex.read_artist_albums(artist_id, newest_first=descending)
        if albums is None:
            raise ItemNotFoundException("Artist", artist_id)
        if query.sort_by == "title":
            albums = sorted(albums, key=_title_order, reverse=descending)
        return [_album(album) for album in _page(albums, query)], len(albums)

    def album(self, album_id: str) -> LibraryAlbum:
        """One album.

        Raises:
            ItemNotFoundException: Plex has no album with this rating key.
        """
        album = self._plex.read_album(album_id)
        if album is None:
            raise ItemNotFoundException("Album", album_id)
        return _album(album)

    def list_album_tracks(
        self, album_id: str, query: TrackListQuerySchema
    ) -> tuple[list[LibraryTrack], int]:
        """A page of an album's tracks, by disc and then by number, and how
        many tracks the album has. A track Plex gives no disc or no number
        comes before those that have one.

        Raises:
            ItemNotFoundException: Plex has no album with this rating key.
        """
        tracks = self._plex.read_album_tracks(album_id)
        if tracks is None:
            raise ItemNotFoundException("Album", album_id)
        tracks = sorted(tracks, key=_track_order, reverse=query.sort_dir == "desc")
        return [_track(track) for track in _page(tracks, query)], len(tracks)

    def list_playlists(
        self, query: PlaylistListQuerySchema
    ) -> tuple[list[LibraryPlaylist], int]:
        """A page of the playlists that can be played, by title, and how many
        of those there are.

        A playlist can be played when it holds 1 to ``playlist_max_tracks``
        tracks, by the count Plex lists it with (``docs/api-design.md``
        section 1.6).
        """
        playlists = sorted(
            (
                playlist
                for playlist in self._plex.read_playlists()
                if 1 <= playlist.track_count <= self._playlist_max_tracks
            ),
            key=_title_order,
            reverse=query.sort_dir == "desc",
        )
        return (
            [_playlist(playlist) for playlist in _page(playlists, query)],
            len(playlists),
        )

    def playlist(self, playlist_id: str) -> LibraryPlaylist:
        """One playlist as it is now, also one the list leaves out.

        Raises:
            ItemNotFoundException: Plex has no audio playlist with this
                rating key.
        """
        playlist = self._plex.read_playlist(playlist_id)
        if playlist is None:
            raise ItemNotFoundException("Playlist", playlist_id)
        return _playlist(playlist)


def _page[T](items: list[T], query: PaginationQuerySchema) -> list[T]:
    return items[query.offset : query.offset + query.per_page]


def _artist_order(artist: PlexArtist) -> tuple[bool, str, int]:
    return (
        _index_letter(artist.sort_title) != NO_LETTER,
        _letters(artist.sort_title),
        int(artist.rating_key),
    )


def _title_order(item: PlexAlbum | PlexPlaylist) -> tuple[str, int]:
    return _letters(item.title), int(item.rating_key)


def _track_order(track: PlexTrack) -> tuple[int, int, int]:
    return track.disc or 0, track.number or 0, int(track.rating_key)


def _letters(title: str) -> str:
    """A title without its accents and in one case."""
    decomposed = unicodedata.normalize("NFKD", title)
    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()


def _index_letter(sort_title: str) -> str:
    """The letter a sort title files under: A to Z, or ``#``."""
    first = _letters(sort_title)[:1].upper()
    return first if len(first) == 1 and "A" <= first <= "Z" else NO_LETTER


def _artist(artist: PlexArtist) -> LibraryArtist:
    return LibraryArtist(
        id=artist.rating_key,
        title=_nfc(artist.title),
        index_letter=_index_letter(artist.sort_title),
        artwork_id=_artwork_id(artist.picture),
    )


def _album(album: PlexAlbum) -> LibraryAlbum:
    return LibraryAlbum(
        id=album.rating_key,
        title=_nfc(album.title),
        artist_id=album.artist_rating_key,
        artist=_nfc(album.artist),
        year=album.year,
        track_count=album.track_count,
        artwork_id=_artwork_id(album.picture),
    )


def _track(track: PlexTrack) -> LibraryTrack:
    return LibraryTrack(
        id=track.rating_key,
        title=_nfc(track.title),
        artist=_nfc(track.artist),
        disc=track.disc,
        number=track.number,
        duration_ms=track.duration_ms,
        artwork_id=_artwork_id(track.picture),
    )


def _playlist(playlist: PlexPlaylist) -> LibraryPlaylist:
    return LibraryPlaylist(
        id=playlist.rating_key,
        title=_nfc(playlist.title),
        track_count=playlist.track_count,
        duration_ms=playlist.duration_ms,
        artwork_id=_artwork_id(playlist.picture),
    )


def _artwork_id(picture: str | None) -> str | None:
    return plex_artwork_id(picture) if picture is not None else None


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
