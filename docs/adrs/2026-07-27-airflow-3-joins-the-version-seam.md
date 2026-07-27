---
id: 2026-07-27-airflow-3-joins-the-version-seam
status: Accepted
supersedes: null
superseded-by: null
components: [airflow-watch, api-versions]
ticket: null
date: 2026-07-27
---
# Airflow 3 joins Airflow 2 behind the widened version seam

## Context

The `airflow-2-only-behind-a-version-seam` ADR scoped the tool to Airflow 2 for one
stated reason: there was no Airflow 3 instance to verify against, and blind support
is worse than an honest refusal. That condition has flipped — the *data production*
deployment ("Production Pipelines") now runs Airflow 3.3.0 on Astro Runtime 3.3-2,
while "Production" remains on 2.11.0. The tool must speak both, and the original ADR
explicitly anticipated this: *"Adding Airflow 3 support extends the seam; it does not
add a parallel stack, and it does not require superseding this ADR."* This ADR is
that extension, and it records the v2-specific choices — the decisions a future
engineer will ask "why is it like this?" about. It does not supersede the seam ADR;
every constraint there stays in force.

Everything below was verified against the live 3.3.0 deployment (read-only calls) or
the astro CLI's cached 3.3.0 OpenAPI spec — not assumed from documentation:

- Most of the surface is **shared**: the `/dags/~/dagRuns` wildcard, task-instance /
  import-error / task-graph field names, the `total_entries` envelope, the 100-record
  page cap, pause (`PATCH /dags/{id}` + `update_mask`), and — contrary to the
  original ADR's belief — `POST /dags/{id}/clearTaskInstances`, which survives in
  v2 with every field we send, `dry_run` still defaulting to true.
- The **real differences**: `exclude_stale` replaces `only_active` (still defaulting
  to hide); `is_stale` replaces `is_active` with inverted sense; `timetable_summary`
  replaces the `schedule_interval` object; run ordering must use `-run_after`
  (`-execution_date` is gone); logs are structured JSON events rather than v1's
  repr-of-tuples; the trigger body **requires** `logical_date` (nullable); and
  `updateTaskInstancesState` is removed in favour of
  `PATCH …/taskInstances/{task_id}` (mapped: `…/{task_id}/{map_index}`) with a
  separate side-effect-free `…/dry_run` endpoint. All mutation responses still use
  the `task_instances` envelope.
- v2 **silently ignores unknown query parameters** (verified: `only_active=false`
  against 3.3.0 returns 200 and applies nothing). Sending v1 spellings to a v2
  server would quietly misreport, which is why dispatch must be explicit and
  guessing stays forbidden.
- Version detection for plain `--api-url` targets is solvable: `/version` exists on
  both majors, needs one call, and returns the exact version string to pin; probing
  the wrong major fails distinctively (Airflow 3 answers a v1 path with 404
  *"/api/v1 has been removed in Airflow 3"*). Astro targets never probe — discovery
  reports `airflowVersion`, as before.
- The astro CLI (1.43.1) carries specs for both majors (2.10–3.3 verified; unknown
  future versions fetch on demand), so the transport ADR needs no change.

## Decision

`airflow-watch` supports Airflow 2 (`/api/v1`) **and** Airflow 3 (`/api/v2`).
`_SUPPORTED_MAJORS` widens to `(2, 3)`; the refusal machinery stays in place and now
fires for any *other* major (1.x, 4.x, unparseable).

All new version knowledge stays inside `api.py`, dispatched on `major_version()`:
builders and parsers that differ take the version; those verified identical stay
version-free. Specifically:

- v2 marks a task state with `PATCH …/taskInstances/{task_id}` (the `{map_index}`
  path variant for mapped instances), sending `{"new_state": …}`; a dry run goes to
  the dedicated `…/dry_run` endpoint instead of a body flag. The one-task-at-a-time
  caller contract is preserved across both versions. The full
  (method, path, body) construction for mutations moves into `api.py`, because with
  two versions the *method and path shape* are version knowledge, not just names.
