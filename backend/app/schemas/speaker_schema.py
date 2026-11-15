"""The speakers and their volume, as the API answers and takes them
(``docs/api-design.md`` sections 3.1 and 3.3)."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpeakerGroupSchema(BaseModel):
    """The speakers a speaker plays together with, itself included. A speaker
    that plays alone has a group of one, with itself as the coordinator.
    """

    model_config = ConfigDict(from_attributes=True)

    coordinator_id: str = Field(
        description=(
            "The `id` of the speaker that leads the group. What the group "
            "plays is its playback."
        )
    )
    member_ids: list[str] = Field(
        description="The `id` of every speaker of the group, this one included."
    )
    volume: int = Field(
        ge=0,
        le=100,
        description=(
            "The group's volume, 0 to 100. For a speaker alone it is the "
            "speaker's own volume."
        ),
    )
    muted: bool = Field(description="Whether the group is muted.")


class SpeakerSchema(BaseModel):
    """A speaker: a room of the Sonos household. The devices a room is made
    of, such as the two halves of a stereo pair, are one speaker.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "The speaker's Sonos id (`RINCON_…`), the `speaker_id` of every "
            "path. It is the device's own and does not change with the "
            "speaker's name or address: a client that remembers a speaker "
            "keeps this."
        )
    )
    name: str = Field(description="The room's name, as the Sonos app shows it.")
    model: str = Field(description="The speaker's model, such as `Play:3`.")
    volume: int = Field(
        ge=0, le=100, description="The speaker's own volume, 0 to 100."
    )
    muted: bool = Field(description="Whether the speaker itself is muted.")
    group: SpeakerGroupSchema = Field(
        description="The group the speaker plays in, with the group's volume."
    )


class SpeakerListSchema(BaseModel):
    """The speakers of the household."""

    data: list[SpeakerSchema] = Field(
        description=(
            "Every speaker, by name. Empty while no speaker answers the backend."
        )
    )


class VolumeChangeSchema(BaseModel):
    """A change of a volume: `volume` or `delta`, one of the two, and `muted`
    where the mute changes with it.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    volume: int | None = Field(
        None, ge=0, le=100, description="The volume to set, 0 to 100."
    )
    delta: int | None = Field(
        None,
        ge=-100,
        le=100,
        description=(
            "A step from the volume as it is now, -100 to 100; `0` leaves it. "
            "The speaker takes the step itself and keeps the volume within 0 "
            "to 100, so the steps of two clients add up, where two values of "
            "`volume` overwrite each other."
        ),
    )
    muted: bool | None = Field(
        None,
        description=(
            "`true` mutes and `false` unmutes. Left out, the mute stays as it is."
        ),
    )

    @model_validator(mode="after")
    def _volume_or_delta(self) -> Self:
        if (self.volume is None) == (self.delta is None):
            raise ValueError("the body holds volume or delta, one of the two")
        return self


class VolumeSchema(BaseModel):
    """A volume and its mute, as the speaker gives them."""

    model_config = ConfigDict(from_attributes=True)

    volume: int = Field(ge=0, le=100, description="The volume, 0 to 100.")
    muted: bool = Field(description="Whether it is muted.")
