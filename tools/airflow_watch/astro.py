"""Airflow and Astro access for airflow-watch, via the `astro` CLI.

The only module in the tool that performs I/O, and the only one that spawns a
process. Mirrors `tools/pr_watch/github.py`: a single thin `_run()` wrapper, a
`require_astro()` preflight, and a pure classification step that turns raw
subprocess failures into typed, user-facing `PollError`s so no stderr ever
reaches the UI.

Why the CLI rather than HTTP (see the airflow-access-via-astro-cli ADR): Astro's
ingress authenticates *every* path, its session tokens are 60-minute JWTs the
CLI already refreshes silently, and the credential cache has an undocumented
schema. Owning that is the largest chunk of risk in this tool, and the CLI
demonstrably already does it. The repo has no HTTP client dependency and this
tool adds none.

Two hard rules this module enforces:

* **Every `astro api airflow` invocation passes `--airflow-version`**, taken
  from discovery. Pinning it is what takes a call from ~1.9s to ~0.75s, so it
  is a correctness-of-performance requirement rather than a tweak.
* **No bearer token may reach a message, a log line, or a debug pane.**
  `_redact` scrubs JWT-shaped strings and `Authorization:` values out of
  anything displayable, and `astro auth token` / `astro api --generate` — both
  of which print live credentials on stdout — are never invoked.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import api
from .models import (
    Action,
    Dag,
    DagList,
    DagRun,
    Deployment,
    Snapshot,
    TaskInstance,
    TaskLog,
    TaskRow,
    order_task_instances,
    sort_runs,
)

__all__ = [
    "KINDS",
    "AstroError",
    "Pages",
    "PollError",
    "RunTasks",
    "airflow_call",
    "airflow_get",
    "classify_astro_error",
    "fetch_log",
    "fetch_run_tasks",
    "fetch_snapshot",
    "list_deployments",
    "perform",
    "require_astro",
    "resolve_deployment",
]

# The binary we shell out to. Named once so tests can assert on argv[0].
ASTRO = "astro"

# A single call's ceiling. A cold `astro api airflow` with the version pinned
# lands near 0.75s; 30s means "something is wrong", not "be patient".
DEFAULT_TIMEOUT = 30.0

# Log fetches can be large and are worth waiting a little longer for.
LOG_TIMEOUT = 60.0

# How much of one attempt's log we are willing to keep in memory. v1 returns the
# whole body in one response — there is no server-side offset we can mint, since
# the continuation token is signed with the webserver's secret — so this is the
# boundary that keeps a runaway log (a retry loop printing a stack trace per
# second for a day) from being held, re-parsed and searched in full. ~2 MB is
# far more than the pane renders (`ui.MAX_LOG_LINES`) and enough to hold any
# normal task's log whole.
MAX_LOG_CHARS = 2_000_000

# How many `astro` processes may be in flight at once. Six calls serial measured
# 4.47s against 1.12s parallel, so fanning out is what keeps the tool from
# feeling broken; the cap keeps us from stampeding a webserver humans also use.
MAX_WORKERS = 8

# v1 caps a page at `api.PAGE_LIMIT` whatever you ask for — a `limit=1000` request
# returns 100 records and no error — and the CLI cannot paginate for us
# (`--paginate --slurp` was verified against 2.11 to return page one and drop
# `total_entries`). So every list is paged by `offset` here, off the server's own
# `total_entries`, and never by guessing from `len(rows) < limit`.
#
# These ceilings bound one poll's cost. Hitting one truncates the list *visibly*
# — the Snapshot carries the server's total, and the UI says "N of M" — because a
# truncation nobody can see is the bug being fixed.
MAX_DAG_PAGES = 12
MAX_RUN_PAGES = 8
MAX_TASK_PAGES = 12


class AstroError(RuntimeError):
    """A user-actionable problem talking to the `astro` CLI or to Airflow.

    Raised at the I/O boundary and never shown directly: `classify_astro_error`
    converts it to a `PollError` first, which is what strips command noise and
    credentials.
    """


@dataclass(frozen=True, slots=True)
class PollError:
    """A classified failure, ready for the UI to show and act on.

    `kind` is the discriminator the dashboard renders distinct states from — see
    `KINDS` below for the full set. `message` is a concise, user-facing line
    that has already been redacted and never contains the raw command.
    """

    message: str
    kind: str = "unknown"
    retry_after: int | None = None

    @property
    def rate_limited(self) -> bool:
        """Whether this is the case worth backing off for (drives the app's
        exponential backoff, exactly as in my-prs)."""
        return self.kind == "rate_limited"

    @property
    def recoverable(self) -> bool:
        """False when retrying on a timer cannot possibly help — a missing
        binary, a refused version, an absent deployment. The dashboard still
        shows these, it just says so rather than implying a retry."""
        return self.kind not in ("missing_cli", "unsupported_version", "not_found")


# Every failure mode gets its own kind so the UI can say something specific
# instead of "error". Ordered as the classifier tests them; the test suite
# asserts every one of these is reachable, so the taxonomy cannot rot.
KINDS = (
    "missing_cli",
    "auth",
    "hibernating",
    "unsupported_version",
    "rate_limited",
    "forbidden",
    "not_found",
    "unknown",
)

# How much of a raw stderr tail we are willing to show. Long enough to be
# useful, short enough not to swamp a one-line summary bar.
_MAX_DETAIL = 240

# JWT-shaped strings: three base64url segments, the first of which is a base64
# `{"` — i.e. what an Astro session token looks like. Matched greedily enough
# that a token embedded in a longer line still goes.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?")

# `Authorization: Bearer …` in any casing, header or curl form.
_AUTH_HEADER = re.compile(
    r"(authorization\s*[:=]\s*)(?:bearer\s+)?\S+", re.IGNORECASE
)

# A bare `Bearer <token>` with no header name in front of it.
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)

REDACTED = "[redacted]"


def _redact(text: str) -> str:
    """Strip anything credential-shaped out of text that might be displayed.

    Applied to every `PollError.message` and every activity-log entry, because
    an error path is exactly where a token is most likely to be echoed back at
    us — Airflow's own error bodies quote request headers, and the CLI's verbose
    modes print whole requests. This is an ADR constraint, not a nicety.
    """
    scrubbed = _AUTH_HEADER.sub(rf"\1{REDACTED}", text)
    scrubbed = _BEARER.sub(f"Bearer {REDACTED}", scrubbed)
    return _JWT.sub(REDACTED, scrubbed)


def _display_command(args: list[str]) -> str:
    """A readable one-line form of a command, for the raw AstroError only.

    Long arguments are truncated (a log path with an encoded run id is
    unreadable at full length), and the whole thing is redacted, so even the
    pre-classification error text cannot carry a credential.
    """
    parts = [arg if len(arg) <= 60 else arg[:57] + "…" for arg in args]
    return _redact(" ".join(parts))


def _run(
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
) -> str:
    """Run an `astro` command and return its stdout, or raise AstroError.

    The single subprocess seam in the tool — tests monkeypatch exactly this.

    `astro` reports HTTP failures in two places at once: the status line goes to
    stderr (`Error: API request failed with status 404`) while Airflow's problem
    document goes to *stdout*. Both are folded into the raised error so the
    classifier can key off the status and still quote Airflow's own `detail`.

    A body is passed via `--input -` on stdin rather than `-f`/`-F`, because
    those flags silently flip the request method to POST.
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            input=input_text,
        )
    except FileNotFoundError as exc:  # pragma: no cover - env dependent
        raise AstroError(f"`{args[0]}` is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AstroError(
            f"`{_display_command(args)}` timed out after {timeout:.0f}s."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join(
            part.strip() for part in (exc.stderr, exc.stdout) if part and part.strip()
        )
        raise AstroError(f"`{_display_command(args)}` failed: {detail}") from exc
    return proc.stdout


def require_astro() -> None:
    """Fail fast with a clear message if the CLI is missing.

    Deliberately a `shutil.which` check and nothing more, matching
    `require_gh()`: no network at startup. Authentication problems surface
    through the first poll's error path, where they can be retried, rather than
    blocking the app from opening at all.
    """
    if shutil.which(ASTRO) is None:
        raise AstroError(
            "The Astro CLI (`astro`) is required but is not on PATH. Install it "
            "(https://docs.astronomer.io/astro/cli/install-cli), then run "
            "`astro login`."
        )


# --- classification --------------------------------------------------------


def _retry_after(text: str) -> int | None:
    """Pull a `Retry-After: N` hint out of an error, if the server gave one."""
    lowered = text.lower()
    marker = "retry-after"
    if marker not in lowered:
        return None
    tail = text[lowered.index(marker) + len(marker) :].lstrip(" :=\t")
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def _tail(raw: str) -> str:
    """The part of a raw AstroError after the command prefix.

    `_run` formats failures as ```<command>` failed: <detail>``; only the
    detail belongs anywhere near a user.
    """
    if "` failed: " in raw:
        return raw.split("` failed: ", 1)[1].strip()
    if "` timed out" in raw:
        return "the request timed out."
    return raw.strip()


def classify_astro_error(exc: AstroError | api.UnsupportedAirflowVersion) -> PollError:
    """Turn a raw failure into a concise, kinded, redacted `PollError`.

    Mirrors `my_prs.github.classify_github_error`. Every branch here corresponds
    to a row of the plan's error table, and every message names an action the
    user can take.
    """
    if isinstance(exc, api.UnsupportedAirflowVersion):
        return PollError(message=str(exc), kind="unsupported_version")

    raw = str(exc)
    tail = _tail(raw)
    low = tail.lower()

    # Matches both `_run`'s FileNotFoundError text and `require_astro`'s, so the
    # preflight and a mid-flight disappearance classify the same way.
    if "not on path" in low:
        return PollError(
            message="`astro` not found on PATH — install it, then run `astro login`.",
            kind="missing_cli",
        )
    # The CLI's own not-authenticated messages, which are distinctive. An HTTP
    # 401/403 is *not* this case — that is a permissions problem on a session
    # that authenticated fine, and it falls through to `forbidden` below.
    if (
        "no context set" in low
        or "astro login" in low
        or "not logged in" in low
        or "have you authenticated" in low
        or "token is expired" in low
    ):
        return PollError(
            message="Astro authentication failed — run `astro login`.", kind="auth"
        )
    if "hibernat" in low:
        return PollError(
            message="Deployment is hibernating — its Airflow API is not running.",
            kind="hibernating",
        )
    if "429" in low or "too many requests" in low or "rate limit" in low:
        return PollError(
            message="Airflow API rate limit hit — backing off before retrying.",
            kind="rate_limited",
            retry_after=_retry_after(tail),
        )
    if "403" in low or "forbidden" in low or "401" in low or "not authorized" in low:
        return PollError(
            message=(
                "Not permitted to read this deployment's Airflow API — check your "
                "Astro workspace role."
            ),
            kind="forbidden",
        )
    # `resolve_deployment`'s own wording, kept as its own row of the taxonomy:
    # the ADR names "deployment not found" as a mode that must be specific, and
    # `not_found` is what stops the app retrying a name that cannot resolve.
    if "not found" in low or "404" in low or "no deployment matches" in low:
        return PollError(message=_detail_or(tail, "Not found."), kind="not_found")
    return PollError(message=_detail_or(tail, "Unknown `astro` failure."), kind="unknown")


def _detail_or(tail: str, fallback: str) -> str:
    """Airflow's own `detail`, if the tail carries a problem document; otherwise
    the (redacted, truncated) tail itself."""
    detail = _problem_detail(tail)
    text = detail or tail or fallback
    text = _redact(text)
    if len(text) > _MAX_DETAIL:
        text = text[: _MAX_DETAIL - 1] + "…"
    return text


def _problem_detail(text: str) -> str | None:
    """Find and read an embedded RFC-7807 body inside an error tail."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return api.parse_error_detail(payload)


# --- calls -----------------------------------------------------------------


def _cloud_args(operation: str) -> list[str]:
    return [ASTRO, "api", "cloud", operation]


def _airflow_args(
    deployment: Deployment,
    path: str,
    *,
    method: str = "GET",
    with_body: bool = False,
) -> list[str]:
    """Build the argv for one `astro api airflow` call.

    `--airflow-version` is unconditional: it is an ADR constraint, and it is
    what avoids the version-detection round trip on every single call. An Astro
    deployment is addressed by `-d <id>`; a plain Airflow by `--api-url`, which
    is the one place the two platforms differ.

    A non-GET method is stated with `-X` and its body arrives on stdin via
    `--input -`. We never use `-f`/`-F`, which would flip the method to POST
    behind our back.
    """
    version = deployment.airflow_version
    if not api.supports(version):
        raise api.UnsupportedAirflowVersion(version)

    args = [ASTRO, "api", "airflow"]
    if deployment.is_astro:
        args += ["-d", deployment.id]
    else:
        args += ["--api-url", api.api_url_for(deployment.api_url, version)]
    args += ["--airflow-version", version]
    if method != "GET":
        args += ["-X", method]
    if with_body:
        args += ["--input", "-"]
    args.append(path)
    return args


def _json(raw: str) -> dict:
    """Parse a response body, turning junk into an AstroError rather than a
    JSONDecodeError escaping the I/O boundary."""
    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        raise AstroError(
            f"`{ASTRO}` returned a response that is not JSON: {raw[:120]!r}"
        ) from exc
    return payload if isinstance(payload, dict) else {}


def airflow_get(
    deployment: Deployment, path: str, *, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """One read from a deployment's Airflow API."""
    return _json(_run(_airflow_args(deployment, path), timeout=timeout))


def airflow_call(
    deployment: Deployment,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """One call to a deployment's Airflow API, with an optional JSON body."""
    args = _airflow_args(deployment, path, method=method, with_body=body is not None)
    raw = _run(
        args,
        timeout=timeout,
        input_text=json.dumps(body) if body is not None else None,
    )
    return _json(raw)


def list_deployments() -> list[Deployment]:
    """Every deployment in the org, with its Airflow version.

    One call answers discovery entirely — including `airflowVersion`, which is
    what lets every later call pin the version and skip auto-detection.
    """
    return api.parse_deployments(_json(_run(_cloud_args("ListDeployments"))))


def resolve_deployment(
    deployments: list[Deployment], wanted: str | None
) -> Deployment:
    """Pick the deployment the user asked for, by id or (case-insensitive) name.

    Raises with an actionable message rather than guessing: an ambiguous name or
    a typo should say what the options were.
    """
    if not deployments:
        raise AstroError(
            "No Astro deployments visible to this account — check `astro login` "
            "and your workspace access."
        )
    if wanted is None:
        return deployments[0]
    for deployment in deployments:
        if deployment.id == wanted:
            return deployment
    lowered = wanted.casefold()
    matches = [d for d in deployments if d.name.casefold() == lowered]
    if not matches:
        matches = [d for d in deployments if lowered in d.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(d.name for d in deployments)
    if not matches:
        raise AstroError(f"No deployment matches {wanted!r}. Available: {known}.")
    raise AstroError(
        f"{wanted!r} matches more than one deployment "
        f"({', '.join(m.name for m in matches)}) — use the deployment id."
    )


def _require_supported(deployment: Deployment) -> None:
    """Refuse a non-2.x target at the discovery boundary, by name.

    The refusal happens here rather than at the first request because a wrong
    API version on Astro looks like a 404 *after* auth, which is indistinguish-
    able from a missing DAG — an obscure failure the ADR exists to prevent.
    """
    if not api.supports(deployment.airflow_version):
        raise api.UnsupportedAirflowVersion(deployment.airflow_version)


@dataclass(frozen=True, slots=True)
class Pages:
    """The result of paging one list endpoint.

    `total` is what the server said exists; `truncated` says we stopped short of
    it at our own ceiling. Both travel with the data so no caller can mistake a
    partial list for a complete one.
    """

    payloads: tuple[dict, ...]
    calls: int
    total: int
    truncated: bool = False


def _page_offsets(total: int, want: int | None, max_pages: int) -> list[int]:
    """The offsets to request after page one, and whether we stopped short.

    `want` is how many records the caller asked for (None = all of them). Pages
    are computed from the server's `total_entries` rather than inferred from a
    short page, because v1 truncates silently and a full page is not evidence of
    more.
    """
    reachable = total if want is None else min(total, want)
    return list(range(api.PAGE_LIMIT, reachable, api.PAGE_LIMIT))[: max_pages - 1]


def _paged(
    deployment: Deployment,
    build_path: Callable[[int, int], str],
    first_page: dict,
    *,
    want: int | None,
    max_pages: int,
    pool: ThreadPoolExecutor,
) -> Pages:
    """Fetch every remaining page of a list, in parallel, given its first page.

    Two rounds rather than a serial walk: only page one reveals `total_entries`,
    but once it has, the rest go out together — which is what keeps a 242-DAG
    deployment at roughly the cost of one call rather than three.
    """
    total = api.total_entries(first_page)
    offsets = _page_offsets(total, want, max_pages)
    held = min(total, want) if want is not None else total
    truncated = len(offsets) + 1 < _pages_needed(held)
    if not offsets:
        return Pages(
            payloads=(first_page,), calls=0, total=total, truncated=truncated
        )
    futures = [
        pool.submit(airflow_get, deployment, build_path(api.PAGE_LIMIT, offset))
        for offset in offsets
    ]
    payloads = [first_page] + [future.result() for future in futures]
    return Pages(
        payloads=tuple(payloads),
        calls=len(offsets),
        total=total,
        truncated=truncated,
    )


def _pages_needed(records: int) -> int:
    return max(1, -(-records // api.PAGE_LIMIT))


def fetch_snapshot(
    deployment: Deployment,
    *,
    limit: int = api.DEFAULT_LIMIT,
    states: tuple[str, ...] = (),
    deployments: list[Deployment] | None = None,
    dags: DagList | None = None,
    dag_pattern: str = "",
) -> Snapshot:
    """One poll of a deployment: runs, DAGs and import errors, fanned out.

    The calls run concurrently rather than in sequence, because serial fan-out
    measured 4.47s for six calls against 1.12s parallel — the difference between
    a tool that feels alive and one that feels stuck.

    Passing `dags` reuses an already-known DAG list and skips fetching it. That
    matters because the DAG list is the expensive part (a 100-record page of DAG
    objects is far heavier than a page of runs, and a large deployment needs
    several), while it only changes on a deploy — whereas runs change constantly.
    The caller owns that policy; see `cli._DagCache`. It is a `DagList` rather
    than a bare tuple so a reused list keeps saying it was truncated.
    """
    _require_supported(deployment)
    if deployment.is_hibernating:
        raise AstroError(
            f"Deployment {deployment.name} is hibernating — no webserver is running."
        )

    def runs_path(page: int, offset: int) -> str:
        return api.dag_runs_path(limit=page, offset=offset, states=states)

    def dags_path(page: int, offset: int) -> str:
        return api.dags_path(limit=page, offset=offset, dag_id_pattern=dag_pattern)

    started = time.monotonic()
    first: dict[str, str] = {
        "runs": runs_path(min(limit, api.PAGE_LIMIT), 0),
        "errors": api.import_errors_path(limit=api.PAGE_LIMIT),
    }
    if dags is None:
        first["dags"] = dags_path(api.PAGE_LIMIT, 0)

    known = dags or DagList()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            name: pool.submit(airflow_get, deployment, path)
            for name, path in first.items()
        }
        payloads = {name: future.result() for name, future in futures.items()}
        # Only page one reveals how many pages there are, so the rest go out in a
        # second parallel round rather than one at a time.
        run_pages = _paged(
            deployment,
            runs_path,
            payloads["runs"],
            want=limit,
            max_pages=MAX_RUN_PAGES,
            pool=pool,
        )
        dag_pages = (
            _paged(
                deployment,
                dags_path,
                payloads["dags"],
                want=None,
                max_pages=MAX_DAG_PAGES,
                pool=pool,
            )
            if dags is None
            else None
        )
        if dag_pages is not None:
            collected: dict[str, Dag] = {}
            for payload in dag_pages.payloads:
                for dag in api.parse_dags(payload):
                    collected.setdefault(dag.key, dag)  # see `by_key` below
            known = DagList(
                dags=tuple(collected.values()),
                total=dag_pages.total,
                truncated=dag_pages.truncated,
            )
    elapsed = time.monotonic() - started

    # Keyed rather than appended, because offset paging *can* repeat a record:
    # the runs list is ordered by a date thousands of runs share, and rows are
    # being inserted while we walk the offsets, so a row can land on two pages.
    # A repeat would be a double-counted run and — since the UI keys table rows
    # by `run.key` — a DuplicateKey crash on the next render.
    by_key: dict[str, DagRun] = {}
    for payload in run_pages.payloads:
        for run in api.parse_dag_runs(payload):
            by_key.setdefault(run.key, run)
    collected_runs = list(by_key.values())
    errors = api.parse_import_errors(payloads["errors"])
    calls = len(first) + run_pages.calls + (dag_pages.calls if dag_pages else 0)
    return Snapshot(
        deployment=deployment,
        deployments=tuple(deployments or [deployment]),
        runs=tuple(sort_runs(collected_runs[:limit])),
        dags=known.dags,
        import_errors=tuple(errors),
        calls=calls,
        elapsed=elapsed,
        runs_total=run_pages.total,
        dags_total=known.total or len(known.dags),
        dags_truncated=known.truncated,
    )


@dataclass(frozen=True, slots=True)
class RunTasks:
    """One run's task instances, placed in the DAG's dependency order.

    `graph` comes back so the caller can cache it: a DAG's structure changes only
    on deploy, while its runs change constantly, so refetching it per drill-down
    would be a wasted process spawn every time.
    """

    tasks: tuple[TaskInstance, ...]
    rows: tuple[TaskRow, ...]
    total: int
    truncated: bool
    graph: dict[str, tuple[str, ...]]
    calls: int


def fetch_run_tasks(
    deployment: Deployment,
    run: DagRun,
    *,
    graph: dict[str, tuple[str, ...]] | None = None,
) -> RunTasks:
    """The task instances of one run, in dependency order — the first drill step.

    The DAG's structure (`/dags/{id}/tasks`) is fetched *concurrently* with the
    first page of task instances rather than after it, so showing dependency
    order costs no extra wall clock. A structure fetch that fails is not fatal:
    the pane falls back to start order rather than showing nothing.
    """
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        first_future = pool.submit(
            airflow_get,
            deployment,
            api.task_instances_path(run.dag_id, run.run_id, limit=api.PAGE_LIMIT),
        )
        graph_future = (
            pool.submit(_fetch_task_graph, deployment, run.dag_id)
            if graph is None
            else None
        )
        first_page = first_future.result()
        pages = _paged(
            deployment,
            lambda page, offset: api.task_instances_path(
                run.dag_id, run.run_id, limit=page, offset=offset
            ),
            first_page,
            want=None,
            max_pages=MAX_TASK_PAGES,
            pool=pool,
        )
        graph_calls = 0
        if graph_future is not None:
            graph = graph_future.result()
            graph_calls = 1

    # Deduplicated for the same reason the runs list is: a repeat across pages
    # would double a row and break the table's keying.
    by_key: dict[str, TaskInstance] = {}
    for payload in pages.payloads:
        for task in api.parse_task_instances(payload):
            by_key.setdefault(task.key, task)
    tasks = list(by_key.values())
    return RunTasks(
        tasks=tuple(tasks),
        rows=tuple(order_task_instances(tasks, graph)),
        total=pages.total,
        truncated=pages.truncated,
        graph=graph or {},
        calls=1 + pages.calls + graph_calls,
    )


def _fetch_task_graph(
    deployment: Deployment, dag_id: str
) -> dict[str, tuple[str, ...]]:
    """A DAG's `{task_id: downstream_task_ids}`, or `{}` if it cannot be read.

    Swallows the failure on purpose: dependency ordering is an improvement on
    start order, not a precondition for showing the run at all. A DAG whose file
    has been deleted still has task instances worth reading.
    """
    try:
        return api.parse_task_graph(
            airflow_get(deployment, api.tasks_path(dag_id))
        )
    except AstroError:
        return {}


def fetch_log(
    deployment: Deployment, run: DagRun, task: TaskInstance, try_number: int
) -> TaskLog:
    """One attempt's log — the second drill-down step, and the end of the
    investigation loop.

    A mapped task instance is addressed by its `map_index`; without it the API
    answers 404 for every mapped task.

    The body arrives whole (the transport cannot stream, per the ADR), so the
    only defence against a pathological log is to stop *holding* one: past
    `MAX_LOG_CHARS` the content is cut and the TaskLog says so, rather than
    keeping tens of megabytes alive for a pane that renders the first few
    thousand lines of it.
    """
    payload = airflow_get(
        deployment,
        api.log_path(
            run.dag_id,
            run.run_id,
            task.task_id,
            try_number,
            map_index=task.map_index,
        ),
        timeout=LOG_TIMEOUT,
    )
    return _bounded(api.parse_log(payload, try_number))


def _bounded(log: TaskLog) -> TaskLog:
    """A TaskLog trimmed to `MAX_LOG_CHARS`, marked when it had to be trimmed.

    The cut lands on a line boundary so the last shown line is not half a line.
    """
    if len(log.content) <= MAX_LOG_CHARS:
        return log
    head = log.content[:MAX_LOG_CHARS]
    cut = head.rfind("\n")
    return dataclasses.replace(
        log, content=head[:cut] if cut > 0 else head, truncated=True
    )


# --- mutations -------------------------------------------------------------
#
# Every one of these is reached only through the app's confirmation modal, and
# every one is recorded in the activity log. `dry_run` is carried on the Action
# and always sent, because v1 defaults it to true.

def _mutation_request(action: Action) -> tuple[str, str, dict[str, object]]:
    """(method, path, body) for one action. Pure — all the version-specific
    naming comes from `api`."""
    match action.kind:
        case "pause" | "unpause":
            return (
                "PATCH",
                api.pause_dag_path(action.dag_id),
                api.pause_body(action.kind == "pause"),
            )
        case "trigger":
            return "POST", api.trigger_run_path(action.dag_id), api.trigger_body()
        case "clear":
            # Both of these endpoints scope by run, and a body that names task
            # ids but *no* run scopes to the task's whole history instead — so an
            # unscoped clear is refused here rather than sent and hoped about.
            return (
                "POST",
                api.clear_task_instances_path(action.dag_id),
                api.clear_body(
                    _scoped_run(action), action.task_ids, dry_run=action.dry_run
                ),
            )
        case "mark":
            # v1's set-state endpoint names a *single* `task_id` (see
            # api.mark_body); refuse rather than silently marking one of several
            # or sending a body the API will reject.
            if len(action.task_ids) != 1:
                raise AstroError(
                    "Marking a task state takes exactly one task instance "
                    f"(asked for {len(action.task_ids)})."
                )
            return (
                "POST",
                api.mark_task_state_path(action.dag_id),
                api.mark_body(
                    _scoped_run(action),
                    action.task_ids[0],
                    action.state,
                    dry_run=action.dry_run,
                ),
            )
        case _:
            raise AstroError(f"Unknown action {action.kind!r}.")


def _scoped_run(action: Action) -> str:
    """The run a clear/set-state must be confined to.

    Not defaulted: with task ids and no run id, Airflow clears or re-states those
    tasks in *every* run of the DAG. The app always drills into a run before
    offering either action, so an absent run id is a programming error — and the
    one place it could do real damage is the one place to refuse it.
    """
    if not action.run_id:
        raise AstroError(
            f"Refusing to {action.kind} {action.dag_id} with no DAG run named — "
            "that would affect every run of the task."
        )
    return action.run_id


def perform(deployment: Deployment, action: Action) -> str:
    """Run a confirmed action and return the one-line outcome to log.

    The returned line is what the activity log records, which is how a user
    answers "did my retry actually fire?" — the question that matters more here
    than in a read-only tool.
    """
    method, path, body = _mutation_request(action)
    payload = airflow_call(deployment, path, method=method, body=body)
    return f"{action.summary} — {_outcome(action, payload)}"


def _outcome(action: Action, payload: dict) -> str:
    """Describe what the response says happened, distinguishing a dry run's
    preview from a real change."""
    affected = api.parse_task_instances(payload)
    if action.kind in ("clear", "mark"):
        noun = "task instance" if len(affected) == 1 else "task instances"
        verb = "would affect" if action.dry_run else "affected"
        return f"{verb} {len(affected)} {noun}"
    if action.kind == "trigger":
        run = api.parse_dag_run(payload)
        return f"created run {run.run_id}" if run is not None else "requested"
    return "ok"
