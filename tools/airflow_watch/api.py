"""The Airflow API version seam — the *only* module that knows a version exists.

Per the airflow-2-only-behind-a-version-seam ADR and the
airflow-3-joins-the-version-seam ADR that widened it, every piece of
version-dependent knowledge lives here and nowhere else:

* the base path (`/api/v1` for Airflow 2, `/api/v2` for Airflow 3),
* every endpoint path, and — since the two majors mark a task state through
  different HTTP verbs on different paths — every mutation's *shape*,
* every query-parameter name (`only_active` vs `exclude_stale`, …),
* every request-body field name (`logical_date`, `dry_run`, `new_state`, …),
* and the response→model mapping (`is_active` vs `is_stale`, and v1's
  repr-of-tuples log against v2's structured events).

No other module contains an ``/api/v`` literal, a version conditional, or a
version-specific field name. `tests/test_airflow_watch.py::test_no_api_version_literal_outside_api_module`
enforces that by grepping the package, so a reviewer does not have to.

Two dialects, not two stacks: builders and parsers that the two majors were
*verified* to share stay version-free, and only the ones that genuinely differ
take a `version` and dispatch on `major_version()`. Guessing stays forbidden in
both directions — Airflow 3 silently ignores a query parameter it does not know
(measured: `only_active=false` against 3.3.0 returns 200 and filters nothing),
so a v1 spelling sent to a v2 server misreports rather than failing.

Two behaviours of the `astro` CLI shape the builders here, both measured rather
than assumed (see the airflow-access-via-astro-cli ADR):

* **Query parameters are embedded in the path string.** Passing them as
  ``-f``/``-F`` silently flips the HTTP method to POST, which returns 405 on a
  GET endpoint. Every builder therefore returns a complete
  ``/path?query=string``.
* **Paths carry no trailing slash.**
"""

from __future__ import annotations

import ast
from datetime import datetime
from urllib.parse import quote, urlencode

from .models import (
    Action,
    Dag,
    DagRun,
    Deployment,
    ImportErrorEntry,
    TaskInstance,
    TaskLog,
)

__all__ = [
    "UnsupportedAirflowVersion",
    "api_url_for",
    "base_path",
    "clear_body",
    "clear_task_instances_path",
    "dag_path",
    "dag_runs_path",
    "dags_path",
    "import_errors_path",
    "log_path",
    "major_version",
    "mark_body",
    "mark_task_instance_path",
    "mark_task_state_path",
    "mutation_request",
    "pause_body",
    "pause_dag_path",
    "parse_dag_run",
    "parse_dag_runs",
    "parse_dags",
    "parse_deployments",
    "parse_error_detail",
    "parse_import_errors",
    "parse_log",
    "parse_task_graph",
    "parse_task_instances",
    "parse_version",
    "probe_pins",
    "tasks_path",
    "supported_range",
    "supports",
    "task_instances_path",
    "total_entries",
    "trigger_body",
    "trigger_run_path",
    "unsupported_message",
    "version_path",
]

# The two API-version literals in the tool. Airflow 2.x serves `/api/v1`;
# Airflow 3.x removed it entirely and serves `/api/v2`. No release serves both
# and there is no compatibility shim, so the base path is picked from the
# target's major and never guessed.
_V1_BASE_PATH = "/api/v1"
_V2_BASE_PATH = "/api/v2"

# Every base path we recognize, for stripping a suffix a caller already typed.
_BASE_PATHS = (_V1_BASE_PATH, _V2_BASE_PATH)

# The Airflow major versions this build understands, as a closed set — unlike
# task and run states, which are deliberately open. Both are verified live
# (2.11.0 and 3.3.0 on Astro); a 1.x, a 4.x or an unparseable version is refused
# by name rather than attempted.
_SUPPORTED_MAJORS = (2, 3)

# The concrete spec versions to pin while probing a plain `--api-url` target,
# one per major. They are handed to `astro --airflow-version`, which uses them
# to pick a bundled OpenAPI spec, so "2.x" would not do — and naming them here
# keeps the last invented version number inside the seam.
_PROBE_PINS = ("2.11.0", "3.3.0")

# Airflow 2's default page limit is 100 and Airflow 3's is 50; we ask explicitly
# everywhere so neither default decides what we show.
DEFAULT_LIMIT = 50