- v2 triggers send `"logical_date": null` explicitly when no date is chosen (the
  field is required-but-nullable); v1 keeps omitting it.
- v2 structured log events are flattened to readable `timestamp level message`
  lines at parse time; v1 keeps the repr-unwrapping. The `TaskLog` model is
  unchanged.
- A plain `--api-url` target with no `--airflow-version` is resolved by a **one-time
  startup probe** of `/version`, pinning the exact string the server reports,
  trying the base path implied by the URL's suffix first. The previous behaviour —
  silently assuming 2.11 — is replaced; an explicit `--airflow-version` skips the
  probe entirely.

## Consequences

**Easier.** Both production deployments are watchable again with one tool. The
refusal path, open state sets, and pagination logic carry over unchanged — Airflow
3's new `awaiting_input` state, for example, already renders via the fallback
bucket. Plain targets get honest version detection instead of a silent assumption.

**Harder, and accepted.**

- Two dialects to keep true. Every future endpoint the tool adopts must be checked
  against both specs, and the wire-name discipline (no version-specific field name
  outside `api.py`) now covers two name sets — the enforcement tests grow to match.
- Live verification covers exactly the minors we run: 2.11.0 and 3.3.0, both on
  Astro. Other 3.x minors are supported by `major_version()` but are spec-verified
  at best. Mutation endpoints on v2 were verified against the spec and (for dry-run
  paths) live; destructive variants were not fired against production.
- The startup probe adds one ~0.7 s call for plain targets without an explicit
  version — and on ingresses that 401 unauthenticated paths (like Astro's), the
  probe cannot distinguish "wrong version" from "no access", so its failure message
  must name both possibilities. Astro targets are unaffected.
- The seam interface grows a version parameter on part of its surface, which is
  mild asymmetry: some builders take `version`, some don't. The alternative — a
  uniform dialect object — was judged more structure than two dialects need (see
  below), and can still be adopted if a third shape ever appears.

**Constraints this imposes** (in addition to everything the seam ADR already
requires, which remains binding):

- No v2 field or parameter spelling appears outside `api.py`, exactly as for v1.
- A v2 request never carries a v1-only parameter or vice versa — silence-on-unknown
  makes that a misreport, not an error.
- Dry-run semantics must be equivalent across versions from the caller's view: the
  caller sets `Action.dry_run` and the seam chooses body-flag (v1) or endpoint (v2).
- The version probe runs at most once per session per plain target, and only when
  the user did not state a version.

## Alternatives considered

- **Let the astro CLI auto-detect the version per call** (drop `--airflow-version`).
  Rejected: measured at ~1.9 s vs ~0.75 s per call, and pinning is a constraint of
  the transport ADR precisely for that reason.
- **A full dialect/adapter object** (`api.dialect(version)` returning a strategy).
  Cleaner call sites, but it reshapes every consumer and test for two dialects that
  share most of their surface. Rejected as the "normalising translation layer" the
  original ADR already declined; revisit if a third API shape appears.
- **Sniff response fields instead of dispatching on version** (e.g. read whichever
  of `is_active`/`is_stale` is present). Tempting for parsers, but it cannot work
  for *requests* (v2 ignores unknown params silently), and half-sniffing splits the
  dispatch logic in two places. Rejected.
- **Supersede the airflow-2-only ADR.** Its Decision line ("supports Airflow 2
  only") is ended by this work, but its own Consequences section pre-authorized
  widening without supersession, and every structural constraint it imposes remains
  live. Superseding would retire prose that still governs the code. Rejected.
- **Keep assuming 2.11 for plain targets.** Preserves old behaviour but silently
  breaks against a plain Airflow 3 server, in exactly the "obscure 404" way the
  seam ADR exists to prevent. Rejected in favour of the probe.
