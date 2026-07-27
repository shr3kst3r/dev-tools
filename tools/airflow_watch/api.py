"""The Airflow API version seam — the *only* module that knows a version exists.

Per the airflow-2-only-behind-a-version-seam ADR, every piece of
version-dependent knowledge lives here and nowhere else:

* the base path (`/api/v1`),
* every endpoint path,
* every query-parameter name (`execution_date_*`, `only_active`, …),
* every request-body field name (`logical_date`, `dry_run`, …),
* and the response→model mapping.

No other module contains an ``/api/v`` literal, a version conditional, or a
v1-only field name. `tests/test_airflow_watch.py::test_no_api_version_literal_outside_api_module`
enforces that by grepping the package, so a reviewer does not have to.

Airflow 3 support is an *extension* of this module: a second set of builders and
parsers chosen by `supports()`/`base_path()`, with the refusal path already in
place. Until then `supports()` returns False for 3.x and the discovery boundary
refuses those targets by name.

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
    "assumed_version",
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
    "mark_task_state_path",
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
    "tasks_path",
    "supported_range",
    "supports",
    "task_instances_path",
    "total_entries",
    "trigger_body",
    "trigger_run_path",
    "unsupported_message",
]

# The one API-version literal in the tool. Airflow 2.x serves this; Airflow 3.x
# removed it entirely in favour of /api/v2, which is why a 3.x target is refused
# rather than attempted.
_V1_BASE_PATH = "/api/v1"

# The Airflow major versions this build understands, as a closed set — unlike
# task and run states, which are deliberately open.
_SUPPORTED_MAJORS = (2,)

# The spec version to pin when the target's real version is unknowable, which
# only happens for a plain `--api-url` Airflow. 2.11 is the final 2.x line, and
# one of the OpenAPI specs the `astro` CLI bundles.
_DEFAULT_SPEC_VERSION = "2.11.0"

# Airflow 2's default page limit is 100; we ask explicitly everywhere so a
# version that changes the default cannot silently change what we show. (Airflow
# 3 changed the default to 50, which is exactly why this lives here.)
DEFAULT_LIMIT = 50

# The largest page v1 will serve, whatever you ask for: `maximum_page_limit`
# defaults to 100 and a request for more is silently truncated to it. Callers
# that need everything must page with `offset`, because the `astro` CLI's own
# `--paginate`/`--slurp` do not paginate this API (verified against 2.11: it
# returns page one and drops `total_entries`).
PAGE_LIMIT = 100

# Newest-first, using v1's date field name. Airflow 3 renamed this to
# `logical_date` / `run_after`, which is exactly the kind of knowledge that
# must not leak out of this module.
_RUNS_ORDER_BY = "-execution_date"


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


def assumed_version() -> str:
    """The version to assume when there is nothing to discover it from.

    Only a plain `--api-url` Airflow reaches this: Astro discovery always
    reports `airflowVersion`. It must be a *concrete* version because it is
    handed straight to `astro --airflow-version`, which uses it to pick a
    bundled OpenAPI spec. Naming the assumption here keeps it inside the seam —
    no caller spells a version number itself.
    """
    return _DEFAULT_SPEC_VERSION


def unsupported_message(version: str) -> str:
    """Why we are declining, naming the detected version.

    Deliberately an honest refusal rather than a degraded attempt: Airflow 3
    serves a different API with renamed filters and removed endpoints, so a
    monitoring tool that guessed would misreport state.
    """
    shown = version.strip() or "unknown"
    return (
        f"Airflow {shown} is not supported — airflow-watch speaks "
        f"{supported_range()} only. Airflow 3 serves a different API "
        f"(/api/v2) with no compatibility shim."
    )


def base_path(version: str) -> str:
    """The API base path for an Airflow version, or raise."""
    if not supports(version):
        raise UnsupportedAirflowVersion(version)
    return _V1_BASE_PATH


def api_url_for(url: str, version: str) -> str:
    """Normalize a user-supplied Airflow base URL for `astro --api-url`.

    Astro's discovery already reports an `apiUrl` carrying the version suffix,
    but a human typing `--api-url https://airflow.example.com` has not. Append
    the base path when it is missing so both spellings work, and raise for an
    unsupported version rather than constructing a URL we cannot read.
    """
    suffix = base_path(version)
    trimmed = url.rstrip("/")
    if trimmed.endswith(suffix):
        return trimmed
    return trimmed + suffix


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

    Repeated keys matter: v1's `state` filter is an array parameter, so
    `?state=failed&state=running` is how you ask for two states.
    """
    kept = [(key, value) for key, value in params if value != ""]
    if not kept:
        return path
    return f"{path}?{urlencode(kept)}"


