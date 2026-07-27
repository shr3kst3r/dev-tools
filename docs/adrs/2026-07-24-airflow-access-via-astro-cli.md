---
id: 2026-07-24-airflow-access-via-astro-cli
status: Accepted
supersedes: null
superseded-by: null
components: [airflow-watch, transport]
ticket: null
date: 2026-07-24
---
# Reach Airflow and Astro exclusively by shelling out to the `astro` CLI

## Context

`airflow-watch` needs to read DAGs, runs, task instances, logs, import errors and
pools from several Airflow deployments, and to perform a few mutating actions. The
deployments are Astronomer Astro (Hybrid), and the tool must also work against
plain self-hosted Airflow.

Four things about the environment shaped this decision, all verified rather than
assumed:

**Astro's ingress authenticates everything.** Unauthenticated `curl` against a
deployment returns `401 Not authorized` on *every* path, including `/api/v1/version`
and `/api/v1/health`. The unauthenticated version probe that works for self-hosted
Airflow is impossible here, and a 401 is indistinguishable from a wrong API version.

**Auth is not a small amount of code to own.** Astro session tokens are 60-minute
Auth0 JWTs. The installed `astro` CLI refreshes them silently: mid-research the
cached `expiresin` read `2026-07-23T22:06` — already past — yet calls succeeded, and
the cache afterwards read `2026-07-24T21:43`. The credential file
(`~/.astro/config.yaml`) has an **undocumented** `contexts.*` schema and would need
a YAML parser, which this repo does not have. Owning auth means owning refresh
against an undocumented cache, or implementing the Astro token flow directly.

**The `astro` CLI already solves the hard parts.** Version 1.43.1 ships an
`astro api airflow` command that resolves a deployment's Airflow URL from
`--deployment-id`, authenticates, auto-detects the target's Airflow version to pick
the right API spec, and supports `--paginate`, `--slurp`, `-q/--jq`, `-X`, `-H` and
`--generate`. It bundles OpenAPI specs for Airflow 2.10, 2.11, 3.0 and 3.1.
`astro api cloud ListDeployments` returns every deployment in the org — including
`airflowVersion`, `status` and `apiUrl` — in one call.

**Latency was the only real objection, and it is fixable.** Measured against a
production deployment:

| Shape | Wall clock |
|---|---|
| One call, version auto-detected | 0.80 – 1.96 s |
| One call, `--airflow-version` pinned | **0.73 – 0.78 s** |
| Six calls, serial | 4.47 s |
| Six calls, parallel | **1.12 s** |

Most of the variance was the version-detection round trip, which is avoidable:
`ListDeployments` already reports `airflowVersion`, so the version can always be
pinned. Subprocess spawn parallelises well, so a full refresh across both
deployments (~12 calls) lands near 1.2 s.

The repo has an established precedent for exactly this shape: `pr-watch` and
`my-prs` shell out to `gh` rather than implementing GitHub auth, behind a single
`_run()` wrapper and a `require_gh()` preflight. It has no HTTP client dependency
at all — `httpx` is absent from `uv.lock` — and `slack_me/slack.py` says outright
that it uses stdlib `urllib` "rather than adding a `requests` dependency".

The user has explicitly accepted the `astro` CLI as a dependency, conditional on
solid detection and helpful error messages.

## Decision

All Airflow and Astro API access in `airflow-watch` goes through subprocess calls to
the `astro` CLI — `astro api cloud` for deployment discovery and `astro api airflow`
for per-deployment data — behind a single `_run()` wrapper, mirroring
`tools/pr_watch/github.py`. We add no HTTP client dependency.

We always pass `--airflow-version`, taken from the `airflowVersion` reported by
deployment discovery, so no call pays for version auto-detection. Concurrent calls
are fanned out from the existing thread-worker pattern rather than issued serially.

A `require_astro()` preflight checks for the binary and for an authenticated
context, and every failure mode — CLI missing, not logged in, deployment not found,
deployment hibernating — produces a specific, actionable message rather than a
propagated subprocess error.