# The largest page either major will serve, whatever you ask for:
# `maximum_page_limit` defaults to 100 and a request for more is silently
# truncated to it (verified live on both 2.11.0 and 3.3.0 — `limit=150` returns
# 100 rows). Callers that need everything must page with `offset`, because the
# `astro` CLI's own `--paginate`/`--slurp` do not paginate this API (verified
# against 2.11: it returns page one and drops `total_entries`).
PAGE_LIMIT = 100

# Newest-first, per major. v1 orders runs by `-execution_date`; Airflow 3
# removed that field name outright, and `-run_after` is the verified
# replacement. Sending the wrong one is not an error on v2 — it is silently
# ignored, so the list would come back in an arbitrary order.
_V1_RUNS_ORDER_BY = "-execution_date"
_V2_RUNS_ORDER_BY = "-run_after"


class UnsupportedAirflowVersion(ValueError):
    """Raised when asked to address an Airflow version this build cannot serve.

    Carries the detected version so the refusal message can name it, per the
    ADR's "names the detected version" constraint.
    """

    def __init__(self, version: str) -> None:
        super().__init__(unsupported_message(version))
        self.version = version


# --- version dispatch ------------------------------------------------------


def major_version(version: str) -> int | None:
    """The leading major number of an Airflow version string, or None.

    Lenient on purpose: `2.11.0`, `2.11`, `2.11.0+astro.1` and `v2.11.0` all
    read as 2, and anything unparseable reads as None (which is refused, with
    the raw string quoted back).
    """
    cleaned = version.strip().lstrip("vV")
    head = cleaned.split(".", 1)[0]
    digits = ""
    for ch in head:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def supports(version: str) -> bool:
    """True when this build can talk to the given Airflow version."""
    return major_version(version) in _SUPPORTED_MAJORS


def supported_range() -> str:
    """Human-readable statement of what is supported, for error messages."""
    return " / ".join(f"Airflow {major}.x" for major in _SUPPORTED_MAJORS)


def _serves_v2(version: str) -> bool:
    """True when this target serves `/api/v2` — i.e. it is an Airflow 3.

    The single dispatch predicate in the module: every version branch below
    goes through it, so "which dialect?" is answered in exactly one place.
    """
    return major_version(version) == 3


def unsupported_message(version: str) -> str:
    """Why we are declining, naming the detected version.

    Deliberately an honest refusal rather than a degraded attempt: an Airflow
    outside the supported range serves an API we have not verified against, and
    a monitoring tool that guessed would misreport state rather than fail.
    """
    shown = version.strip() or "unknown"
    return (
        f"Airflow {shown} is not supported — airflow-watch speaks "
        f"{supported_range()} only, and this target serves neither."
    )


def base_path(version: str) -> str:
    """The API base path for an Airflow version, or raise."""
    if not supports(version):
        raise UnsupportedAirflowVersion(version)
    return _V2_BASE_PATH if _serves_v2(version) else _V1_BASE_PATH


def api_url_for(url: str, version: str) -> str:
    """Normalize a user-supplied Airflow base URL for `astro --api-url`.

    Astro's discovery already reports an `apiUrl` carrying the version suffix,
    but a human typing `--api-url https://airflow.example.com` has not. Any
    suffix we recognize is stripped and the *right* one appended, so a URL
    copied from a 3.x deployment cannot end up as `…/api/v2/api/v1` when it is
    pinned to 2.x. An unsupported version raises rather than producing a URL we
    could not read anyway.
    """
    suffix = base_path(version)
    trimmed = url.rstrip("/")
    for known in _BASE_PATHS:
        if trimmed.endswith(known):
            trimmed = trimmed[: -len(known)]
            break
    return trimmed + suffix


# --- version detection (plain `--api-url` targets only) --------------------
#
# Astro targets never reach any of this: discovery reports `airflowVersion`, so
# their version arrives before the first request. A plain Airflow has no such
# oracle, so the version is either stated with `--airflow-version` or probed
# once at startup — never assumed, because assuming 2.x against an Airflow 3
# server is exactly the obscure-404 failure the seam exists to prevent.


def version_path() -> str:
    """`GET /version` — the probe endpoint, which both majors serve."""
    return "/version"


def probe_pins(url: str) -> tuple[str, ...]:
    """The spec versions to pin while probing, best guess first.

    Every `astro` call must name a version (transport ADR), including the probe
    itself — so probing means trying one concrete pin per major until one
    answers. The URL is the only hint available before the first call: a target
    whose URL already ends in a base path is asked in that dialect first. Failing
    that the 2.x line goes first, which is what the previous assume-2.11
    behaviour did, so an existing plain-2.x user still succeeds on call one.
    """
    trimmed = url.rstrip("/")
    hinted = [pin for pin in _PROBE_PINS if trimmed.endswith(base_path(pin))]
    return tuple(hinted + [pin for pin in _PROBE_PINS if pin not in hinted])


