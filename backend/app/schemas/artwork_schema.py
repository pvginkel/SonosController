"""The artwork endpoint's query."""

from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.services.artwork_service import (
    JPEG_SIZE_MAX,
    LVGL_SIZE_MAX,
    SIZE_MIN,
    ArtworkFormat,
)


class ArtworkQuerySchema(BaseModel):
    """The size and the form of a picture."""

    size: int = Field(
        ...,
        ge=SIZE_MIN,
        le=JPEG_SIZE_MAX,
        description=(
            "The side of the square, in pixels. The picture is scaled to cover "
            f"the square and cropped at the centre. From {SIZE_MIN} to "
            f"{JPEG_SIZE_MAX} for `jpeg`, and to {LVGL_SIZE_MAX} for `lvgl`."
        ),
    )
    format: ArtworkFormat = Field(
        ArtworkFormat.JPEG,
        description=(
            "`jpeg`: a JPEG, for any client. `lvgl`: LVGL 9's binary image, "
            "which a display draws without decoding."
        ),
    )

    @model_validator(mode="after")
    def _lvgl_size(self) -> Self:
        if self.format is ArtworkFormat.LVGL and self.size > LVGL_SIZE_MAX:
            raise ValueError(
                f"size is at most {LVGL_SIZE_MAX} for format=lvgl, not {self.size}"
            )
        return self
