"""The music library (``docs/api-design.md`` section 3.5)."""

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify, request
from spectree import Response as SpectreeResponse

from app.schemas.common import ApiErrorResponseSchema
from app.schemas.library_schema import (
    AlbumListQuerySchema,
    AlbumPageSchema,
    AlbumSchema,
    ArtistListQuerySchema,
    ArtistPageSchema,
    ArtistSchema,
    PlaylistListQuerySchema,
    PlaylistPageSchema,
    PlaylistSchema,
    TrackListQuerySchema,
    TrackPageSchema,
    TrackSchema,
)
from app.services.container import ServiceContainer
from app.services.library_service import LibraryService
from app.utils.pagination import build_pagination_meta
from app.utils.spectree_config import api

_ARTIST_ID_PARAMETER = {
    "artist_id": "The artist: Plex's rating key, the `id` of an artist."
}
_ALBUM_ID_PARAMETER = {
    "album_id": "The album: Plex's rating key, the `id` of an album."
}
_PLAYLIST_ID_PARAMETER = {
    "playlist_id": "The playlist: Plex's rating key, the `id` of a playlist."
}

_REFUSED_QUERY = (
    "`page`, `per_page`, `sort_by` or `sort_dir` holds a value the list does "
    "not take."
)
_ARTIST_NOT_FOUND = "Plex has no artist with this id. The code is `ITEM_NOT_FOUND`."
_ALBUM_NOT_FOUND = "Plex has no album with this id. The code is `ITEM_NOT_FOUND`."
_PLAYLIST_NOT_FOUND = (
    "Plex has no playlist of music with this id. The code is `ITEM_NOT_FOUND`."
)
_PLEX_FAILED = "Plex failed or did not answer."

library_bp = Blueprint("library", __name__, url_prefix="/library")


@library_bp.route("/artists", methods=["GET"])
@api.validate(
    query=ArtistListQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=(ArtistPageSchema, "A page of the artists."),
        HTTP_400=(ApiErrorResponseSchema, _REFUSED_QUERY),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
)
@inject
def list_artists(
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """The artists of the music library.

    They come by the title Plex sorts each by, without accents and in one
    case. Each carries the letter it files under, `index_letter`, so a
    client can offer a jump from A to Z without reading a title: a letter's
    artists are together, and those that file under no letter come first.

    The library is read from Plex at every request.
    """
    query = ArtistListQuerySchema.model_validate(request.args.to_dict())
    artists, total = library_service.list_artists(query)
    page = ArtistPageSchema(
        data=[ArtistSchema.model_validate(artist) for artist in artists],
        meta=build_pagination_meta(
            total, query.page, query.per_page, query.sort_by, query.sort_dir
        ),
    )
    return jsonify(page.model_dump(mode="json"))


@library_bp.route("/artists/<artist_id>", methods=["GET"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_200=(ArtistSchema, "The artist."),
        HTTP_404=(ApiErrorResponseSchema, _ARTIST_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
    path_parameter_descriptions=_ARTIST_ID_PARAMETER,
)
@inject
def get_artist(
    artist_id: str,
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """One artist."""
    artist = ArtistSchema.model_validate(library_service.artist(artist_id))
    return jsonify(artist.model_dump(mode="json"))


@library_bp.route("/artists/<artist_id>/albums", methods=["GET"])
@api.validate(
    query=AlbumListQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=(AlbumPageSchema, "A page of the artist's albums."),
        HTTP_400=(ApiErrorResponseSchema, _REFUSED_QUERY),
        HTTP_404=(ApiErrorResponseSchema, _ARTIST_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
    path_parameter_descriptions=_ARTIST_ID_PARAMETER,
)
@inject
def list_artist_albums(
    artist_id: str,
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """An artist's albums.

    Without `sort_by` and `sort_dir` they come newest first, by release
    date: the order in which playing the artist plays them. `sort_dir=asc`
    is oldest first. In either direction the albums without a date come
    last.
    """
    query = AlbumListQuerySchema.model_validate(request.args.to_dict())
    albums, total = library_service.list_artist_albums(artist_id, query)
    page = AlbumPageSchema(
        data=[AlbumSchema.model_validate(album) for album in albums],
        meta=build_pagination_meta(
            total, query.page, query.per_page, query.sort_by, query.sort_dir
        ),
    )
    return jsonify(page.model_dump(mode="json"))


@library_bp.route("/albums/<album_id>", methods=["GET"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_200=(AlbumSchema, "The album."),
        HTTP_404=(ApiErrorResponseSchema, _ALBUM_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
    path_parameter_descriptions=_ALBUM_ID_PARAMETER,
)
@inject
def get_album(
    album_id: str,
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """One album."""
    album = AlbumSchema.model_validate(library_service.album(album_id))
    return jsonify(album.model_dump(mode="json"))


@library_bp.route("/albums/<album_id>/tracks", methods=["GET"])
@api.validate(
    query=TrackListQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=(TrackPageSchema, "A page of the album's tracks."),
        HTTP_400=(ApiErrorResponseSchema, _REFUSED_QUERY),
        HTTP_404=(ApiErrorResponseSchema, _ALBUM_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
    path_parameter_descriptions=_ALBUM_ID_PARAMETER,
)
@inject
def list_album_tracks(
    album_id: str,
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """An album's tracks, in the order of the album: by disc, then by
    number."""
    query = TrackListQuerySchema.model_validate(request.args.to_dict())
    tracks, total = library_service.list_album_tracks(album_id, query)
    page = TrackPageSchema(
        data=[TrackSchema.model_validate(track) for track in tracks],
        meta=build_pagination_meta(
            total, query.page, query.per_page, query.sort_by, query.sort_dir
        ),
    )
    return jsonify(page.model_dump(mode="json"))


@library_bp.route("/playlists", methods=["GET"])
@api.validate(
    query=PlaylistListQuerySchema,
    resp=SpectreeResponse(
        HTTP_200=(PlaylistPageSchema, "A page of the playlists."),
        HTTP_400=(ApiErrorResponseSchema, _REFUSED_QUERY),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
)
@inject
def list_playlists(
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """The playlists of music that can be played, by title.

    A playlist is listed when it holds at least one track and no more than
    a speaker takes at once, 1000 unless the backend is set otherwise. That
    leaves out the empty playlists and the ones too large to queue, Plex's
    own among them. A smart playlist is listed by the count Plex last
    stored for it, which can differ from what it holds now.

    The tracks of a playlist are not listed.
    """
    query = PlaylistListQuerySchema.model_validate(request.args.to_dict())
    playlists, total = library_service.list_playlists(query)
    page = PlaylistPageSchema(
        data=[PlaylistSchema.model_validate(playlist) for playlist in playlists],
        meta=build_pagination_meta(
            total, query.page, query.per_page, query.sort_by, query.sort_dir
        ),
    )
    return jsonify(page.model_dump(mode="json"))


@library_bp.route("/playlists/<playlist_id>", methods=["GET"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_200=(PlaylistSchema, "The playlist."),
        HTTP_404=(ApiErrorResponseSchema, _PLAYLIST_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, _PLEX_FAILED),
    ),
    path_parameter_descriptions=_PLAYLIST_ID_PARAMETER,
)
@inject
def get_playlist(
    playlist_id: str,
    library_service: LibraryService = Provide[ServiceContainer.library_service],
) -> Response:
    """One playlist of music, as it is now.

    It answers every playlist Plex has, also one the list leaves out, with
    the tracks it holds at this request.
    """
    playlist = PlaylistSchema.model_validate(library_service.playlist(playlist_id))
    return jsonify(playlist.model_dump(mode="json"))