def parse_version(payload: object) -> str:
    """`{"version": "3.3.0+astro.2"}` → `"3.3.0"`, or "" if unreadable.

    What the *server* reports is what gets pinned from then on, rather than the
    pin that happened to make the probe succeed — a 2.10 target must not be
    addressed with a 2.11 spec.

    The one thing dropped is the build suffix an image adds to the release. An
    Astro Runtime Airflow answers `/version` with `2.11.0+astro.7`, and the
    pinned string is not a label: `astro --airflow-version` loads the OpenAPI
    spec *named by it*, and a version it cannot resolve fails the same way a
    nonexistent one does ("loading OpenAPI spec: unexpected status code: 404").
    Pinning the build suffix would therefore turn a successful probe into a
    session where every subsequent call fails. Semver already says build
    metadata is not part of the version's identity; the seam agrees, and keeps
    the release — major, minor and patch — exactly as reported.
    """
    if not isinstance(payload, dict):
        return ""
    reported = _text(payload.get("version")).strip()
    return reported.split("+", 1)[0].strip()


# --- endpoint paths --------------------------------------------------------
#
# Every builder returns a path *with* its query string, because handing query
# parameters to `astro` as -f/-F flips the request to POST.


def _segment(value: str) -> str:
    """Percent-encode one path segment.

    Run ids routinely contain `:` and `+` (`manual__2026-05-13T01:04:15+00:00`),
    which must not be read as URL syntax. Verified against a live 2.11
    deployment: the CLI passes the encoded form through without re-encoding it.
    """
    return quote(value, safe="")


def _with_query(path: str, params: list[tuple[str, str]]) -> str:
    """Attach a query string, dropping empty values and keeping repeats.

    Repeated keys matter: the `state` filter is an array parameter in both
    majors, so `?state=failed&state=running` is how you ask for two states.
    """
    kept = [(key, value) for key, value in params if value != ""]
    if not kept:
        return path
    return f"{path}?{urlencode(kept)}"


def dags_path(
    *,
    version: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    dag_id_pattern: str = "",
) -> str:
    """List DAGs — *all* of them, including paused and stale ones.

    "Hide DAGs whose file is gone" is spelled `only_active` in v1 and
    `exclude_stale` in v2, and it **defaults to hiding** in both. A monitoring
    tool must not do that: we turn it off explicitly and let the UI label a
    stale DAG instead of omitting it. The version dispatch is not cosmetic —
    Airflow 3 ignores `only_active` silently, so the v1 spelling against a v2
    server would quietly hide rows rather than complain. Paused DAGs are never
    filtered either: `is_paused` is shown, not applied.

    `dag_id_pattern` is the server-side substring match on the dag id (same
    name in both majors), used when the full list is too large to have loaded
    client-side.
    """
    hide_stale = "exclude_stale" if _serves_v2(version) else "only_active"
    return _with_query(
        "/dags",
        [
            ("limit", str(limit)),
            ("offset", str(offset) if offset else ""),
            (hide_stale, "false"),
            ("dag_id_pattern", dag_id_pattern),
        ],
    )


def tasks_path(dag_id: str) -> str:
    """A DAG's task definitions, including each task's `downstream_task_ids`.

    This is the *structure* of the DAG, as opposed to one run's task instances —
    it is what lets the task pane show dependency order rather than start order.
    """
    return f"/dags/{_segment(dag_id)}/tasks"


def dag_path(dag_id: str) -> str:
    return f"/dags/{_segment(dag_id)}"


def dag_runs_path(
    dag_id: str = "~",
    *,
    version: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    states: tuple[str, ...] = (),
    order_by: str = "",
) -> str:
    """Recent runs, newest first. `dag_id="~"` is the cross-DAG wildcard —
    verified in both majors — so one call covers the whole deployment's run
    history, which is what makes the primary view a single request.

    Only the ordering differs: an empty `order_by` means "this version's
    newest-first", which is `-execution_date` on v1 and `-run_after` on v2.
    """
    order_by = order_by or (
        _V2_RUNS_ORDER_BY if _serves_v2(version) else _V1_RUNS_ORDER_BY
    )
    params = [
        ("limit", str(limit)),
        ("offset", str(offset) if offset else ""),
        ("order_by", order_by),
    ]
    params += [("state", state) for state in states if state]
    return _with_query(f"/dags/{_segment(dag_id)}/dagRuns", params)