def dags_path(
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    dag_id_pattern: str = "",
) -> str:
    """List DAGs — *all* of them, including paused and stale ones.

    `only_active` is v1's name for "hide DAGs whose file is gone" (Airflow 3
    renamed it `exclude_stale`) and it **defaults to true**, which silently hides
    rows. A monitoring tool must not do that: we send `only_active=false`
    explicitly and let the UI label a stale DAG instead of omitting it. Paused
    DAGs are never filtered either — `is_paused` is shown, not applied.

    `dag_id_pattern` is v1's server-side substring match on the dag id, used when
    the full list is too large to have loaded client-side.
    """
    return _with_query(
        "/dags",
        [
            ("limit", str(limit)),
            ("offset", str(offset) if offset else ""),
            ("only_active", "false"),
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
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    states: tuple[str, ...] = (),
    order_by: str = _RUNS_ORDER_BY,
) -> str:
    """Recent runs, newest first. `dag_id="~"` is v1's cross-DAG wildcard —
    one call for the whole deployment's run history, which is what makes the
    primary view a single request."""
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
    """One attempt's log. `full_content=true` asks v1 for the whole body rather
    than a pointer, which is what we want since we cannot stream.

    `map_index` is required for a *mapped* task instance: v1 looks the instance
    up by `(dag_id, run_id, task_id, map_index)` and defaults the last to `-1`,
    so omitting it on a mapped task returns 404 "TaskInstance not found" rather
    than the log. `-1` is v1's own default and our sentinel for "not mapped", so
    it is left off the path in that case. There is no path-segment form of this
    for logs — only the query parameter.
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
    """v1's dedicated clear endpoint. Airflow 3 removed it in favour of a
    generic PATCH."""
    return f"/dags/{_segment(dag_id)}/clearTaskInstances"


def mark_task_state_path(dag_id: str) -> str:
    """v1's dedicated set-state endpoint, likewise removed in Airflow 3."""
    return f"/dags/{_segment(dag_id)}/updateTaskInstancesState"


# --- request bodies --------------------------------------------------------
#
# v1 field names. `dry_run` is always sent explicitly: it defaults to *true* on
# both clear and set-state, so a body that omits it returns 200 and does
# nothing at all.


def pause_body(paused: bool) -> dict[str, object]:
    return {"is_paused": paused}


def trigger_body(
    logical_date: datetime | None = None,
    conf: dict[str, object] | None = None,
) -> dict[str, object]:
    """Trigger payload. v1 accepts `logical_date`; the older `execution_date`
    spelling is gone from Airflow 3, so we never send it."""
    body: dict[str, object] = {"conf": conf or {}}
    if logical_date is not None:
        body["logical_date"] = logical_date.isoformat()
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
    """Set-state payload.

    Note the asymmetry with `clear_body`, which is v1's and not ours: the clear
    endpoint takes a `task_ids` *array*, while set-state takes a single
    **`task_id`** string and expands from it via the four `include_*` flags. A
    body carrying `task_ids` here fails validation (`unknown field`, and
    `task_id` missing) — which is why one task at a time is the caller's
    contract, enforced in `astro._mutation_request`.

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

    v1 reports this as `total_entries` on every list envelope; a response that
    omits it reads as 0, which callers treat as "one page is all there is".
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

    Also reads the `TaskInstanceReferenceCollection` that `clearTaskInstances`
    and `updateTaskInstancesState` answer with — same `task_instances` envelope,
    fewer fields — which is how a mutation reports what it touched. (`.../tries`
    is *not* this shape: it nests under `task_instances_history`.)
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
    """v1 spells tags as `[{"name": "cs"}, …]`."""
    if not isinstance(value, list):
        return ()
    names = (_text(item.get("name")) for item in value if isinstance(item, dict))
    return tuple(name for name in names if name)


def parse_dags(payload: object) -> list[Dag]:
    out: list[Dag] = []
    for row in _rows(payload, "dags"):
        out.append(
            Dag(
                dag_id=_text(row.get("dag_id"), "?"),
                is_paused=bool(row.get("is_paused")),
                owners=_str_tuple(row.get("owners")),
                tags=_tag_names(row.get("tags")),
                has_import_errors=bool(row.get("has_import_errors")),
                next_dagrun=_dt(row.get("next_dagrun")),
                description=_text(row.get("description")),
                # v1 omits `is_active` in some builds; absent means "present",
                # since a stale DAG is the exceptional case.
                is_active=bool(row.get("is_active", True)),
                # Matched against `/importErrors` filenames so a stale DAG's
                # leftover `has_import_errors` flag is not shown as a live error.
                fileloc=_text(row.get("fileloc")),
                schedule=_schedule(row.get("schedule_interval"))
                or _text(row.get("timetable_description")),
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


def parse_log(payload: object, try_number: int) -> TaskLog:
    """`GET .../logs/{n}` → TaskLog.

    v1 returns `content` as the *repr of a list of (host, text) tuples* rather
    than plain text, e.g. ``[('', ' INFO - ...\\n')]``. We unwrap that into
    readable lines when it parses and fall back to the raw string when it does
    not, so an unexpected shape shows something rather than nothing.
    """
    if not isinstance(payload, dict):
        return TaskLog(content="", try_number=try_number)
    token = payload.get("continuation_token")
    return TaskLog(
        content=_unwrap_log_content(payload.get("content")),
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


def parse_error_detail(payload: object) -> str | None:
    """The human-readable message out of an Airflow error body.

    v1 answers failures with an RFC-7807 problem document
    (`{"title": …, "detail": …, "status": 404}`); the `detail` is the only part
    worth showing a user.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("detail", "title", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
