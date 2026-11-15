# Slice documentation plan

Which documentation surfaces a shipped slice brings up to date. This is the doc `.aiworkflowrc`
names as `doc_phase.plan`: the doc phase is "read this and execute it", working from the whole
slice's merged diff. Each surface below is owed an update only when the diff reached it.

## The surfaces

1. **`CLAUDE.md`** is the orientation page every session loads, every turn. It stays at about one
   screen and states each fact once. A slice rarely touches it; when a standing rule genuinely
   belongs there, something else moves out to a `docs/` topic doc. Nothing the pipeline reads by
   machine goes in it (that is `.aiworkflowrc`). Its "Architecture model" section is kept current
   centrally, not by this phase.
2. **`docs/`** holds the topic docs: the three procedure docs (`change-discipline.md`,
   `slice-test-plan.md`, `slice-doc-plan.md`) and the design. A slice that changed how the repo is
   verified, documented or changed updates the matching procedure doc. A design worth writing down
   gets a topic doc linked from `CLAUDE.md`.
3. **The API's design doc, `docs/api-design.md`**, owns the API's intended shape. A slice that
   changed an endpoint, a status value, an error or the SSE events brings it up to date.
4. **The OpenAPI document itself**, served at `/api/docs/openapi.json`, is the contract with the
   displays' firmware (`pvginkel/SonosDisplay`), which reads it as the deployed backend serves it.
   It is generated from the code's schemas, so it is edited there: a slice that changed an endpoint
   keeps its schema and field descriptions right and complete. Never edit a copy of the document.

There is no manual, no frontend and no README beyond `backend/README.md`, which is owed an update
only when how to run the service changed.

## What "up to date" means

**State the design as it is**, as implemented. Where the implementation diverged from the plan, the
doc describes what shipped. No changelog entries, no "as of slice NNN", no tombstones: rewrite the
doc, per [`change-discipline.md`](change-discipline.md). **Ground every claim in the shipped
source**; a sentence that cannot be checked against the merged tree does not go in.

The doc is public: no hostnames, addresses or tokens of the operator's network.

## Gates

The doc phase must not move code:

```bash
kc project lint       # still green
```

Check that relative links resolve, then commit.

## When there is little to do

A slice that changed no design, convention or reader-facing surface owes nothing here, and saying so
plainly is the correct outcome. Do not invent doc work.
