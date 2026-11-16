"""Application-specific configuration.

This module implements app-specific configuration that is separate from the
infrastructure configuration in config.py.
"""

from ipaddress import AddressValueError, IPv4Address
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Sensitive, dump_config_for_logging
from app.exceptions import ConfigurationError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppEnvironment(BaseSettings):
    """Raw environment variable loading for app-specific settings."""

    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        # A validation error names the values it was given, PLEX_TOKEN among
        # them, and the boot log and the live suite print that error.
        hide_input_in_errors=True,
    )

    SONOS_SEED_HOSTS: str = Field(
        description="IPv4 addresses of one or more speakers, comma-separated"
    )
    SONOS_EVENT_ADVERTISE_HOST: str = Field(
        description="The name or address the speakers call back with their events"
    )
    SONOS_EVENT_PORT: int = Field(
        default=1400,
        description="The port the backend listens on for speaker events, and advertises",
    )
    PLEX_URL: str = Field(description="The Plex server's address")
    PLEX_TOKEN: str = Field(description="The Plex token")
    PLEX_LIBRARY: str | None = Field(
        default=None,
        description="The music library's name; optional when the server has one",
    )
    PLAYLIST_MAX_TRACKS: int = Field(
        default=1000,
        ge=1,
        description="The largest playlist, in tracks, that is shown and queued",
    )


class AppSettings(BaseModel):
    """Application-specific settings.

    Annotate every field that holds a secret ``Annotated[str, Sensitive]``
    (``from app.config import Sensitive``): run.py logs these settings at boot.
    """

    model_config = ConfigDict(from_attributes=True)

    sonos_seed_hosts: list[str]
    sonos_event_advertise_host: str
    sonos_event_port: int = 1400
    plex_url: str
    plex_token: Annotated[str, Sensitive] = Field(repr=False)
    plex_library: str | None = None
    playlist_max_tracks: int = 1000

    def dump_for_logging(self) -> dict[str, str]:
        """Every setting, for the boot log, with the Sensitive ones as ``***``."""
        return dump_config_for_logging(self)

    @classmethod
    def load(
        cls, env: "AppEnvironment | None" = None, flask_env: str = "development"
    ) -> "AppSettings":
        """Load app settings from environment variables.

        Raises:
            ConfigurationError: If SONOS_SEED_HOSTS names no speaker, or holds
                anything but IPv4 addresses; if SONOS_EVENT_ADVERTISE_HOST,
                PLEX_URL or PLEX_TOKEN is empty.
        """
        if env is None:
            # pydantic-settings fills the fields from the environment and .env.
            env = AppEnvironment()  # type: ignore[call-arg]
        return cls(
            sonos_seed_hosts=_seed_hosts(env.SONOS_SEED_HOSTS),
            # SoCo advertises the pod's own address in place of an empty one,
            # which the speakers cannot reach (docs/api-design.md section 1.1).
            sonos_event_advertise_host=_not_empty(
                "SONOS_EVENT_ADVERTISE_HOST", env.SONOS_EVENT_ADVERTISE_HOST
            ),
            sonos_event_port=env.SONOS_EVENT_PORT,
            plex_url=_not_empty("PLEX_URL", env.PLEX_URL),
            plex_token=_not_empty("PLEX_TOKEN", env.PLEX_TOKEN),
            plex_library=env.PLEX_LIBRARY or None,
            playlist_max_tracks=env.PLAYLIST_MAX_TRACKS,
        )


def _seed_hosts(value: str) -> list[str]:
    """Split SONOS_SEED_HOSTS. SoCo reaches a speaker by address, never by name."""
    hosts = [host.strip() for host in value.split(",") if host.strip()]
    if not hosts:
        raise ConfigurationError("SONOS_SEED_HOSTS names no speaker")
    for host in hosts:
        try:
            IPv4Address(host)
        except AddressValueError as exc:
            raise ConfigurationError(
                f"SONOS_SEED_HOSTS holds {host!r}, which is not an IPv4 address"
            ) from exc
    return hosts


def _not_empty(name: str, value: str) -> str:
    if not value.strip():
        raise ConfigurationError(f"{name} is empty")
    return value.strip()
