"""Tests for the app's own settings."""

import pytest
from pydantic import ValidationError

from app.app_config import AppEnvironment, AppSettings
from app.exceptions import ConfigurationError


def _environment(**values: str) -> AppEnvironment:
    """An environment of exactly these variables: no .env, no process environment."""
    return AppEnvironment.model_validate(values)


class TestAppSettings:
    def test_load_reads_the_sonos_settings(self):
        settings = AppSettings.load(
            _environment(
                SONOS_SEED_HOSTS="192.0.2.1, 192.0.2.2",
                SONOS_EVENT_ADVERTISE_HOST="sonos-events.invalid",
                SONOS_EVENT_PORT="1401",
            )
        )

        assert settings.sonos_seed_hosts == ["192.0.2.1", "192.0.2.2"]
        assert settings.sonos_event_advertise_host == "sonos-events.invalid"
        assert settings.sonos_event_port == 1401

    def test_event_port_defaults_to_1400(self):
        settings = AppSettings.load(
            _environment(
                SONOS_SEED_HOSTS="192.0.2.1",
                SONOS_EVENT_ADVERTISE_HOST="sonos-events.invalid",
            )
        )

        assert settings.sonos_event_port == 1400

    @pytest.mark.parametrize(
        "missing", ["SONOS_SEED_HOSTS", "SONOS_EVENT_ADVERTISE_HOST"]
    )
    def test_a_required_setting_that_is_missing_stops_the_app(
        self, missing, monkeypatch, tmp_path
    ):
        values = {
            "SONOS_SEED_HOSTS": "192.0.2.1",
            "SONOS_EVENT_ADVERTISE_HOST": "sonos-events.invalid",
        }
        del values[missing]
        monkeypatch.delenv(missing)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

        with pytest.raises(ValidationError, match=missing):
            # As create_app() loads them, from the environment; no .env.
            AppSettings.load(AppEnvironment(_env_file=tmp_path / "absent"))

    @pytest.mark.parametrize("hosts", ["", " , "])
    def test_seed_hosts_must_name_a_speaker(self, hosts):
        with pytest.raises(ConfigurationError, match="names no speaker"):
            AppSettings.load(
                _environment(
                    SONOS_SEED_HOSTS=hosts,
                    SONOS_EVENT_ADVERTISE_HOST="sonos-events.invalid",
                )
            )

    def test_a_seed_host_must_be_an_address(self):
        with pytest.raises(ConfigurationError, match="'speaker.invalid'"):
            AppSettings.load(
                _environment(
                    SONOS_SEED_HOSTS="192.0.2.1,speaker.invalid",
                    SONOS_EVENT_ADVERTISE_HOST="sonos-events.invalid",
                )
            )

    def test_the_settings_show_in_the_boot_log(self):
        settings = AppSettings(
            sonos_seed_hosts=["192.0.2.1"],
            sonos_event_advertise_host="sonos-events.invalid",
        )

        assert settings.dump_for_logging()["sonos_event_port"] == "1400"
