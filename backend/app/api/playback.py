"""What a speaker plays, and the playback commands (``docs/api-design.md``
section 3.2)."""

from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify
from spectree import Response as SpectreeResponse

from app.api.speakers import SPEAKER_FAILED, SPEAKER_ID_PARAMETER, SPEAKER_NOT_FOUND
from app.schemas.common import ApiErrorResponseSchema
from app.schemas.playback_schema import PlaybackSchema
from app.services.container import ServiceContainer
from app.services.household_mirror import HouseholdMirror, PlaybackAction
from app.services.speaker_service import SpeakerService
from app.utils.spectree_config import api

playback_bp = Blueprint(
    "playback", __name__, url_prefix="/speakers/<speaker_id>/playback"
)


def _command_answers() -> SpectreeResponse:
    return SpectreeResponse(
        HTTP_204=(None, "The speaker took the command."),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
        HTTP_409=(
            ApiErrorResponseSchema,
            "The speaker does not allow the command now: it is not among the "
            "playback's `actions`, or the speaker refused it because what it "
            "allows changed and `playback_changed` has yet to say so. The "
            "code is `ACTION_NOT_AVAILABLE`.",
        ),
        HTTP_502=(ApiErrorResponseSchema, SPEAKER_FAILED),
    )


@playback_bp.route("", methods=["GET"])
@api.validate(
    resp=SpectreeResponse(
        HTTP_200=(PlaybackSchema, "What the speaker's group plays."),
        HTTP_404=(ApiErrorResponseSchema, SPEAKER_NOT_FOUND),
    ),
    path_parameter_descriptions=SPEAKER_ID_PARAMETER,
)
@inject
def get_playback(
    speaker_id: str,
    household_mirror: HouseholdMirror = Provide[ServiceContainer.household_mirror],
) -> Response:
    """What the speaker plays, and what can be done with it.

    Playback is a group's: every speaker of a group answers the same, and a
    command to any of them acts on the group. Whatever plays is shown, also
    what was started elsewhere, such as a radio stream from the Sonos app.

    There is no elapsed time and no repeat.

    A client that shows a speaker fetches this on the event
    `playback_changed` for it. After a command the answer is the old one
    until the speaker has reported the change, which that event says.
    """
    playback = PlaybackSchema.model_validate(household_mirror.playback(speaker_id))
    return jsonify(playback.model_dump(mode="json"))


@playback_bp.route("/play", methods=["POST"])
@api.validate(resp=_command_answers(), path_parameter_descriptions=SPEAKER_ID_PARAMETER)
@inject
def play(
    speaker_id: str,
    speaker_service: SpeakerService = Provide[ServiceContainer.speaker_service],
) -> tuple[str, int]:
    """Play or resume what the speaker's group has loaded.

    What changed arrives as the event `playback_changed`.
    """
    speaker_service.command(speaker_id, PlaybackAction.PLAY)
    return "", 204


@playback_bp.route("/pause", methods=["POST"])
@api.validate(resp=_command_answers(), path_parameter_descriptions=SPEAKER_ID_PARAMETER)
@inject
def pause(
    speaker_id: str,
    speaker_service: SpeakerService = Provide[ServiceContainer.speaker_service],
) -> tuple[str, int]:
    """Pause the speaker's group.

    What the speaker cannot pause, as a radio stream, is stopped. What
    changed arrives as the event `playback_changed`.
    """
    speaker_service.command(speaker_id, PlaybackAction.PAUSE)
    return "", 204


@playback_bp.route("/next", methods=["POST"])
@api.validate(resp=_command_answers(), path_parameter_descriptions=SPEAKER_ID_PARAMETER)
@inject
def next_track(
    speaker_id: str,
    speaker_service: SpeakerService = Provide[ServiceContainer.speaker_service],
) -> tuple[str, int]:
    """Skip to the next track.

    What changed arrives as the event `playback_changed`.
    """
    speaker_service.command(speaker_id, PlaybackAction.NEXT)
    return "", 204


@playback_bp.route("/previous", methods=["POST"])
@api.validate(resp=_command_answers(), path_parameter_descriptions=SPEAKER_ID_PARAMETER)
@inject
def previous_track(
    speaker_id: str,
    speaker_service: SpeakerService = Provide[ServiceContainer.speaker_service],
) -> tuple[str, int]:
    """Go back to the previous track.

    What changed arrives as the event `playback_changed`.
    """
    speaker_service.command(speaker_id, PlaybackAction.PREVIOUS)
    return "", 204
