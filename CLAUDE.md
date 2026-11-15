# SonosController

Plays music from a Plex library on Sonos speakers and serves the touch-screen displays that control them

## Repo structure

- **Root** — the `run-suite` orchestrator (`tools/suite_runner/`, CI's single entry point), the
  shared `Procfile.dev` dev stack and its `scripts/dev.py` launcher, the `Jenkinsfile`, and the
  `Jenkinsfile.architecture` that publishes the architecture model in `docs/architecture/`.
- **`backend/`** — the Flask API (Python, uv).

The app is backend-only: the root template's `use_frontend` is off, so it has no `frontend/` and
CI, the dev stack and the environment have no frontend in them. A web UI is added later by
generating `frontend/` from the frontend template and turning `use_frontend` on at the root with
`copier update` (ModernAppTemplate's `docs/copier_approach.md`, "Backend-only apps", has the steps).

Everything builds through `kc project build|test|lint|setup`, run **from the repo root** — those
verbs are cwd-bound. The toolchain lives in the `modern-app` sidecar, so ad-hoc `uv` commands need
`cexec modern-app`. The root and `backend/` are two uv projects, each with its own `uv.lock`;
`kc project setup` writes both on a fresh app, and they are committed, because CI and the image sync
`--locked`. `scripts/dev.py` starts the dev stack and `scripts/dev.py stop` stops it from another
shell. `cexec modern-app python3 scripts/check_updates.py` reports the dependencies with a newer
release, major, minor and patch, in every component it finds.

## Template ownership

The root and `backend/` are each generated from a ModernAppTemplate Copier template
(`.copier-answers.yml` in each). Files are either **template-owned** (overwritten by
`copier update`) or **app-owned** (generated once, never overwritten). Never modify a
template-owned file to add app behaviour; use the extension points instead:

| Need | Put it in |
|------|-----------|
| New API endpoints | `backend/app/api/`, registered in `register_blueprints()` in `backend/app/startup.py` |
| New services | `backend/app/services/`, wired in `backend/app/services/container.py` |
| Side effects after a request's commit | `after_commit()` from `backend/app/utils/after_commit.py` |
| CLI commands, post-migration logic | `backend/app/startup.py` |
| Test fixtures | `backend/tests/conftest.py` |
| Sidecars CI must wait for | `backend/scripts/wait-for-services.py` (the suite runner calls it) |

If the template lacks an extension point you need, add it to the template first.

## Architecture model

SonosController is a producer in the estate's federated architecture model. Its model is
hand-authored in `docs/architecture/*.yaml`. `Jenkinsfile.architecture`, the job
`AaC/SonosController`, validates it against the architecture service on every push and archives it
for AaC/Architecture to collect; `kc project lint` validates it too (`arch-validate`, in the
`aac-tools` sidecar).

The model is kept current centrally, not from here: the central architecture update in
`pvginkel/Architecture` (`tooling/fleet.py update`) reads the commits since the model last changed
and commits what they change in it. Nothing needs to run here after a change.

**While `docs/architecture/` holds no model, nudge the operator to seed it.** A new app starts
without one, and the AaC job has nothing to validate until it exists. In order:

1. Seed the model with the `/seed-architecture` skill. It is not installed here: it comes with the
   kit that `python3 tooling/fleet.py stage SonosController`, run in the Architecture repo, stages
   into a clone of this repo. It is interactive (the operator triages the candidate elements), so it
   runs in a session of its own.
2. Commit and push the model.
3. The operator creates the job `AaC/SonosController` through the Jenkins API, from the pipeline
   guide's `config.xml` template (Script Path `Jenkinsfile.architecture`), and registers the
   producer in Architecture's `pipeline-producers.yaml` (`repo: pvginkel/SonosController`,
   `jenkinsJob: AaC/SonosController`).

`Jenkinsfile.architecture` is the app's own, and `copier update` leaves it alone.
