# Slice testing strategy

How a slice is proven once its phases are merged. This is the doc `.aiworkflowrc` names as
`test_phase.strategy`: the run loop's test phase is "read this and execute it". Read it top to
bottom and do what it says.

## What this phase proves

**Verification is local, then the push.** This repo has one deployment, production, and no dev
instance. CI (Jenkins job `SonosController`) runs the backend suite in a validation Job with a
PostgreSQL sidecar, builds `registry:5000/sonos-controller:<n>`, and pins it into the private deploy
repo `pvginkel/SonosControllerDeploy` (`config/prd/values.yaml`, `images.sonosController`), which
Argo CD's Application `sonos-controller-prd` auto-syncs. **The push to main is the production
deploy**, so everything is verified before the push.

No gate is known red: a failure in step 1 is this slice's and blocks.

The backend has no authentication by design, so there is no sign-in to arrange anywhere below.

## 0. Preconditions

The driver has ff-merged every code phase into the base branch. Confirm `git status --short` is
clean: a dirty tree means an earlier phase left something behind, and that is a finding. Run every
`kc project` verb from the repo root; the CLI is cwd-bound.

## 1. The suites

```bash
kc project test       # backend pytest, against PostgreSQL, with the in-memory Sonos and Plex fakes
kc project lint       # root ruff; backend ruff + mypy + vulture; the architecture model
```

Both must be green. `kc project build` has no verb of its own in this repo.

## 2. The live check

Do this when the slice touched `backend/`, `Procfile.dev` or `scripts/`; a docs-only slice skips to
step 4 and says so.

Boot the dev stack from the repo root (it runs honcho inside the `python` sidecar and writes
`logs/dev.pid` and `logs/backend.log`), then probe the backend on port 3601:

```bash
mkdir -p logs
nohup scripts/dev.py > logs/dev-stack.log 2>&1 &
# Wait for the boot banner (dev.py truncates logs/backend.log at start); never poll the SSE stream.
for i in $(seq 1 30); do grep -q 'Running on' logs/backend.log 2>/dev/null && break; sleep 2; done
for u in http://localhost:3601/health/readyz http://localhost:3601/health/healthz \
         http://localhost:3601/api/docs/openapi.json; do
  printf '%-50s ' "$u"; curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 "$u"
done
```

Every one answers `200`. Add one real endpoint of the slice's own, with `curl` and the result
checked. Then stop the stack from another shell, as `scripts/dev.py` documents:

```bash
scripts/dev.py stop
```

It signals the script whose pid is in `logs/dev.pid` and forwards a Ctrl-C to honcho. Do not kill
the `cexec` client: honcho dies without stopping its services. Confirm port 3601 is free, that
`cexec python ps -eo pid,ppid,etime,args --no-headers` lists no `honcho`, `watchmedo` or `run.py`
left behind, and that `git status --short` is clean (`logs/` is git-ignored).

## 3. The live suite

Whenever the slice changes what a client sends to Sonos or Plex, run the live suite,
`backend/tests/live/` (marker `live`), with step 2's dev stack stopped: both listen for the
speakers' events on port 1400. It runs on the settings `kc project setup` wrote to `backend/.env`.

```bash
[ -n "${LIVE_PLEX_TOKEN:+x}" ] && echo token present
cexec python sh -c 'cd backend && uv run pytest -m live tests/live'
```

The suite's conftest checks both systems at session start and stops with `pytest.exit()` and a
message on how to fix what is missing (never a skip); follow the message. It wants the Garage alone
in its group, stopped, on a queue of two tracks or more at a track of the Plex library. One test
also wants the Garage at the first or the last track of that queue, which the conftest does not
check: queue an album of the Plex library from the Sonos app and stop it, and the Garage is fit for
all of them. If it cannot run, the criteria that need it are *not verified*: say so, never pass them
by inspection.

Standing rules, here and in every check against the real systems: the **Garage** speaker only, at
low volume, with its queue, position, volume and grouping left as found. **Zitkuil** is never
touched. Never print the Plex token.

## 4. Check off `verification.json`

Mark each acceptance criterion with the evidence that settled it: the command run and what it
returned. A criterion nothing in steps 1-3 exercised is an untested criterion (a finding) or a check
missing from this doc, not "passed by inspection".

## 5. Push, then follow the build

Pushing is this phase's job. Rebase onto `origin/main` first (docs may land concurrently), then push,
honouring any repo named in `plan.md`'s `## Push holds`. **This is the production deploy.**

```bash
git fetch origin && git rebase origin/main
git push origin HEAD:main
~/.local/bin/track_build.py SonosController --hash "$(git rev-parse HEAD)"
```

`track_build.py` follows the build, the pipelines it triggers and the Argo CD roll to the end; its
summary and exit status say how each went (`--help` has the rest). A `NOT_BUILT` build superseded by
a newer push is not a failure: read the build that replaced it. A red build is a blocking finding.

Then check the deployed backend. The host is the `host` in SonosControllerDeploy's
`config/prd/values.yaml` (checked out beside this repo at `/work/SonosControllerDeploy`); it is never
written in this public repo.

```bash
H=$(sed -n 's/^host: *//p' /work/SonosControllerDeploy/config/prd/values.yaml)
curl -sk -o /dev/null -w '%{http_code}\n' "https://$H/health/readyz"
```

It answers `200`. Then the slice's own checks against prd, read-only wherever the slice allows and
bound by the Garage and Zitkuil rule above.

## Findings

Blocking findings come back as appended phases; sub-bar findings go in the close-out report. A check
that cannot be run is reported as *not verified*, never as passed.
