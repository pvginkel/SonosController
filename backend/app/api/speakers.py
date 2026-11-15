"""The speakers and their volume (``docs/api-design.md`` sections 3.1 and
3.3)."""

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify, request
from spectree import Response as SpectreeResponse

from app.schemas.common import ApiErrorResponseSchema
from app.schemas.speaker_schema import (
    SpeakerListSchema,
    SpeakerSchema,
    VolumeChangeSchema,
    VolumeSchema,
)
from app.services.container import ServiceContainer
from app.services.household_mirror import HouseholdMirror
from app.services.speaker_service import SpeakerService
from app.utils.spectree_config import api

SPEAKER_ID_PARAMETER = {
    "speaker_id": (
        "The speaker: its Sonos id (`RINCON_…`), the `id` of a speaker in "
        "`GET /api/speakers`."
    )
}
SPEAKER_NOT_FOUND = "No speaker has this id. The code is `SPEAKER_NOT_FOUND`."
SPEAKER_FAILED = "The speaker failed or did not answer."

_NEW_VOLUME = "The volume and the mute as they are after the change."
_REFUSED_BODY = (
    "The body is not a JSON object, holds both `volume` and `delta` or "
    "neither, or holds a value out of range or a field that is not one of "
    "the three."
)

speakers_bp = Blueprint("speakers", __name__, url_prefix="/speakers")


@speakers_bp.route("", methods=["GET"])
@api.validate(
    resp=SpectreeResponse(HTTP_200=(SpeakerListSchema, "Every speaker, by name."))
)
@inject
def list_speakers(
    household_mirror: HouseholdMirror = Provide[ServiceContainer.household_mirror],
) -> Response:
    """Every speaker of the household.

    A speaker is a room: the devices a room is made of are not listed
    themselves. The list is not paged, a household's speakers being few, and
    it is empty while no speaker answers the backend.

    The event `speakers_changed` says that the list changed, and
    `volume_changed` that a speaker's volume did.
    """
    speakers = SpeakerListSchema(
        data=[
            SpeakerSchema.model_validate(speaker)
            for speaker in household_mirror.speakers()
        ]
    )
    return jsonify(speakers.model_dump(mode="json"))


@speakers_bp.route("/<speaker_id>", methods=["GET"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_200=(SpeakerSchema, "The speaker."),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def get_speaker(
    speaker_id: str,
    household_mirror: HouseholdMirror = Provide[ServiceContainer.household_mirror],
) -> Response:
    """One speaker, with its volume and its group.

    A client that shows a speaker fetches this on the event `volume_changed`
    for it.
    """
    speaker = SpeakerSchema.model_validate(household_mirror.speaker(speaker_id))
    return jsonify(speaker.model_dump(mode="json"))


@speakers_bp.route("/<speaker_id>/volume", methods=["POST"])
@api.validate(
    json=VolumeChangeSchema,
    resp=SpectreeResponse(
        HTTP_200=(VolumeSchema, _NEW_VOLUME),
        HTTP_400=(ApiErrorResponseSchema, _REFUSED_BODY),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, SPEAKER_FAILED),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def change_volume(
    speaker_id: str,
    speaker_service: SpeakerService = Provide[ServiceContainer.speaker_service],
) -> Response:
    """Change the speaker's own volume.

    It moves this speaker alone, also when it plays in a group; the group's
    volume follows from its speakers'. The answer is the speaker's volume
    and mute after the change, for a client to show at once.
    """
    change = VolumeChangeSchema.model_validate(request.get_json())
    volume = speaker_service.change_volume(
        speaker_id, volume=change.volume, delta=change.delta, muted=change.muted
    )
    return jsonify(VolumeSchema.model_validate(volume).model_dump(mode="json"))


@speakers_bp.route("/<speaker_id>/group/volume", methods=["POST"])
@api.validate(
    json=VolumeChangeSchema,
    resp=SpectreeResponse(
        HTTP_200=(VolumeSchema, _NEW_VOLUME),
        HTTP_400=(ApiErrorResponseSchema, _REFUSED_BODY),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
        HTTP_502=(ApiErrorResponseSchema, SPEAKER_FAILED),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def change_group_volume(
    speaker_id: str,
    speaker_service: SpeakerService = Provide[ServiceContainer.speaker_service],
) -> Response:
    """Change the volume of the speaker's group.

    It moves every speaker of the group, as the main slider of the Sonos app
    does, and `muted` mutes or unmutes them all. For a speaker alone it is
    the speaker's own volume, so a control for "what plays here" uses this
    one. The answer is the group's volume and mute after the change, for a
    client to show at once.
    """
    change = VolumeChangeSchema.model_validate(request.get_json())
    volume = speaker_service.change_group_volume(
        speaker_id, volume=change.volume, delta=change.delta, muted=change.muted
    )
    return jsonify(VolumeSchema.model_validate(volume).model_dump(mode="json"))

