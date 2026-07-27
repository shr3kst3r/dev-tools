---
id: 2026-07-24-airflow-2-only-behind-a-version-seam
status: Accepted
supersedes: null
superseded-by: null
components: [airflow-watch, api-versions]
ticket: null
date: 2026-07-24
---
# Support only Airflow 2 at first, with all version-specific knowledge behind one seam

## Context

The tool was originally asked to support Airflow 2 and Airflow 3. Research changed
the calculus, and the scope was then narrowed to Airflow 2 for now.

**Airflow 2 and 3 are a hard fork of the API, not a migration.** Airflow 2.x serves
`/api/v1` (Connexion/Flask); Airflow 3.x serves `/api/v2` (FastAPI) and removed
`/api/v1` entirely. No release serves both and there is no compatibility shim. The
differences are not cosmetic: `execution_date_*` filters became `logical_date_*` and
`run_after_*`; `only_active` became `exclude_stale`; the default page limit changed
from 100 to 50; `POST .../updateTaskInstancesState` and the `setNote` endpoints were
removed in favour of generic `PATCH` bodies; `execution_date` is gone from the
trigger payload; datasets became assets and are keyed by an integer `asset_id`
instead of a `uri`; `/dagSources/{file_token}` became `/dagSources/{dag_id}`; and a
new task state `awaiting_input` exists that Airflow 2 never emits.

**There is nothing to test Airflow 3 against.** Both deployments in the
organization run Airflow 2.11.0 on Astro Runtime 13.4.0. Writing Airflow 3 support
now means writing it blind and claiming a compatibility we cannot demonstrate —
which is worse than not claiming it, because the failure would surface on someone
else's production instance.

**But Airflow 2 is not the long term.** Airflow 2.11 is the final 2.x line and its
maintenance ends April 2027; Astro Runtime already ships 3.0–3.3 for Airflow 3.
Airflow 3 support is a matter of when, not if, so the cost of retrofitting it is a
real consideration now.

**The version is always known cheaply.** `astro api cloud ListDeployments` reports
`airflowVersion` per deployment, and the deployment object's `apiUrl` already carries
the correct `/api/v1` or `/api/v2` suffix. So dispatching on version costs nothing at
runtime — the information arrives with discovery.

One subtlety that makes the seam cheaper than it looks: the primary view needs no
version-specific work at all. `GET /dags/~/dagRuns` — cross-DAG run listing — is
documented in the 2.11 spec and verified working live, and exists in Airflow 3 too.

## Decision

`airflow-watch` supports Airflow 2 (`/api/v1`) only. It detects each target's
Airflow version from discovery and **refuses non-2.x targets with a clear message**
rather than attempting a request that would fail obscurely.

All version-dependent knowledge — base path, endpoint paths, query-parameter names,
and response field mapping — is confined to a single module that the rest of the tool
addresses through one interface. No other module contains a version check, an
`/api/v1` literal, or a v1-only field name.

Parsing is lenient in one specific direction: unknown task and run states, and
unknown response fields, are preserved or bucketed rather than rejected, so a 2.x
patch release cannot break the tool.

## Consequences

**Easier.** Every claim the tool makes is testable against a real instance today.
The client layer stays small — one API shape, one auth story, one set of field names.
Adding Airflow 3 later is additive: a second implementation behind the existing
interface, with the dispatch point and the refusal path already in place and already
exercised.

**Harder, and accepted.** We ship an indirection with exactly one implementation
behind it, which is the shape reviewers rightly distrust — it looks like speculative
generality and reads as over-engineering until the second implementation lands. This
ADR is the justification, and the honest cost is that if Airflow 3 support never
happens, the seam was pure overhead. We judge that unlikely given the April 2027 date.

The seam is also unproven: an abstraction designed against one implementation
usually turns out to be subtly wrong for the second, so the Airflow 3 work should
expect to reshape it rather than merely fill it in. Designing it from the *known*
v1/v2 differences rather than from imagination is the mitigation, not a guarantee.

Users on Airflow 3 get a refusal, not degraded service. That is a deliberate choice
in favour of an honest error over partial, silently-wrong behaviour — a monitoring
tool that misreports state is worse than one that declines to run.

**Constraints this imposes.**

- No `/api/v1` literal, version conditional, or v1-only field name appears outside
  the version module. A reviewer should be able to grep for this.
- No task-state or run-state enum is closed. Unknown values render in a fallback
  bucket and must never raise.
- Non-2.x targets are refused explicitly at the discovery boundary, with the
  detected version named in the message.
- Adding Airflow 3 support extends the seam; it does not add a parallel stack, and
  it does not require superseding this ADR — only widening the supported range.

## Alternatives considered

- **Support both versions now.** What was originally asked. Rejected because no
  Airflow 3 instance exists to verify against, so the support would be untested by
  construction, and the OpenAPI-derived differences above are numerous enough that
  blind implementation would very likely be wrong in detail.
- **Airflow 2 only, hardcoded throughout,** with no seam. Simplest, most honest
  about present scope, and the smallest diff. Rejected: with Airflow 2.11 EOL in
  April 2027, retrofitting version dispatch through a codebase that assumed one API
  shape means touching every module — precisely the expensive-to-reverse decision
  this ADR exists to avoid.
- **Target Airflow 3 only** and wait for the fleet to upgrade. Rejected: it would
  make the tool useless against the only two deployments that exist today.
- **A normalising translation layer** presenting one internal model with adapters
  per version, built out fully now. Rejected as too much structure for one
  implementation; the interface here is deliberately thinner, and can grow into this
  if Airflow 3 support shows it needs to.
- **Attempt v1, fall back to v2 on 404.** Rejected: on Astro every unauthenticated
  path returns 401 and a wrong-version path returns 404 only *after* auth, so probe
  logic is both unnecessary (the version arrives with discovery) and ambiguous.
