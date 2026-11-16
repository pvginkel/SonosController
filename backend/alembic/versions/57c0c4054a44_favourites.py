"""The favourites: the household's marks on artists, albums and playlists.

Revision ID: 57c0c4054a44
Revises:
Create Date: 2026-11-16 19:01:12.621085

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "57c0c4054a44"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade database schema."""
    op.create_table(
        "favourites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "item_kind",
            sa.Enum(
                "artist",
                "album",
                "playlist",
                name="ck_favourites_item_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("rating_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("artist_title", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "item_kind", "guid", name="uq_favourites_item_kind_guid"
        ),
        sa.CheckConstraint(
            "(item_kind = 'album') = (artist_title IS NOT NULL)",
            name="ck_favourites_artist_title_of_album",
        ),
    )


def downgrade() -> None:
    """Downgrade database schema."""
    op.drop_table("favourites")