## Consequences

**Easier.** No auth code, no token refresh, no credential parsing, and no OAuth
flow — the single largest chunk of risk in this tool disappears. Deployment URL
construction is avoided entirely (the real form carries a routing label the docs
omit). One command shape covers Astro (`--deployment-id`) and plain Airflow
(`--api-url`), so both platforms share a code path. Zero new dependencies keeps the
repo's dependency posture intact, and the pattern is already familiar from `gh`.

**Harder, and accepted.** Every request costs a process spawn — ~0.75 s, versus a
few tens of milliseconds for a pooled HTTP connection. Drill-down interactions need
a visible loading state to feel responsive, and log paging will never feel instant.
We cannot stream: `--paginate`/`--slurp` buffer whole responses, so Airflow 3's
ndjson log streaming would be unavailable to us if we ever wanted it, and tailing a
running task's log means repeated polling rather than a held connection.

We inherit the CLI's behaviour, including its bugs and its output-format changes,
and we are pinned to whatever it decides an endpoint looks like. Two of its quirks
already bite: adding `-f`/`-F` parameters silently flips the HTTP method to POST
(a `405` on a GET endpoint), so query parameters must be embedded in the path; and
`--username/--password` are documented "local only", so a remote non-Astro Airflow
needs credentials injected via `-H`.

We are accepting a hard dependency on an external binary and on the user having run
`astro login`. This is a developer tool for a team that already uses Astro, which is
what makes that acceptable — it would not be for a library.

**Constraints this imposes.**

- No HTTP client dependency may be added to this tool. Adding one supersedes this
  ADR rather than sitting alongside it, because a second transport means two auth
  models and two error taxonomies.
- The tool never reads or writes `~/.astro/config.yaml`. Its schema is undocumented
  and reading the token there means owning refresh.
- Bearer tokens must never reach the activity log, an error message, or a debug
  pane. `astro auth token` and `astro api --generate` both emit live credentials on
  stdout, so neither may be used in a code path whose output is displayed.
- Every `astro` invocation passes an explicit `--airflow-version`.
- Subprocess failures are converted to typed data at the I/O boundary and never
  surface as raw stderr, per the existing `GitHubError` → `PollError` pattern.

## Alternatives considered

- **Hand-rolled `httpx` client owning auth end to end.** Fast, poolable,
  async-native, and it would give us streaming logs. Rejected: it means
  implementing and maintaining Astro's token acquisition and refresh — the part the
  CLI demonstrably already does correctly — plus a new dependency, for a latency
  win that parallel fan-out mostly recovers anyway.
- **Hybrid: `astro auth token` for the credential and `apiUrl` for the endpoint,
  then native HTTP.** Verified working with raw `curl`, and genuinely attractive:
  documented seams, no OAuth code, full HTTP speed. Rejected for now because it
  still needs an HTTP dependency and a token-expiry retry path, and because it
  splits the transport in two — the CLI for discovery, HTTP for data — doubling the
  error taxonomy for a first version. This is the most likely successor if
  per-keystroke latency proves intolerable, and it is deliberately left cheap to
  adopt: it reuses the same discovery call and the same endpoint metadata.
- **Hybrid via `astro api airflow --generate`,** parsing the emitted curl command
  for URL and token. Rejected: `--generate`'s output format is not a documented
  contract, and the approach requires printing a live credential into a parsed
  buffer.
- **`apache-airflow-client`, the official generated client.** Rejected on hard
  grounds: the Airflow 2 and Airflow 3 client lines are published under the *same
  distribution name*, so both cannot be installed, which forecloses the version
  span we may want later. It is also sync-only, does not handle auth, and its strict
  generated models reject unknown enum values — the opposite of the leniency a
  monitoring tool needs.
- **stdlib `urllib`, following the `slack_me` precedent.** Avoids a dependency and
  matches the existing thread-worker concurrency model. Rejected: it still leaves us
  owning Astro auth and refresh, which is the actual cost centre. The dependency was
  never the hard part.
