"""Base exceptions with user-ready messages.

These are template-provided base exceptions that all apps need. Add
app-specific exceptions below these base classes.
"""


class ConfigurationError(Exception):
    """Raised when application configuration is invalid."""

    pass


class BusinessLogicException(Exception):
    """Base exception class for business logic errors.

    All business logic exceptions include user-ready messages that can be
    displayed directly in the UI without client-side message construction.
    """

    def __init__(self, message: str, error_code: str) -> None:
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class RecordNotFoundException(BusinessLogicException):
    """Exception raised when a requested record is not found."""

    def __init__(self, resource_type: str, identifier: str | int) -> None:
        message = f"{resource_type} {identifier} was not found"
        super().__init__(message, error_code="RECORD_NOT_FOUND")


class ResourceConflictException(BusinessLogicException):
    """Exception raised when attempting to create a resource that already exists."""

    def __init__(self, resource_type: str, identifier: str | int) -> None:
        message = f"A {resource_type.lower()} with {identifier} already exists"
        super().__init__(message, error_code="RESOURCE_CONFLICT")


class InvalidOperationException(BusinessLogicException):
    """Exception raised when an operation cannot be performed due to business rules."""

    def __init__(self, operation: str, cause: str) -> None:
        self.operation = operation
        self.cause = cause
        message = f"Cannot {operation} because {cause}"
        super().__init__(message, error_code="INVALID_OPERATION")


class RouteNotAvailableException(BusinessLogicException):
    """Exception raised when accessing endpoints that are not available in the current mode."""

    def __init__(
        self,
        message: str = "This endpoint is only available when the server is running in testing mode",
    ) -> None:
        super().__init__(message, error_code="ROUTE_NOT_AVAILABLE")


class AuthenticationException(BusinessLogicException):
    """Exception raised when authentication fails (missing, invalid, or expired token)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="AUTHENTICATION_REQUIRED")


class AuthorizationException(BusinessLogicException):
    """Exception raised when the authenticated user lacks required permissions."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="AUTHORIZATION_FAILED")


class ValidationException(BusinessLogicException):
    """Exception raised for request validation failures (malformed input, invalid redirect, etc.)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code="VALIDATION_FAILED")


class SpeakerNotFoundException(RecordNotFoundException):
    """Exception raised when the household has no room with the given Sonos id."""

    def __init__(self, speaker_id: str) -> None:
        super().__init__("Speaker", speaker_id)
        self.error_code = "SPEAKER_NOT_FOUND"


class ItemNotFoundException(RecordNotFoundException):
    """Exception raised when Plex has no item of a kind with the given rating key.

    Args:
        kind: What was asked for, as the message starts ("Artist").
        item_id: The rating key asked for.
    """

    def __init__(self, kind: str, item_id: str) -> None:
        super().__init__(kind, item_id)
        self.error_code = "ITEM_NOT_FOUND"


class ActionNotAvailableException(InvalidOperationException):
    """Exception raised for a playback command the speaker does not allow now.

    Args:
        operation: The command, phrased to follow "Cannot" ("pause speaker
            RINCON_…").
    """

    def __init__(self, operation: str) -> None:
        super().__init__(operation, "the speaker does not allow it now")
        self.error_code = "ACTION_NOT_AVAILABLE"


class CommandRefusedException(InvalidOperationException):
    """Exception raised for a transport command the speaker refuses because
    it does not allow the command now.

    Args:
        operation: The command, phrased to follow "Cannot" ("stop speaker
            RINCON_…").
        cause: The speaker's refusal, in the Sonos client's words.
    """


class PlaylistNotPlayableException(InvalidOperationException):
    """Exception raised for a playlist the backend does not queue: one
    without tracks, or one of more than ``PLAYLIST_MAX_TRACKS``.

    Args:
        playlist_id: The playlist's rating key.
        cause: What keeps it from the queue ("it holds no tracks").
    """

    def __init__(self, playlist_id: str, cause: str) -> None:
        super().__init__(f"queue playlist {playlist_id}", cause)
        self.error_code = "PLAYLIST_NOT_PLAYABLE"
