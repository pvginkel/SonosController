#!/usr/bin/env python3
"""Give backend/.env the settings the app cannot start without.

`kc project setup` runs this before anything builds the app. It adds what
.env lacks and keeps every line .env holds, so it is safe on a fresh checkout
and on a .env an earlier setup or a developer wrote.

No value is in this repository, which is public. Each comes from where it
lives:

- DATABASE_URL: the environment's PostgreSQL sidecar, the app's own default.
- SONOS_SEED_HOSTS: `sonos.seedHosts` in the deploy repo's prd values, in the
  SonosControllerDeploy checkout beside this repository.
- SONOS_EVENT_ADVERTISE_HOST: this environment's own DNS name, as
  `kc env describe` gives it, where its port `upnp-events` is published. Never
  prd's events Service: the speakers would call production.
- PLEX_URL and PLEX_LIBRARY: `plex.url` and `plex.library` in the same prd
  values.
- PLEX_TOKEN: this environment's secret LIVE_PLEX_TOKEN.

A value it cannot find stops it, with where the value comes from. It prints
the names it adds, never a value.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml
from dotenv import dotenv_values

_BACKEND_DIR = Path(__file__).resolve().parent.parent

DEFAULT_ENV_FILE = _BACKEND_DIR / ".env"
DEFAULT_DEPLOY_VALUES = (
    _BACKEND_DIR.parent.parent / "SonosControllerDeploy" / "config" / "prd" / "values.yaml"
)

# Keep in step with Settings.database_url in app/config.py.
DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/sonos_controller"


class SetupError(Exception):
    """A value setup could not find; the message says where it comes from."""


def _deploy_value(deploy_values: Path, name: str, key: str) -> object:
    """What the deploy repo's prd values hold at ``key``, a dotted path."""
    if not deploy_values.is_file():
        raise SetupError(
            f"{name} comes from {key} in {deploy_values}, and that file is "
            "missing. It is in the SonosControllerDeploy checkout that "
            ".kubecoder/config.yaml lists under repos."
        )
    value: object = yaml.safe_load(deploy_values.read_text(encoding="utf-8"))
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def seed_hosts(deploy_values: Path) -> str:
    """SONOS_SEED_HOSTS: the speakers' addresses in the deploy repo's prd values."""
    hosts = _deploy_value(deploy_values, "SONOS_SEED_HOSTS", "sonos.seedHosts")
    if not isinstance(hosts, list) or not hosts:
        raise SetupError(
            f"SONOS_SEED_HOSTS comes from sonos.seedHosts in {deploy_values}, "
            "and that list is missing or empty. Bring the SonosControllerDeploy "
            "checkout up to date."
        )
    return ",".join(str(host) for host in hosts)


def plex_value(deploy_values: Path, name: str, key: str) -> str:
    """PLEX_URL and PLEX_LIBRARY: Plex's address and its music library in the
    deploy repo's prd values."""
    value = _deploy_value(deploy_values, name, key)
    if not isinstance(value, str) or not value:
        raise SetupError(
            f"{name} comes from {key} in {deploy_values}, and that value is "
            "missing or empty. Bring the SonosControllerDeploy checkout up to "
            "date."
        )
    return value


def plex_token(environ: Mapping[str, str]) -> str:
    """PLEX_TOKEN: the environment's secret LIVE_PLEX_TOKEN."""
    token = environ.get("LIVE_PLEX_TOKEN")
    if not token:
        raise SetupError(
            "PLEX_TOKEN comes from this environment's LIVE_PLEX_TOKEN, which "
            ".kubecoder/config.yaml lists under secrets, and it is not set. "
            "Outside a KubeCoder environment, set PLEX_TOKEN in .env to a token "
            "of the Plex server."
        )
    return token


def environment_dns_name() -> str:
    """SONOS_EVENT_ADVERTISE_HOST: the name this environment's ports are published on."""
    source = "this environment's DNS name, which `kc env describe` gives"
    try:
        described = subprocess.run(
            ["kc", "env", "describe", "--output=json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(
            f"SONOS_EVENT_ADVERTISE_HOST comes from {source}, and that command "
            f"failed: {exc}. Outside a KubeCoder environment, set it in .env to "
            "a name or address the speakers reach this machine's port 1400 on."
        ) from exc
    name = json.loads(described.stdout).get("dnsName")
    if not name:
        raise SetupError(
            f"SONOS_EVENT_ADVERTISE_HOST comes from {source}, and it gave none."
        )
    return str(name)


def write_dev_env(
    env_file: Path,
    deploy_values: Path,
    dns_name: Callable[[], str],
    environ: Mapping[str, str],
) -> list[str]:
    """Append the settings ``env_file`` lacks. Returns the names it added."""
    sources: dict[str, Callable[[], str]] = {
        "DATABASE_URL": lambda: DATABASE_URL,
        "SONOS_SEED_HOSTS": lambda: seed_hosts(deploy_values),
        "SONOS_EVENT_ADVERTISE_HOST": dns_name,
        "PLEX_URL": lambda: plex_value(deploy_values, "PLEX_URL", "plex.url"),
        "PLEX_LIBRARY": lambda: plex_value(
            deploy_values, "PLEX_LIBRARY", "plex.library"
        ),
        "PLEX_TOKEN": lambda: plex_token(environ),
    }
    held = dotenv_values(env_file) if env_file.exists() else {}
    added = {name: source() for name, source in sources.items() if name not in held}
    if added:
        text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        text += "".join(f"{name}={value}\n" for name, value in added.items())
        env_file.write_text(text, encoding="utf-8")
    return list(added)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--deploy-values", type=Path, default=DEFAULT_DEPLOY_VALUES)
    args = parser.parse_args(argv)
    try:
        added = write_dev_env(
            args.env_file, args.deploy_values, environment_dns_name, os.environ
        )
    except SetupError as exc:
        print(f"write_dev_env: {exc}", file=sys.stderr)
        return 1
    if added:
        print(f"{args.env_file}: added {', '.join(added)}")
    else:
        print(f"{args.env_file}: nothing to add")
    return 0


if __name__ == "__main__":
    sys.exit(main())