def task_instances_path(
    dag_id: str, run_id: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> str:
    return _with_query(
        f"/dags/{_segment(dag_id)}/dagRuns/{_segment(run_id)}/taskInstances",
        [("limit", str(limit)), ("offset", str(offset) if offset else "")],
    )


def log_path(
    dag_id: str,
    run_id: str,
    task_id: str,
    try_number: int,
    *,
    map_index: int = -1,
    full_content: bool = True,
) -> str:
    """One attempt's log. `full_content=true` asks for the whole body rather
    than a pointer, which is what we want since we cannot stream. Both majors
    take the parameter (v2 flipped its *default* to false, which is moot: we
    always send it).

    `map_index` is required for a *mapped* task instance: Airflow looks the
    instance up by `(dag_id, run_id, task_id, map_index)` and defaults the last
    to `-1`, so omitting it on a mapped task returns 404 "TaskInstance not
    found" rather than the log. `-1` is Airflow's own default and our sentinel
    for "not mapped", so it is left off the path in that case. There is no
    path-segment form of this for logs — only the query parameter, in both
    majors.
    """
    return _with_query(
        f"/dags/{_segment(dag_id)}/dagRuns/{_segment(run_id)}"
        f"/taskInstances/{_segment(task_id)}/logs/{max(1, try_number)}",
        [
            ("full_content", "true" if full_content else ""),
            ("map_index", str(map_index) if map_index >= 0 else ""),
        ],
    )


def import_errors_path(*, limit: int = DEFAULT_LIMIT) -> str:
    return _with_query("/importErrors", [("limit", str(limit))])


def pause_dag_path(dag_id: str) -> str:
    """PATCH target for pause/unpause.

    `update_mask` limits the patch to the one field, so a future API addition
    cannot be blanked by a request that only meant to flip the pause switch.
    """
    return _with_query(dag_path(dag_id), [("update_mask", "is_paused")])


def trigger_run_path(dag_id: str) -> str:
    return f"/dags/{_segment(dag_id)}/dagRuns"


def clear_task_instances_path(dag_id: str) -> str:
    """The dedicated clear endpoint — **the same in both majors**.

    Worth stating because it is easy to assume otherwise: Airflow 3 removed the
    neighbouring `updateTaskInstancesState` endpoint, and the original seam ADR
    recorded this one as removed too. It is not. Verified against the 3.3.0
    spec, with every field `clear_body` sends still present and `dry_run` still
    defaulting to true.
    """
    return f"/dags/{_segment(dag_id)}/clearTaskInstances"


def mark_task_state_path(dag_id: str) -> str:
    """**v1 only.** Its dedicated set-state endpoint, which takes one task id in
    the body; Airflow 3 removed it in favour of patching the task instance
    itself (see `mark_task_instance_path`)."""
    return f"/dags/{_segment(dag_id)}/updateTaskInstancesState"


def mark_task_instance_path(
    dag_id: str,
    run_id: str,
    task_id: str,
    *,
    map_index: int = -1,
    dry_run: bool = False,
) -> str:
    """**v2 only.** The task instance to PATCH a new state onto.

    Airflow 3 addresses the instance by path rather than naming it in a body, so
    both of the things v1 carried as fields become path segments here: a mapped
    instance appends its `map_index`, and a dry run goes to a separate,
    side-effect-free `…/dry_run` endpoint instead of setting a body flag. That
    is why the whole (method, path, body) triple is built in this module — with
    two majors the path *shape* is version knowledge too.
    """
    path = (
        f"/dags/{_segment(dag_id)}/dagRuns/{_segment(run_id)}"
        f"/taskInstances/{_segment(task_id)}"
    )
    if map_index >= 0:
        path += f"/{map_index}"
    if dry_run:
        path += "/dry_run"
    return path


# --- request bodies --------------------------------------------------------
#
# Wire field names. Where a body carries `dry_run` (v1 clear and set-state, v2
# clear) it is always sent explicitly: it defaults to *true*, so a body that
# omits it returns 200 and does nothing at all.


def pause_body(paused: bool) -> dict[str, object]:
    """Pause/unpause payload — identical in both majors, alongside the
    `update_mask=is_paused` query parameter `pause_dag_path` adds."""
    return {"is_paused": paused}


def trigger_body(
    version: str,
    logical_date: datetime | None = None,
    conf: dict[str, object] | None = None,
) -> dict[str, object]:
    """Trigger payload. Both majors take `logical_date`; the older
    `execution_date` spelling is gone from Airflow 3, so we never send it.

    The one difference is what "no date chosen" looks like. v1 omits the field
    and the server stamps one. v2 makes it **required but nullable**, so an
    omitted field is a validation error and an explicit `null` is how you ask
    the server to stamp it.
    """
    body: dict[str, object] = {"conf": conf or {}}
    if logical_date is not None:
        body["logical_date"] = logical_date.isoformat()
    elif _serves_v2(version):
        body["logical_date"] = None
    return body


def clear_body(
    run_id: str,
    task_ids: tuple[str, ...] = (),
    *,
    dry_run: bool,
) -> dict[str, object]:
    body: dict[str, object] = {
        "dry_run": dry_run,
        "dag_run_id": run_id,
        "only_failed": False,
        "reset_dag_runs": True,
        "include_downstream": False,
        "include_upstream": False,
    }
    if task_ids:
        body["task_ids"] = list(task_ids)
    return body


def mark_body(
    run_id: str,
    task_id: str,
    state: str,
    *,
    dry_run: bool,
) -> dict[str, object]:
    """**v1's** set-state payload.

    Note the asymmetry with `clear_body`, which is v1's and not ours: the clear
    endpoint takes a `task_ids` *array*, while set-state takes a single
    **`task_id`** string and expands from it via the four `include_*` flags. A
    body carrying `task_ids` here fails validation (`unknown field`, and
    `task_id` missing) — which is why one task at a time is the caller's
    contract, enforced in `mutation_request`.

    All four `include_*` flags are required by this endpoint's schema, so they
    are always sent rather than left to a default.
    """
    return {
        "dry_run": dry_run,
        "dag_run_id": run_id,
        "task_id": task_id,
        "new_state": state,
        "include_downstream": False,
        "include_upstream": False,
        "include_future": False,
        "include_past": False,
    }


# --- mutations: the whole request, not just its names ----------------------


def mutation_request(
    version: str, action: Action
) -> tuple[str, str, dict[str, object]]:
    """The (method, path, body) that performs one confirmed action.

    Pure, and the only place a mutation's *shape* is decided. It lives here
    rather than in the transport because with two majors the verb and the path
    are version knowledge too, not merely field names: marking a task state is a
    POST to a collection endpoint on v1 and a PATCH of the instance itself on
    v2. Callers hand over an `Action` and send back exactly what they get.

    Dry runs are equivalent from the caller's side either way — it sets
    `Action.dry_run` and this function picks the body flag (v1) or the dedicated
    endpoint (v2).

    Raises `ValueError` for a request that must not be sent *at all*: an unknown
    action kind, an unscoped clear or mark, or a mark naming anything but one
    task. This module raises no transport error because it knows nothing about
    the transport; the caller converts.
    """
    match action.kind:
        case "pause" | "unpause":
            return (
                "PATCH",
                pause_dag_path(action.dag_id),
                pause_body(action.kind == "pause"),
            )
        case "trigger":
            return "POST", trigger_run_path(action.dag_id), trigger_body(version)
        case "clear":
            # Both majors' clear endpoints scope by run, and a body that names
            # task ids but *no* run scopes to the task's whole history instead —
            # so an unscoped clear is refused here rather than sent and hoped
            # about.
            return (
                "POST",
                clear_task_instances_path(action.dag_id),
                clear_body(
                    _scoped_run(action), action.task_ids, dry_run=action.dry_run
                ),
            )
        case "mark":
            return _mark_request(version, action)
        case _:
            raise ValueError(f"Unknown action {action.kind!r}.")


def _mark_request(
    version: str, action: Action
) -> tuple[str, str, dict[str, object]]:
    """Marking one task instance's state, in whichever shape the target takes.

    The one-task-at-a-time contract holds across both majors, for different
    reasons that land in the same place: v1's endpoint names a single `task_id`
    in the body (see `mark_body`) and v2's addresses a single instance by path.
    Refusing here rather than marking one of several is the point.
    """
    if len(action.task_ids) != 1:
        raise ValueError(
            "Marking a task state takes exactly one task instance "
            f"(asked for {len(action.task_ids)})."
        )
    run_id = _scoped_run(action)
    task_id = action.task_ids[0]
    if _serves_v2(version):
        return (
            "PATCH",
            mark_task_instance_path(
                action.dag_id,
                run_id,
                task_id,
                map_index=action.map_index,
                dry_run=action.dry_run,
            ),
            {"new_state": action.state},
        )
    return (
        "POST",
        mark_task_state_path(action.dag_id),
        mark_body(run_id, task_id, action.state, dry_run=action.dry_run),
    )


def _scoped_run(action: Action) -> str:
    """The run a clear or a mark must be confined to.

    Not defaulted: with task ids and no run id, Airflow clears or re-states those
    tasks in *every* run of the DAG. The app always drills into a run before
    offering either action, so an absent run id is a programming error — and the
    one place it could do real damage is the one place to refuse it.
    """
    if not action.run_id:
        raise ValueError(
            f"Refusing to {action.kind} {action.dag_id} with no DAG run named — "
            "that would affect every run of the task."
        )
    return action.run_id


# --- pure parsing (unit-tested) --------------------------------------------
#
# Lenient in one direction, per the ADR: unknown states and unknown fields are
# preserved or ignored, never rejected. A 2.x patch release that adds a field or
# a state cannot break the tool.


def _dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def total_entries(payload: object) -> int:
    """How many records the server says exist, across all pages.

    Both majors report this as `total_entries` on every list envelope (verified
    live on 3.3.0); a response that omits it reads as 0, which callers treat as
    "one page is all there is".
    """
    if not isinstance(payload, dict):
        return 0
    return _int(payload.get("total_entries"))


def _rows(payload: object, key: str) -> list[dict]:
    """The list of records under `key`, tolerating a missing or wrong-typed
    envelope — a shape we cannot parse yields an empty list, not a traceback."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def parse_deployments(payload: object) -> list[Deployment]:
    """Astro's ListDeployments response → Deployments.

    `airflowVersion` is the field that lets every later call pin
    `--airflow-version`; unsupported versions are kept (not dropped) so the
    switcher can show them and explain the refusal rather than hiding them.
    """
    out: list[Deployment] = []
    for row in _rows(payload, "deployments"):
        scaling = row.get("scalingStatus")
        hibernation = (
            scaling.get("hibernationStatus") if isinstance(scaling, dict) else None
        )
        hibernating = bool(
            isinstance(hibernation, dict) and hibernation.get("isHibernating")
        )
        out.append(
            Deployment(
                id=_text(row.get("id")),
                name=_text(row.get("name"), "(unnamed)"),
                workspace_name=_text(row.get("workspaceName")),
                airflow_version=_text(row.get("airflowVersion")),
                status=_text(row.get("status")),
                api_url=_text(row.get("apiUrl")),
                hibernating=hibernating,
            )
        )
    return out


def _dag_run(row: dict) -> DagRun:
    note = row.get("note")
    return DagRun(
        dag_id=_text(row.get("dag_id"), "?"),
        run_id=_text(row.get("dag_run_id")),
        state=_text(row.get("state"), "none"),
        run_type=_text(row.get("run_type")),
        logical_date=_dt(row.get("logical_date")),
        # v2 only; absent on v1 and read leniently rather than dispatched. It
        # matters because v2's `logical_date` is nullable — a manually
        # triggered Airflow 3 run can have no logical date at all, and
        # `run_after` is what still says when it belongs.
        run_after=_dt(row.get("run_after")),
        start_date=_dt(row.get("start_date")),
        end_date=_dt(row.get("end_date")),
        note=note if isinstance(note, str) else None,
    )


def parse_dag_runs(payload: object) -> list[DagRun]:
    """`GET /dags/~/dagRuns` → DagRuns. State is copied through verbatim."""
    return [_dag_run(row) for row in _rows(payload, "dag_runs")]


def parse_dag_run(payload: object) -> DagRun | None:
    """A *single* DAG run object, as `POST /dags/{id}/dagRuns` answers with.

    Separate from `parse_dag_runs` because the trigger endpoint returns the bare
    run rather than the list envelope — and knowing which shape each endpoint
    uses is exactly the version-specific knowledge that belongs here.
    """
    if not isinstance(payload, dict) or not payload.get("dag_run_id"):
        return None
    return _dag_run(payload)


def parse_task_instances(payload: object) -> list[TaskInstance]:
    """`GET .../taskInstances` → TaskInstances.

    Also reads the collection that every mutation answers with — v1's
    `clearTaskInstances` and `updateTaskInstancesState`, v2's clear and its
    task-instance PATCH (dry run or not) — because all four use the same
    `task_instances` envelope with fewer fields, which is how a mutation reports
    what it touched. That shared shape is why the outcome line needs no version.
    (`.../tries` is *not* this shape: it nests under `task_instances_history`.)
    """
    out: list[TaskInstance] = []
    for row in _rows(payload, "task_instances"):
        out.append(
            TaskInstance(
                task_id=_text(row.get("task_id"), "?"),
                state=_text(row.get("state"), "none"),
                try_number=_int(row.get("try_number")),
                max_tries=_int(row.get("max_tries")),
                operator=_text(row.get("operator")),
                start_date=_dt(row.get("start_date")),
                end_date=_dt(row.get("end_date")),
                pool=_text(row.get("pool")),
                map_index=_int(row.get("map_index"), -1),
            )
        )
    return out


def _str_tuple(value: object) -> tuple[str, ...]:
    """A list-of-strings field, skipping anything that isn't one."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _schedule(value: object) -> str:
    """v1 spells a schedule as `{"__type": "CronExpression", "value": "23 7 * * *"}`
    (or None for an unscheduled DAG)."""
    if not isinstance(value, dict):
        return ""
    return _text(value.get("value"))


def _tag_names(value: object) -> tuple[str, ...]:
    """Tags arrive as `[{"name": "cs"}, …]` in both majors."""
    if not isinstance(value, list):
        return ()
    names = (_text(item.get("name")) for item in value if isinstance(item, dict))
    return tuple(name for name in names if name)


# The three DAG fields the two majors disagree about, read per major. Kept as
# two separate readers rather than one field-name lookup table so each version's
# spellings can be seen whole, next to what they mean.


def _v1_dag_fields(row: dict) -> tuple[bool, datetime | None, str]:
    """`(is_active, next_dagrun, schedule)` out of a v1 DAG row.

    `is_active` is missing from some 2.x builds; absent means "present", since a
    stale DAG is the exceptional case.
    """
    return (
        bool(row.get("is_active", True)),
        _dt(row.get("next_dagrun")),
        _schedule(row.get("schedule_interval")) or _text(row.get("timetable_description")),
    )


def _v2_dag_fields(row: dict) -> tuple[bool, datetime | None, str]:
    """`(is_active, next_dagrun, schedule)` out of a v2 DAG row.

    Airflow 3 renamed all three and **inverted the first**: `is_stale` where v1
    said `is_active`, so reading the wrong one would report every DAG as stale.
    Absent still means "not stale", matching v1's lenient default. The schedule
    is a plain cron string in `timetable_summary` rather than v1's typed object,
    with `timetable_description` (prose, e.g. "At 07:23") as the fallback both
    majors share.
    """
    return (
        not bool(row.get("is_stale", False)),
        _dt(row.get("next_dagrun_run_after")),
        _text(row.get("timetable_summary")) or _text(row.get("timetable_description")),
    )


def parse_dags(payload: object, version: str) -> list[Dag]:
    """`GET /dags` → Dags. Everything but the three renamed fields is shared."""
    read_renamed = _v2_dag_fields if _serves_v2(version) else _v1_dag_fields
    out: list[Dag] = []
    for row in _rows(payload, "dags"):
        is_active, next_dagrun, schedule = read_renamed(row)
        out.append(
            Dag(
                dag_id=_text(row.get("dag_id"), "?"),
                is_paused=bool(row.get("is_paused")),
                owners=_str_tuple(row.get("owners")),
                tags=_tag_names(row.get("tags")),
                has_import_errors=bool(row.get("has_import_errors")),
                next_dagrun=next_dagrun,
                description=_text(row.get("description")),
                is_active=is_active,
                # Matched against `/importErrors` filenames so a stale DAG's
                # leftover `has_import_errors` flag is not shown as a live error.
                fileloc=_text(row.get("fileloc")),
                schedule=schedule,
            )
        )
    return out


def parse_task_graph(payload: object) -> dict[str, tuple[str, ...]]:
    """`GET /dags/{dag_id}/tasks` → `{task_id: downstream_task_ids}`.

    The adjacency map only; everything the task pane does with it — topological
    ordering, the tree prefixes, cycle handling — is a pure function over this
    shape in `models.order_task_instances`, so it needs no I/O to test.
    """
    graph: dict[str, tuple[str, ...]] = {}
    for row in _rows(payload, "tasks"):
        task_id = _text(row.get("task_id"))
        if not task_id:
            continue
        graph[task_id] = _str_tuple(row.get("downstream_task_ids"))
    return graph


def parse_import_errors(payload: object) -> list[ImportErrorEntry]:
    out: list[ImportErrorEntry] = []
    for row in _rows(payload, "import_errors"):
        out.append(
            ImportErrorEntry(
                filename=_text(row.get("filename"), "?"),
                stacktrace=_text(row.get("stack_trace")),
                timestamp=_dt(row.get("timestamp")),
            )
        )
    return out


def parse_log(payload: object, try_number: int, version: str) -> TaskLog:
    """`GET .../logs/{n}` → TaskLog. The one parser where the two majors return
    genuinely different *kinds* of thing, not just different names.

    v1 hands back `content` as the *repr of a list of (host, text) tuples*
    rather than plain text, e.g. ``[('', ' INFO - ...\\n')]``. v2 hands back a
    JSON array of structured events. Either way this returns readable lines, and
    either way an unexpected shape shows something rather than nothing.

    `continuation_token` is read the same way in both: it exists in both schemas
    and is carried, not used (see `models.TaskLog`).
    """
    if not isinstance(payload, dict):
        return TaskLog(content="", try_number=try_number)
    token = payload.get("continuation_token")
    raw = payload.get("content")
    read = _flatten_log_events if _serves_v2(version) else _unwrap_log_content
    return TaskLog(
        content=read(raw),
        try_number=try_number,
        continuation_token=token if isinstance(token, str) else None,
    )


def _unwrap_log_content(raw: object) -> str:
    """Turn v1's `[(host, text), …]` repr into the text a human wants to read."""
    if isinstance(raw, list):  # some builds return the structure, not its repr
        return "".join(_log_chunk(chunk) for chunk in raw)
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not (text.startswith("[") and text.endswith("]")):
        return raw
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return raw
    if not isinstance(parsed, list):
        return raw
    return "".join(_log_chunk(chunk) for chunk in parsed)


def _log_chunk(chunk: object) -> str:
    """One (host, text) pair's text, or the chunk itself if it isn't a pair."""
    if isinstance(chunk, (tuple, list)) and len(chunk) == 2:
        return chunk[1] if isinstance(chunk[1], str) else str(chunk[1])
    return chunk if isinstance(chunk, str) else str(chunk)


def _flatten_log_events(raw: object) -> str:
    """Turn v2's structured log events into one readable line each.

    Airflow 3 answers with a JSON array of event objects
    (`{"event": …, "timestamp": …, "level": …, "logger": …, …}`) instead of v1's
    text blob. The log pane renders lines and the `/` filter matches lines, so
    the events are flattened rather than pretty-printed: the fields a human
    reads (`timestamp level event`) are kept in that order and the rest dropped.

    Anything that is not the expected array — a plain string body, an older
    shape, junk — is shown as-is rather than swallowed.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return str(raw)
    return "\n".join(_log_event(item) for item in raw)


def _log_event(item: object) -> str:
    """One v2 log event as a line. Bare strings in the array pass through."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    event = item.get("event")
    message = _text(event) or ("" if event is None else str(event))
    if not message:
        return str(item)  # an event we cannot read still shows something
    parts = (_text(item.get("timestamp")), _text(item.get("level")), message)
    return " ".join(part for part in parts if part)


def parse_error_detail(payload: object) -> str | None:
    """The human-readable message out of an Airflow error body.

    v1 answers failures with an RFC-7807 problem document
    (`{"title": …, "detail": …, "status": 404}`) and v2 with FastAPI's
    `{"detail": …}`; either way the `detail` is the only part worth showing a
    user. Version-free because both shapes are read by the same lookup — except
    for one v2 addition, below.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("detail", "title", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _validation_detail(payload.get("detail"))


def _validation_detail(detail: object) -> str | None:
    """FastAPI reports a *validation* failure as a list, not a string.

    `[{"type": …, "loc": ["body", "logical_date"], "msg": "Field required"}, …]`
    — which the string lookup above skips straight past, leaving the user with
    a raw JSON tail. The first entry is what says what went wrong; the rest are
    counted, not printed.
    """
    if not isinstance(detail, list) or not detail:
        return None
    first = detail[0]
    if not isinstance(first, dict):
        return str(first)
    message = _text(first.get("msg")) or _text(first.get("type")) or "invalid request"
    location = first.get("loc")
    where = (
        ".".join(str(part) for part in location) if isinstance(location, list) else ""
    )
    more = f" (+{len(detail) - 1} more)" if len(detail) > 1 else ""
    return f"{where}: {message}{more}" if where else f"{message}{more}"
