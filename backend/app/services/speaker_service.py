"""Commands to the speakers: playback and volume.

``docs/api-design.md`` sections 3.2 and 3.3. Whether a speaker exists and
what it allows now is read from the household mirror; the command itself goes
through the Sonos client, which sends a playback command to the coordinator
of the speaker's group. Nothing is changed in the mirror: the speaker's event
brings the change there.
"""

from collections.abc import Callable

from app.exceptions import ActionNotAvailableException
from app.services.household_mirror import HouseholdMirror, PlaybackAction
from app.services.sonos_client import SonosClient, TransportAction, Volume

# Each action of the API: how it follows "Cannot", and the speaker's commands
# that carry it out, of which the first the speaker allows is sent.
_COMMANDS: dict[PlaybackAction, tuple[str, tuple[TransportAction, ...]]] = {
    PlaybackAction.PLAY: ("play on", (TransportAction.PLAY,)),
    PlaybackAction.PAUSE: ("pause", (TransportAction.PAUSE, TransportAction.STOP)),
    PlaybackAction.NEXT: ("skip to the next track on", (TransportAction.NEXT,)),
    PlaybackAction.PREVIOUS: (
        "go back to the previous track on",
        (TransportAction.PREVIOUS,),
    ),
}


class SpeakerService:
    """What a client has a speaker do."""

    def __init__(
        self, household_mirror: HouseholdMirror, sonos_client: SonosClient
    ) -> None:
        self._mirror = household_mirror
        self._sonos = sonos_client
        self._send: dict[TransportAction, Callable[[str], None]] = {
            TransportAction.PLAY: sonos_client.play,
            TransportAction.PAUSE: sonos_client.pause,
            TransportAction.STOP: sonos_client.stop,
            TransportAction.NEXT: sonos_client.next,
            TransportAction.PREVIOUS: sonos_client.previous,
        }

    def command(self, speaker_id: str, action: PlaybackAction) -> None:
        """Send a playback command to the speaker's group.

        ``PAUSE`` stops what the speaker cannot pause and can stop, as a
        radio stream (section 1.7).

        Raises:
            SpeakerNotFoundException: The household has no such speaker.
            ActionNotAvailableException: The speaker does not allow the
                command now, by what the mirror holds.
            ExternalServiceException: The speaker failed.
        """
        described, commands = _COMMANDS[action]
        allowed = self._mirror.transport_actions(speaker_id)
        for command in commands:
            if command in allowed:
                self._send[command](speaker_id)
                return
        raise ActionNotAvailableException(f"{described} speaker {speaker_id}")

    def change_volume(
        self,
        speaker_id: str,
        *,
        volume: int | None = None,
        delta: int | None = None,
        muted: bool | None = None,
    ) -> Volume:
        """Change the speaker's own volume and mute: what is given of them.

        Args:
            volume: The volume to set, 0 to 100.
            delta: The speaker's own relative step, instead of ``volume``.
            muted: The mute to set.

        Returns:
            The volume and the mute as the speaker gives them afterwards.

        Raises:
            SpeakerNotFoundException: The household has no such speaker.
            ExternalServiceException: The speaker failed.
        """
        self._mirror.speaker(speaker_id)
        # A mute goes before the volume and an unmute after it: the speaker
        # never plays louder than where the change starts or ends.
        if muted is True:
            self._sonos.set_muted(speaker_id, True)
        if volume is not None:
            self._sonos.set_volume(speaker_id, volume)
        if delta is not None:
            self._sonos.change_volume(speaker_id, delta)
        if muted is False:
            self._sonos.set_muted(speaker_id, False)
        return self._sonos.read_volume(speaker_id)

    def change_group_volume(
        self,
        speaker_id: str,
        *,
        volume: int | None = None,
        delta: int | None = None,
        muted: bool | None = None,
    ) -> Volume:
        """Change the volume and the mute of the speaker's group: what is
        given of them. It moves every speaker of the group.

        Args:
            volume: The group volume to set, 0 to 100.
            delta: The speaker's own relative step, instead of ``volume``.
            muted: The mute to set, for every speaker of the group.

        Returns:
            The group's volume and mute as the speaker gives them afterwards.

        Raises:
            SpeakerNotFoundException: The household has no such speaker.
            ExternalServiceException: The speaker failed.
        """
        self._mirror.speaker(speaker_id)
        # As for a speaker's own volume: a mute first, an unmute last.
        if muted is True:
            self._sonos.set_group_muted(speaker_id, True)
        if volume is not None:
            self._sonos.set_group_volume(speaker_id, volume)
        if delta is not None:
            self._sonos.change_group_volume(speaker_id, delta)
        if muted is False:
            self._sonos.set_group_muted(speaker_id, False)
        return self._sonos.read_group_volume(speaker_id)
