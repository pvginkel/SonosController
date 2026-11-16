# sonos-controller

Plays music from a Plex library on Sonos speakers and serves the touch-screen displays that control them

Flask backend generated from the ModernAppBackendTemplate. See `CLAUDE.md` in
the repository root for how to run, test and lint it.

## Settings

The app does not start without the speakers' and Plex's settings (`app/app_config.py`;
`docs/api-design.md` section 6 says what each holds). No value is in this repository, which is
public. `kc project setup` runs `scripts/write_dev_env.py`, which adds to the git-ignored `.env`
each setting it lacks and keeps every line it holds:

| Setting | From |
|---|---|
| `DATABASE_URL` | The environment's PostgreSQL sidecar. |
| `SONOS_SEED_HOSTS` | `sonos.seedHosts` in `config/prd/values.yaml` of the SonosControllerDeploy checkout beside this repository. |
| `SONOS_EVENT_ADVERTISE_HOST` | This environment's own DNS name, where its port `upnp-events` is published. Never prd's events Service: the speakers would call production. |
| `PLEX_URL`, `PLEX_LIBRARY` | `plex.url` and `plex.library` in the same values. |
| `PLEX_TOKEN` | The environment's secret `LIVE_PLEX_TOKEN`. |

To take a value that changed, remove its line from `.env` and run setup again.

The dev stack and the live suite both listen for the speakers' events on `SONOS_EVENT_PORT`,
1400 by default, so one of them runs at a time.

A new setting the app cannot start without goes into `scripts/write_dev_env.py`'s sources, and
into `tests/conftest.py`: its `test_app_settings` fixture, and the `os.environ` lines that keep a
process the suite starts off the household `.env` names.

## Tests

`kc project test` runs pytest with in-memory fakes of the Sonos and the Plex client
(`tests/fakes/`). The live suite, `tests/live/`, runs the real clients against the Garage speaker
and the real Plex. CI never runs it. From `backend/`, with the dev stack stopped:

```bash
cexec python uv run pytest -m live tests/live
```

Its conftest stops the run where the speakers or Plex cannot take it, and skips the tests that
command the Garage, with the state it found, where the Garage is in use or not set up for them;
`docs/change-discipline.md` has the rules for the real speakers.
