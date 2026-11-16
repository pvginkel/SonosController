"""Tests for the app's own settings."""

import pytest
from pydantic import ValidationError

from app.app_config import AppEnvironment, AppSettings
from app.exceptions import ConfigurationError

REQUIRED = {
    "SONOS_SEED_HOSTS": "192.0.2.1",
    "SONOS_EVENT_ADVERTISE_HOST": "sonos-events.invalid",
    "PLEX_URL": "http://192.0.2.1:32400",
    "PLEX_TOKEN": "a-plex-token",
}


def _environment(**values: str) -> AppEnvironment:
    """An environment of the required variables and these. They go before
    the process environment and .env, which still give a variable not named
    here."""
    return AppEnvironment.model_validate(REQUIRED | values)


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
        settings = AppSettings.load(_environment())

        assert settings.sonos_event_port == 1400

    def test_load_reads_the_plex_settings(self):
        settings = AppSettings.load(
            _environment(
                PLEX_URL=" http://192.0.2.2:32400 ",
                PLEX_TOKEN="another-token",
                PLEX_LIBRARY="Music",
            )
        )

        assert settings.plex_url == "http://192.0.2.2:32400"
        assert settings.plex_token == "another-token"
        assert settings.plex_library == "Music"

    @pytest.mark.parametrize("values", [{}, {"PLEX_LIBRARY": ""}])
    def test_the_plex_library_is_optional(self, values, monkeypatch, tmp_path):
        monkeypatch.delenv("PLEX_LIBRARY")

        settings = AppSettings.load(
            AppEnvironment(_env_file=tmp_path / "absent", **(REQUIRED | values))
        )

        assert settings.plex_library is None

    def test_the_largest_playlist_is_1000_tracks_unless_set(self):
        assert AppSettings.load(_environment()).playlist_max_tracks == 1000
        assert (
            AppSettings.load(
                _environment(PLAYLIST_MAX_TRACKS="250")
            ).playlist_max_tracks
            == 250
        )

    @pytest.mark.parametrize("value", ["0", "-1", "many", ""])
    def test_a_largest_playlist_that_is_no_count_stops_the_app(self, value):
        with pytest.raises(ValidationError, match="PLAYLIST_MAX_TRACKS"):
            _environment(PLAYLIST_MAX_TRACKS=value)

    @pytest.mark.parametrize("missing", sorted(REQUIRED))
    def test_a_required_setting_that_is_missing_stops_the_app(
        self, missing, monkeypatch, tmp_path
    ):
        monkeypatch.delenv(missing)
        for name, value in REQUIRED.items():
            if name != missing:
                monkeypatch.setenv(name, value)

        with pytest.raises(ValidationError, match=missing) as raised:
            # As create_app() loads them, from the environment; no .env.
            AppSettings.load(AppEnvironment(_env_file=tmp_path / "absent"))

        assert "a-plex-token" not in str(raised.value)

    @pytest.mark.parametrize("name", ["PLEX_URL", "PLEX_TOKEN"])
    @pytest.mark.parametrize("value", ["", " "])
    def test_an_empty_plex_setting_stops_the_app(self, name, value):
        with pytest.raises(ConfigurationError, match=f"{name} is empty"):
            AppSettings.load(_environment(**{name: value}))

    @pytest.mark.parametrize("value", ["", " "])
    def test_an_empty_advertise_host_stops_the_app(self, value):
        with pytest.raises(
            ConfigurationError, match="SONOS_EVENT_ADVERTISE_HOST is empty"
        ):
            AppSettings.load(_environment(SONOS_EVENT_ADVERTISE_HOST=value))

    @pytest.mark.parametrize("hosts", ["", " , "])
    def test_seed_hosts_must_name_a_speaker(self, hosts):
        with pytest.raises(ConfigurationError, match="names no speaker"):
            AppSettings.load(_environment(SONOS_SEED_HOSTS=hosts))

    def test_a_seed_host_must_be_an_address(self):
        with pytest.raises(ConfigurationError, match="'speaker.invalid'"):
            AppSettings.load(
                _environment(SONOS_SEED_HOSTS="192.0.2.1,speaker.invalid")
            )

    def test_the_settings_show_in_the_boot_log(self):
        settings = AppSettings.load(_environment())

        assert settings.dump_for_logging()["sonos_event_port"] == "1400"
        assert settings.dump_for_logging()["plex_url"] == "'http://192.0.2.1:32400'"

    def test_the_plex_token_shows_nowhere(self):
        settings = AppSettings.load(_environment())

        assert settings.dump_for_logging()["plex_token"] == "***"
        assert "a-plex-token" not in repr(settings)
        assert "a-plex-token" not in str(settings)
