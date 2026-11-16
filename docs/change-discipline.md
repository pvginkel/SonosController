# Change discipline

The rules every code change in this repo obeys. This is the doc `.aiworkflowrc` names as
`design_philosophy`: it is handed to every `code-writer`, `code-reviewer`, `plan-writer` and
`plan-reviewer` the pipeline dispatches, and a reviewer cites it when sending work back. It states
rules; the design they apply to lives in the API's design doc (`docs/api-design.md`).

## Clean breaking changes

Greenfield. Nothing outside this repo imports its code, and the HTTP API has a small, known set of
consumers: the touch-screen displays (firmware in `pvginkel/SonosDisplay`) and, later, a web UI.
The contract between this repo and the firmware is the backend's OpenAPI document as the deployed
backend serves it (`/api/docs/openapi.json`); neither repo reads the other's code. So when an
interface changes, **fix the callers in this repo**: no shim, adapter, compatibility route or
response field that keeps the old shape alive. A change that breaks the firmware's reading of the
OpenAPI document is a breaking change to a consumer nobody here can update in the same commit: say so
in the close-out so the operator can schedule the firmware side.

## No tombstones

Delete replaced code completely. No "moved to X" comments, forwarding stubs, deprecated aliases,
commented-out blocks, dead re-exports or unused settings. Superseded prose is rewritten, not
annotated; git history records what things used to be. `vulture` runs in `kc project lint` to catch
the code half. When it flags something genuinely reachable, the whitelist entry goes in
`backend/vulture_whitelist.py` with the reason, not a `# noqa` at the site.

## No defensive coding

No `try`/`except` that swallows an error, no drop-the-bad-input-and-keep-going path, no null-guard for
a condition the framework already prevents, no silent fallback for configuration the app requires.
Prefer obvious-now failure over silent corruption later.

**Boundary validation is the exception, and it is the point.** Validate what crosses in from outside:
HTTP request bodies, Sonos's answers and event notifications, Plex's answers. Trust what the system
already established. A speaker that is unreachable or a Plex that refuses is a real, expected
condition: it surfaces as an error to the caller, never as a silent default.

## Sonos and Plex stay behind two clients

The speakers own their state: the queue, what plays and the volume live on the speaker, and the
backend stores none of it. Favourites in PostgreSQL are its only stored data.

In memory the backend keeps what the design (`docs/api-design.md`) names, and nothing else: the
mirror of the speakers' state, fed by their events and read again from the speaker at every
subscription (section 2), a small cache of finished artwork by id, size and format
(section 3.7), and each group's queueing tasks in their turns (section 3.4). All are lost at a
restart, and the first two built again. Do not cache anything else of a speaker
or of the library to save a round trip: the library is read from Plex on each request.

The backend reaches Sonos and Plex only through two thin client services in the DI container:
`sonos_client`, wrapping SoCo (`backend/app/services/sonos_client.py`), and `plex_client`, wrapping
plexapi (`backend/app/services/plex_client.py`). Nothing else imports SoCo or plexapi. This is what
makes the next section possible.

## Testability is critical

Every change ships with a test. "I verified it by hand" is not a substitute: the test runs again next
time. The suite is pytest under `backend/tests/`, run as `kc project test`, against a real
PostgreSQL and the real SSE machinery.

**Sonos and Plex are the one thing that is faked: the one exception to the modern app guide's
TEST-1 (no mocks or fakes), approved for this project at its onboarding.** CI has no speaker and no
Plex. pytest overrides the two client providers with hand-written in-memory fakes that keep state,
in `backend/tests/fakes/` (a household's rooms with their queues, transport, volume and groups;
Plex's library of artists, albums, tracks and playlists, with their pictures). Never `MagicMock`,
never `monkeypatch` of SoCo or plexapi internals, and no other fake anywhere: everything above the
clients is real. One test is the named exception:
`test_the_soco_client_asks_a_speaker_while_soco_holds_a_topology_subscription` in
`backend/tests/services/test_sonos_client.py` sets SoCo's state by hand (a SoCo instance's private
household id, a stand-in subscription in SoCo's registry, the client's private entry), because it is
the only test that fails when the client answers a layout read from SoCo's cache while a topology
subscription is held, and the live suite cannot fail on that.

What the fakes cannot prove is what the real clients send, so a separate **live suite**,
`backend/tests/live/`, marked `live` and deselected by default, runs the real clients against the
Garage speaker and the real Plex from a KubeCoder environment. CI never runs it. Its conftest checks
both systems at session start and stops with `pytest.exit()` and a message saying how to fix it,
never a skip (TEST-2). `docs/slice-test-plan.md`, step 3, has how it is run.

Live rules, standing for every session: use the **Garage** speaker only, at low volume, and leave its
queue, position, volume and grouping as found. **Zitkuil** (a room of three devices) is a living
space and is never touched. The Plex token reaches the environment as `LIVE_PLEX_TOKEN`: never print
it, test presence with `[ -n "${LIVE_PLEX_TOKEN:+x}" ]`. A session that subscribes to a speaker's
events unsubscribes before it ends, and a probe that subscribes other than through the app's Sonos
client asks a lease of minutes (SoCo's `requested_timeout`, UPnP's `TIMEOUT: Second-300`). A
subscription left behind whose callback no longer answers holds up the speaker's notifications to
every subscriber, prd's backend included, until its lease runs out, 24 hours when none is asked;
the speaker then refuses its `UNSUBSCRIBE` (`docs/api-design.md` 1.1).

## Never hand-edit generated artifacts

The OpenAPI document is generated from the code's schemas. An API change is a change to the
schemas and their descriptions, never to a copy of the document. Lockfiles (`uv.lock`, root and
`backend/`) change through `uv`, never by hand. Files the ModernAppTemplate owns are not modified
for app behaviour; `CLAUDE.md` lists the extension points.

## This is a public repo

`github.com/pvginkel/SonosController` is world-readable. The spec repo and the deploy repo are
private. No secrets, tokens, internal hostnames or addresses, speaker or Plex addresses, and no
non-public names in code, tests, fixtures or commit messages: they belong in the environment and the
deploy repo. Assume every line is read by someone outside the homelab.
