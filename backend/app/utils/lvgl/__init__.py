"""LVGL's image converter.

``LVGLImage.py`` is LVGL's own ``scripts/LVGLImage.py``, byte for byte as
``pvginkel/IoTSupport`` keeps it in ``backend/app/utils/lvgl/``
(``docs/api-design.md`` section 3.7). It is replaced whole and never edited:
what lint finds in it is configured away in ``pyproject.toml`` and
``vulture_whitelist.py``.
"""

from app.utils.lvgl.LVGLImage import ColorFormat, CompressMethod, LVGLImage

__all__ = ["ColorFormat", "CompressMethod", "LVGLImage"]
