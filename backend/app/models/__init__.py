"""SQLAlchemy models.

Import all models here so Alembic can detect them for migration auto-generation.
"""

from app.models.favourite import Favourite, ItemKind

__all__ = ["Favourite", "ItemKind"]
