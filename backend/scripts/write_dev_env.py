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

A value it cannot find stops it, with where the value comes from. It prints
the names it adds, never a value.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
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


def seed_hosts(deploy_values: Path) -> str:
    """SONOS_SEED_HOSTS: the speakers' addresses in the deploy repo's prd values."""
    source = f"sonos.seedHosts in {deploy_values}"
    if not deploy_values.is_file():
        raise SetupError(
            f"SONOS_SEED_HOSTS comes from {source}, and that file is missing. "
            "It is in the SonosControllerDeploy checkout that "
            ".kubecoder/config.yaml lists under repos."
        )
    values = yaml.safe_load(deploy_values.read_text(encoding="utf-8")) or {}
    hosts = (values.get("sonos") or {}).get("seedHosts")
    if not isinstance(hosts, list) or not hosts:
        raise SetupError(
            f"SONOS_SEED_HOSTS comes from {source}, and that list is missing or "
            "empty. Bring the SonosControllerDeploy checkout up to date."
        )
    return ",".join(str(host) for host in hosts)


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
    env_file: Path, deploy_values: Path, dns_name: Callable[[], str]
) -> list[str]:
    """Append the settings ``env_file`` lacks. Returns the names it added."""
    sources: dict[str, Callable[[], str]] = {
        "DATABASE_URL": lambda: DATABASE_URL,
        "SONOS_SEED_HOSTS": lambda: seed_hosts(deploy_values),
        "SONOS_EVENT_ADVERTISE_HOST": dns_name,
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
        added = write_dev_env(args.env_file, args.deploy_values, environment_dns_name)
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
