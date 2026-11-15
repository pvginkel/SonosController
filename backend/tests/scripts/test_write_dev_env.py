"""Tests for the script that gives backend/.env what the app requires."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "write_dev_env.py"

# scripts/ is not a package: the script is loaded from its file.
_spec = importlib.util.spec_from_file_location("write_dev_env", _SCRIPT)
_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_script)

DATABASE_URL = _script.DATABASE_URL
SetupError = _script.SetupError
environment_dns_name = _script.environment_dns_name
main = _script.main
seed_hosts = _script.seed_hosts
write_dev_env = _script.write_dev_env

_VALUES = """\
stage: prd
sonos:
  seedHosts:
    - 192.0.2.10
    - 192.0.2.11
  events:
    host: events.invalid
"""


@pytest.fixture
def deploy_values(tmp_path: Path) -> Path:
    path = tmp_path / "values.yaml"
    path.write_text(_VALUES, encoding="utf-8")
    return path


def _dns_name() -> str:
    return "environment.invalid"


def _no_dns_name() -> str:
    raise AssertionError("the environment's name was asked for")


class TestWriteDevEnv:
    def test_a_fresh_checkout_gets_every_setting(self, tmp_path, deploy_values):
        env_file = tmp_path / ".env"

        added = write_dev_env(env_file, deploy_values, _dns_name)

        assert added == [
            "DATABASE_URL",
            "SONOS_SEED_HOSTS",
            "SONOS_EVENT_ADVERTISE_HOST",
        ]
        assert dotenv_values(env_file) == {
            "DATABASE_URL": DATABASE_URL,
            "SONOS_SEED_HOSTS": "192.0.2.10,192.0.2.11",
            "SONOS_EVENT_ADVERTISE_HOST": "environment.invalid",
        }

    def test_an_earlier_setups_env_keeps_its_lines_and_gains_the_rest(
        self, tmp_path, deploy_values
    ):
        env_file = tmp_path / ".env"
        earlier = "# mine\nDATABASE_URL=postgresql+psycopg://elsewhere/db"
        env_file.write_text(earlier, encoding="utf-8")

        added = write_dev_env(env_file, deploy_values, _dns_name)

        assert added == ["SONOS_SEED_HOSTS", "SONOS_EVENT_ADVERTISE_HOST"]
        assert env_file.read_text(encoding="utf-8") == (
            earlier
            + "\nSONOS_SEED_HOSTS=192.0.2.10,192.0.2.11"
            + "\nSONOS_EVENT_ADVERTISE_HOST=environment.invalid\n"
        )

    def test_a_value_the_env_holds_is_kept_and_its_source_not_asked(self, tmp_path):
        env_file = tmp_path / ".env"
        held = (
            f"DATABASE_URL={DATABASE_URL}\n"
            "SONOS_SEED_HOSTS=192.0.2.99\n"
            "SONOS_EVENT_ADVERTISE_HOST=mine.invalid\n"
        )
        env_file.write_text(held, encoding="utf-8")

        added = write_dev_env(env_file, tmp_path / "absent.yaml", _no_dns_name)

        assert added == []
        assert env_file.read_text(encoding="utf-8") == held

    def test_missing_deploy_values_say_where_the_seeds_come_from(self, tmp_path):
        absent = tmp_path / "absent.yaml"

        with pytest.raises(SetupError) as raised:
            seed_hosts(absent)

        message = str(raised.value)
        assert "SONOS_SEED_HOSTS comes from sonos.seedHosts in" in message
        assert str(absent) in message
        assert "SonosControllerDeploy" in message

    @pytest.mark.parametrize(
        "values", ["", "sonos: {}\n", "sonos:\n  seedHosts: []\n"]
    )
    def test_values_without_seeds_say_so(self, tmp_path, values):
        path = tmp_path / "values.yaml"
        path.write_text(values, encoding="utf-8")

        with pytest.raises(SetupError, match="that list is missing or empty"):
            seed_hosts(path)

    def test_nothing_is_written_when_a_value_cannot_be_found(self, tmp_path):
        env_file = tmp_path / ".env"

        with pytest.raises(SetupError):
            write_dev_env(env_file, tmp_path / "absent.yaml", _dns_name)

        assert not env_file.exists()

    def test_without_kc_the_callback_name_says_where_it_comes_from(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PATH", str(tmp_path))

        with pytest.raises(SetupError) as raised:
            environment_dns_name()

        message = str(raised.value)
        assert "SONOS_EVENT_ADVERTISE_HOST comes from this environment's DNS name" in message
        assert "kc env describe" in message

    def test_main_prints_names_and_no_values(self, tmp_path, deploy_values, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "SONOS_EVENT_ADVERTISE_HOST=mine.invalid\n", encoding="utf-8"
        )

        status = main(
            ["--env-file", str(env_file), "--deploy-values", str(deploy_values)]
        )

        output = capsys.readouterr().out
        assert status == 0
        assert "added DATABASE_URL, SONOS_SEED_HOSTS" in output
        assert "192.0.2" not in output
        assert main(
            ["--env-file", str(env_file), "--deploy-values", str(deploy_values)]
        ) == 0
        assert "nothing to add" in capsys.readouterr().out

    def test_the_script_stops_setup_with_the_message(self, tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--env-file",
                str(tmp_path / ".env"),
                "--deploy-values",
                str(tmp_path / "absent.yaml"),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(tmp_path)},
        )

        assert result.returncode == 1
        assert "SONOS_SEED_HOSTS comes from sonos.seedHosts in" in result.stderr
        assert not (tmp_path / ".env").exists()
