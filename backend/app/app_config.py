"""Application-specific configuration.

This module implements app-specific configuration that is separate from the
infrastructure configuration in config.py.
"""

from ipaddress import AddressValueError, IPv4Address
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import dump_config_for_logging
from app.exceptions import ConfigurationError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppEnvironment(BaseSettings):
    """Raw environment variable loading for app-specific settings."""

    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
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


class AppSettings(BaseModel):
    """Application-specific settings.

    Annotate every field that holds a secret ``Annotated[str, Sensitive]``
    (``from app.config import Sensitive``): run.py logs these settings at boot.
    """

    model_config = ConfigDict(from_attributes=True)

    sonos_seed_hosts: list[str]
    sonos_event_advertise_host: str
    sonos_event_port: int = 1400

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
                anything but IPv4 addresses.
        """
        if env is None:
            # pydantic-settings fills the fields from the environment and .env.
            env = AppEnvironment()  # type: ignore[call-arg]
        return cls(
            sonos_seed_hosts=_seed_hosts(env.SONOS_SEED_HOSTS),
            sonos_event_advertise_host=env.SONOS_EVENT_ADVERTISE_HOST,
            sonos_event_port=env.SONOS_EVENT_PORT,
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
