"""Tests for the favourite's store."""

from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Favourite, ItemKind


def _artist(guid: str = "plex://artist/5d07a", **values: object) -> Favourite:
    return Favourite(
        item_kind=ItemKind.ARTIST,
        guid=guid,
        rating_key="37059",
        title="Radiohead",
        **values,
    )


def _album(guid: str = "plex://album/5d07b", **values: object) -> Favourite:
    fields: dict[str, object] = {
        "item_kind": ItemKind.ALBUM,
        "guid": guid,
        "rating_key": "37060",
        "title": "Amnesiac",
        "artist_title": "Radiohead",
    }
    return Favourite(**(fields | values))


def _playlist(**values: object) -> Favourite:
    return Favourite(
        item_kind=ItemKind.PLAYLIST,
        guid="com.plexapp.agents.none://e2",
        rating_key="43919",
        title="Workshop",
        **values,
    )


def _stored(session: Session, favourite: Favourite) -> Favourite:
    """The favourite as the store gives it back."""
    session.add(favourite)
    session.flush()
    session.expire_all()
    return session.execute(
        select(Favourite).where(Favourite.id == favourite.id)
    ).scalar_one()


class TestFavourite:
    def test_a_favourite_of_each_kind_is_stored_as_marked(self, session: Session):
        artist = _stored(session, _artist())
        album = _stored(session, _album())
        playlist = _stored(session, _playlist())

        assert [
            (f.item_kind, f.guid, f.rating_key, f.title, f.artist_title)
            for f in (artist, album, playlist)
        ] == [
            (ItemKind.ARTIST, "plex://artist/5d07a", "37059", "Radiohead", None),
            (ItemKind.ALBUM, "plex://album/5d07b", "37060", "Amnesiac", "Radiohead"),
            (
                ItemKind.PLAYLIST,
                "com.plexapp.agents.none://e2",
                "43919",
                "Workshop",
                None,
            ),
        ]
        assert len({artist.id, album.id, playlist.id}) == 3

    def test_the_kind_is_stored_as_the_apis_kind_writes_it(self, session: Session):
        album = _stored(session, _album())

        stored = session.execute(
            text("SELECT item_kind FROM favourites WHERE id = :id"), {"id": album.id}
        ).scalar_one()

        assert stored == "album"

    def test_the_store_stamps_when_the_item_was_marked(self, session: Session):
        artist = _stored(session, _artist())

        assert isinstance(artist.created_at, datetime)
        assert artist.created_at.utcoffset() is not None

    def test_a_second_favourite_of_one_kind_and_guid_is_refused(
        self, session: Session
    ):
        _stored(session, _album(guid="plex://album/5e11"))
        session.add(_album(guid="plex://album/5e11", rating_key="38103"))

        with pytest.raises(IntegrityError, match="uq_favourites_item_kind_guid"):
            session.flush()

    def test_one_guid_may_be_marked_once_under_each_kind(self, session: Session):
        artist = _stored(session, _artist(guid="local://40001"))
        album = _stored(session, _album(guid="local://40001"))

        assert artist.id != album.id

    def test_an_album_without_its_artists_title_is_refused(self, session: Session):
        session.add(_album(artist_title=None))

        with pytest.raises(
            IntegrityError, match="ck_favourites_artist_title_of_album"
        ):
            session.flush()

    @pytest.mark.parametrize("favourite", [_artist, _playlist])
    def test_an_artist_or_a_playlist_with_an_artist_title_is_refused(
        self, session: Session, favourite
    ):
        session.add(favourite(artist_title="Radiohead"))

        with pytest.raises(
            IntegrityError, match="ck_favourites_artist_title_of_album"
        ):
            session.flush()

    def test_a_kind_that_is_none_of_the_three_is_refused(self, session: Session):
        with pytest.raises(IntegrityError, match="ck_favourites_item_kind"):
            session.execute(
                text(
                    "INSERT INTO favourites (item_kind, guid, rating_key, title)"
                    " VALUES ('track', 'plex://track/5d07c', '37061', 'Pyramid Song')"
                )
            )

    def test_the_titles_are_kept_as_plex_gives_them_an_empty_one_too(
        self, session: Session
    ):
        album = _stored(session, _album(title="", artist_title=""))

        assert (album.title, album.artist_title) == ("", "")
