"""Azure DevOps access for azdo-watch, via the `az` CLI.

The only module in the tool that talks to Azure DevOps, and the only one that
spawns `az` processes (`investigate.py` spawns exactly one other binary — `gw`,
for the run hand-off). Mirrors `airflow_watch/astro.py`: a single thin `_run()`
wrapper, a `require_az()` preflight, and a pure classification step that turns
raw subprocess failures into typed, user-facing `PollError`s so no stderr ever
reaches the UI.

**Why the CLI rather than HTTP.** The same reasoning that put airflow-watch on
`astro`: `az devops` already holds the credential. A PAT lives in the az
credential cache with an undocumented layout, Entra-backed orgs use a bearer
token the CLI refreshes silently, and the repo has no HTTP client dependency and
this tool adds none. `/azdo-pr` — the skill that already does this work by hand —
uses `az devops invoke` for exactly the same reason, so the tool and the skill
share one authentication story.

Two hard rules this module enforces:

* **Every call passes `--api-version`**, from `api.API_VERSION`. Left to the
  extension's default, the timeline and stage resources resolve to whichever
  preview the installed `az devops` prefers, and a monitoring tool that changes
  behaviour on an unrelated `az extension update` is not one you can trust.
* **No credential may reach a message, a log line, or a debug pane.** `_redact`
  scrubs PAT-shaped and JWT-shaped strings and `Authorization:` values out of
  anything displayable, and `az account get-access-token` / `az devops login`
  — both of which handle live credentials — are never invoked.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import api
from .models import (
    Action,
    Pipeline,
    PipelineList,
    Project,
    Record,
    RecordRow,
    Run,
    RunLog,
    Snapshot,
    order_records,
    sort_pipelines,
    sort_runs,
)

__all__ = [
    "KINDS",
    "AZ",
    "AzdoError",
    "PollError",
    "RecordLog",
    "RunBundle",
    "RunTimeline",
    "classify_azdo_error",
    "default_org",
    "default_project",
    "fetch_log",
    "fetch_run_bundle",
    "fetch_run_timeline",
    "fetch_snapshot",
    "invoke",
    "list_projects",
    "perform",
    "require_az",
    "resolve_project",
]

# The binary we shell out to. Named once so tests can assert on argv[0].
AZ = "az"

# A single call's ceiling. A warm `az devops invoke` lands near 1.5s and the
# pipeline inventory near 5s; 60s means "something is wrong", not "be patient" —
# `az` starts a Python interpreter and loads an extension on every call, so the
# floor here is higher than a plain HTTP client's would be.
DEFAULT_TIMEOUT = 60.0

# Log fetches can be large and are worth waiting longer for.
LOG_TIMEOUT = 120.0

# How much of one log we are willing to keep in memory. Azure DevOps returns the
# whole body in one response — `$format=json` has no range parameter `az devops
# invoke` can pass — so this is the boundary that keeps a runaway log (a retry
# loop printing a stack trace per second for an hour) from being held, re-parsed
# and searched in full. ~2 MB is far more than the pane renders
# (`ui.MAX_LOG_LINES`) and enough to hold any normal step's log whole.
MAX_LOG_CHARS = 2_000_000

# How many `az` processes may be in flight at once. Six calls serial measured
# ~9s against 1.7s parallel, so fanning out is what keeps the tool from feeling
# broken; the cap keeps us from starting a dozen Python interpreters at once,
# which is a local cost rather than a service one.
MAX_WORKERS = 6

# How deep a continuation-token walk may go past the first page. Paging is serial
# here (see `api`'s module docstring), so each extra page is another ~2s of wall
# clock — the ceiling is where "scroll for more" stops, and it is reported rather
# than hidden: `Snapshot.runs_more` stays true, so the summary bar keeps saying
# more exist.
MAX_RUN_PAGES = 8
MAX_PIPELINE_PAGES = 4


class AzdoError(RuntimeError):
    """A user-actionable problem talking to the `az` CLI or to Azure DevOps.

    Raised at the I/O boundary and never shown directly: `classify_azdo_error`
    converts it to a `PollError` first, which is what strips command noise and
    credentials.
    """


@dataclass(frozen=True, slots=True)
class PollError:
    """A classified failure, ready for the UI to show and act on.

    `kind` is the discriminator the dashboard renders distinct states from — see
    `KINDS` for the full set. `message` is a concise, user-facing line that has
    already been redacted and never contains the raw command.
    """

    message: str
    kind: str = "unknown"
    retry_after: int | None = None

    @property
    def rate_limited(self) -> bool:
        """Whether this is the case worth backing off for (drives the app's
        exponential backoff, exactly as in my-prs and airflow-watch)."""
        return self.kind == "rate_limited"

    @property
    def recoverable(self) -> bool:
        """False when retrying on a timer cannot possibly help — a missing
        binary, a missing extension, a project that does not exist. The dashboard
        still shows these, it just says so rather than implying a retry."""
        return self.kind not in ("missing_cli", "missing_extension", "not_found")


# Every failure mode gets its own kind so the UI can say something specific
# instead of "error". Ordered as the classifier tests them; the test suite asserts
# every one of these is reachable, so the taxonomy cannot rot.
KINDS = (
    "missing_cli",
    "missing_extension",
    "auth",
    "rate_limited",
    "forbidden",
    "not_found",
    "unknown",
)

# How much of a raw stderr tail we are willing to show. Long enough to be useful,
# short enough not to swamp a one-line summary bar.
_MAX_DETAIL = 240

# A personal access token as Azure DevOps issues them: 52 characters of base32-ish
# alphabet. Matched on length and alphabet because a PAT has no prefix to key off,
# which is also why it is so easy to leak into a log.
_PAT = re.compile(r"\b[a-z2-7]{52}\b", re.IGNORECASE)

# JWT-shaped strings, for the Entra-backed orgs where the CLI holds a bearer token.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?")

# `Authorization: Bearer …` in any casing, header or curl form.
_AUTH_HEADER = re.compile(r"(authorization\s*[:=]\s*)(?:bearer\s+)?\S+", re.IGNORECASE)

# A bare `Bearer <token>` with no header name in front of it.
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)

REDACTED = "[redacted]"


def _redact(text: str) -> str:
    """Strip anything credential-shaped out of text that might be displayed.

    Applied to every `PollError.message` and every activity-log entry, because an
    error path is exactly where a credential is most likely to be echoed back at
    us — `az`'s own `--debug` output prints whole requests, and a 401 body can
    quote the header it rejected.
    """
    scrubbed = _AUTH_HEADER.sub(rf"\1{REDACTED}", text)
    scrubbed = _BEARER.sub(f"Bearer {REDACTED}", scrubbed)
    scrubbed = _JWT.sub(REDACTED, scrubbed)
    return _PAT.sub(REDACTED, scrubbed)


def _display_command(args: list[str]) -> str:
    """A readable one-line form of a command, for the raw AzdoError only.

    Long arguments are truncated and the whole thing is redacted, so even the
    pre-classification error text cannot carry a credential.
    """
    parts = [arg if len(arg) <= 60 else arg[:57] + "…" for arg in args]
    return _redact(" ".join(parts))


def _run(args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run an `az` command and return its stdout, or raise AzdoError.

    The single subprocess seam in the tool — tests monkeypatch exactly this.

    `az` reports service failures on stderr (`TF400813: The user … is not
    authorized`) while sometimes also printing a partial body on stdout, so both
    are folded into the raised error: the classifier keys off the status text and
    can still quote the service's own `message`.
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # pragma: no cover - env dependent
        raise AzdoError(f"`{args[0]}` is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AzdoError(
            f"`{_display_command(args)}` timed out after {timeout:.0f}s."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join(
            part.strip() for part in (exc.stderr, exc.stdout) if part and part.strip()
        )
        raise AzdoError(f"`{_display_command(args)}` failed: {detail}") from exc
    return proc.stdout


def require_az() -> None:
    """Fail fast with a clear message if the CLI or its extension is missing.

    Deliberately a `shutil.which` check plus one *local* `az extension show`, and
    nothing more, matching `require_gh()` / `require_astro()`: no network at
    startup. Authentication problems surface through the first poll's error path,
    where they can be retried, rather than blocking the app from opening at all.

    The extension check earns its ~1s because the failure it prevents is
    otherwise indecipherable: without `azure-devops`, every `az devops invoke`
    fails with `'devops' is not in the 'az' command group`, which reads like the
    tool is broken rather than like one install step is missing.
    """
    if shutil.which(AZ) is None:
        raise AzdoError(
            "The Azure CLI (`az`) is required but is not on PATH. Install it "
            "(https://learn.microsoft.com/cli/azure/install-azure-cli), then "
            "`az extension add --name azure-devops` and `az devops login`."
        )
    try:
        subprocess.run(
            [AZ, "extension", "show", "--name", "azure-devops"],
            capture_output=True,
            check=True,
            timeout=DEFAULT_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AzdoError(
            "The `azure-devops` extension is required but is not installed. Run "
            "`az extension add --name azure-devops`, then `az devops login`."
        ) from exc
    except FileNotFoundError as exc:  # pragma: no cover - env dependent
        raise AzdoError(f"`{AZ}` is not installed or not on PATH.") from exc


# --- defaults ----------------------------------------------------------------


def _configured_defaults() -> dict[str, str]:
    """What `az devops configure --list` has been set to.

    Read once at startup so the tool needs no flags on a machine where `az devops
    configure --defaults organization=… project=…` has already been run — which is
    the normal state for anyone who has used `/azdo-pr`. A parse failure is not an
    error: it just means no defaults, and the flags take over.
    """
    try:
        raw = _run([AZ, "devops", "configure", "--list"])
    except AzdoError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip()] = value.strip()
    return values


def default_org() -> str:
    """The configured organization, as a bare name (`example-org`), or "".

    Bare because that is what the REST routes and the web URLs want; `az devops
    configure` stores it as a full URL, so the two forms have to be reconciled
    somewhere and here is the only place that knows both.
    """
    return org_name(_configured_defaults().get("organization", ""))


def default_project() -> str:
    return _configured_defaults().get("project", "")


def org_name(value: str) -> str:
    """`example-org` from any of `example-org`, `https://dev.azure.com/example-org`, or that with a
    trailing slash. Accepting all three is the difference between a flag that
    works when you paste a URL into it and one that does not."""
    text = value.strip().rstrip("/")
    if "://" in text:
        text = text.split("://", 1)[1]
        _, _, tail = text.partition("/")
        return tail.split("/", 1)[0]
    return text


def org_url(org: str) -> str:
    """The `--org` argument every call needs. `az devops` requires the URL form
    even though nothing else does."""
    return f"https://dev.azure.com/{org_name(org)}"


# --- classification --------------------------------------------------------


def _retry_after(text: str) -> int | None:
    """Pull a `Retry-After: N` hint out of an error, if the service gave one.

    Azure DevOps does send one when it throttles — its rate limiting is a
    per-user resource quota, and the header says how long the quota needs.
    """
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
    """The part of a raw AzdoError after the command prefix.

    `_run` formats failures as ```<command>` failed: <detail>``; only the detail
    belongs anywhere near a user.
    """
    if "` failed: " in raw:
        return raw.split("` failed: ", 1)[1].strip()
    if "` timed out" in raw:
        return "the request timed out."
    return raw.strip()


def classify_azdo_error(exc: AzdoError | api.AzdoApiError) -> PollError:
    """Turn a raw failure into a concise, kinded, redacted `PollError`.

    Mirrors `astro.classify_astro_error`. Every branch corresponds to a failure a
    user can act on, and every message names the action.
    """
    if isinstance(exc, api.AzdoApiError):
        # A refused request — an unknown action, a missing id. It never reached
        # the service, so it is not a transport failure and must not be retried.
        return PollError(message=str(exc), kind="not_found")

    raw = str(exc)
    tail = _tail(raw)
    low = tail.lower()

    # Matches both `_run`'s FileNotFoundError text and `require_az`'s, so the
    # preflight and a mid-flight disappearance classify the same way.
    if "not on path" in low or "not installed" in low:
        return PollError(
            message="`az` not found on PATH — install the Azure CLI, then "
            "`az extension add --name azure-devops`.",
            kind="missing_cli",
        )
    # The extension being absent is its own kind because its fix is its own
    # command, and because `az` reports it as a *usage* error rather than a
    # service one — which would otherwise land in `unknown`.
    if (
        "azure-devops" in low
        and ("not installed" in low or "extension" in low and "not" in low)
    ) or "is not in the 'az' command group" in low:
        return PollError(
            message="The `azure-devops` extension is missing — run "
            "`az extension add --name azure-devops`.",
            kind="missing_extension",
        )
    # The CLI's own not-authenticated messages, which are distinctive. A 403 is
    # *not* this case — that is a permissions problem on a credential that
    # authenticated fine, and it falls through to `forbidden` below.
    if (
        "az login" in low
        or "az devops login" in low
        or "please run 'az login'" in low
        or "no credentials" in low
        or "token is expired" in low
        or "authentication failed" in low
        or "before you can run this command you need to log in" in low
        or "401" in low
    ):
        return PollError(
            message="Azure DevOps authentication failed — run `az devops login` "
            "with a current PAT.",
            kind="auth",
        )
    if "429" in low or "too many requests" in low or "rate limit" in low:
        return PollError(
            message="Azure DevOps rate limit hit — backing off before retrying.",
            kind="rate_limited",
            retry_after=_retry_after(tail),
        )
    # TF400813 is the service's own "this user is not authorized" code, which is
    # how a project you cannot see reports itself.
    if (
        "403" in low
        or "forbidden" in low
        or "not authorized" in low
        or "tf400813" in low
        or "does not have permission" in low
    ):
        return PollError(
            message="Not permitted to read this project — check your Azure DevOps "
            "access and the PAT's scopes.",
            kind="forbidden",
        )
    if "404" in low or "not found" in low or "does not exist" in low:
        return PollError(message=_detail_or(tail, "Not found."), kind="not_found")
    return PollError(message=_detail_or(tail, "Unknown `az` failure."), kind="unknown")


def _detail_or(tail: str, fallback: str) -> str:
    """The service's own `message`, if the tail carries an error body; otherwise
    the (redacted, truncated) tail itself."""
    detail = _problem_detail(tail)
    text = detail or tail or fallback
    text = _redact(text)
    if len(text) > _MAX_DETAIL:
        text = text[: _MAX_DETAIL - 1] + "…"
    return text


def _problem_detail(text: str) -> str | None:
    """Find and read an embedded error body inside an error tail."""
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


def _invoke_args(
    org: str,
    area: str,
    resource: str,
    *,
    route: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    method: str = "GET",
    body_path: str | None = None,
) -> list[str]:
    """Build the argv for one `az devops invoke` call.

    `--api-version` is unconditional (see the module docstring). Route parameters
    and query parameters are passed as `key=value` pairs, which is the shape the
    extension expects — and the reason a value containing a space would break, so
    nothing here ever passes one.

    A body travels in a file named by `--in-file` rather than on stdin: the
    extension has no stdin mode, and an inline JSON argument would be mangled by
    the same `key=value` splitting the parameters use.
    """
    args = [
        AZ,
        "devops",
        "invoke",
        "--org",
        org_url(org),
        "--area",
        area,
        "--resource",
        resource,
        "--api-version",
        api.API_VERSION,
        "--http-method",
        method,
        "--only-show-errors",
        "--output",
        "json",
    ]
    if route:
        args.append("--route-parameters")
        args += [f"{key}={value}" for key, value in route.items()]
    if params:
        args.append("--query-parameters")
        args += [f"{key}={value}" for key, value in params.items()]
    if body_path is not None:
        args += ["--in-file", body_path]
    return args


def _json(raw: str) -> dict:
    """Parse a response body, turning junk into an AzdoError rather than a
    JSONDecodeError escaping the I/O boundary.

    A `null` body is a success with nothing in it — `az devops invoke` prints
    that for a 204, which is what a cancel returns — so it becomes `{}` rather
    than an error.
    """
    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        raise AzdoError(
            f"`{AZ}` returned a response that is not JSON: {raw[:120]!r}"
        ) from exc
    return payload if isinstance(payload, dict) else {}


def invoke(
    org: str,
    area: str,
    resource: str,
    *,
    route: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """One call to the Azure DevOps REST API through `az devops invoke`."""
    if body is None:
        return _json(
            _run(
                _invoke_args(org, area, resource, route=route, params=params, method=method),
                timeout=timeout,
            )
        )
    # A named temp file, deleted on the way out: the extension reads the body from
    # a path, and leaving request bodies behind in the temp dir is how a `queue`
    # body's branch name outlives the session that sent it.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="azdo-watch-", delete=True
    ) as handle:
        json.dump(body, handle)
        handle.flush()
        return _json(
            _run(
                _invoke_args(
                    org,
                    area,
                    resource,
                    route=route,
                    params=params,
                    method=method,
                    body_path=handle.name,
                ),
                timeout=timeout,
            )
        )


def list_projects(org: str) -> list[Project]:
    """Every project in the org.

    `az devops project list` rather than `invoke`, because the extension's own
    command is the one place its output is already normalized (it converts the
    timestamps) and there is nothing here worth a hand-rolled route.
    """
    raw = _run(
        [
            AZ,
            "devops",
            "project",
            "list",
            "--org",
            org_url(org),
            "--only-show-errors",
            "--output",
            "json",
        ]
    )
    return api.parse_projects(_json(raw), org_name(org))


def resolve_project(projects: list[Project], wanted: str | None) -> Project:
    """Pick the project the user asked for, by id or (case-insensitive) name.

    Raises with an actionable message rather than guessing: an ambiguous name or a
    typo should say what the options were.
    """
    if not projects:
        raise AzdoError(
            "No Azure DevOps projects visible to this account — check "
            "`az devops login` and the PAT's scopes."
        )
    if wanted is None:
        return projects[0]
    for project in projects:
        if project.id == wanted:
            return project
    lowered = wanted.casefold()
    matches = [p for p in projects if p.name.casefold() == lowered]
    if not matches:
        matches = [p for p in projects if lowered in p.name.casefold()]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(p.name for p in projects)
    if not matches:
        raise AzdoError(f"No project matches {wanted!r}. Available: {known}.")
    raise AzdoError(
        f"{wanted!r} matches more than one project "
        f"({', '.join(m.name for m in matches)}) — use the project id."
    )


# --- the poll ----------------------------------------------------------------


def _walk_runs(
    project: Project, *, limit: int, states: tuple[str, ...]
) -> tuple[list[Run], int, bool]:
    """Fetch up to `limit` runs, following continuation tokens.

    Returns `(runs, calls, more)`. Serial by necessity — page two's token is
    inside page one's response — which is why the first page asks for as much as
    the service will give (`api.MAX_TOP`) rather than for a polite slice. `more`
    is true when a token was still on offer when we stopped, whether that was
    because the limit was reached or because the page ceiling was.
    """
    runs: list[Run] = []
    token = ""
    calls = 0
    for _ in range(MAX_RUN_PAGES):
        want = min(limit - len(runs), api.MAX_TOP)
        if want <= 0:
            break
        payload = invoke(
            project.org,
            "build",
            "builds",
            route={"project": project.route},
            params=api.builds_params(top=want, continuation=token, states=states),
        )
        calls += 1
        runs += api.parse_runs(payload, project.org, project.route)
        token = api.continuation_token(payload)
        if not token:
            return runs, calls, False
    return runs, calls, bool(token)


def _walk_pipelines(
    project: Project, *, name_filter: str = ""
) -> tuple[PipelineList, int]:
    """Fetch the pipeline inventory, following continuation tokens.

    The expensive half of a poll — 58 pipelines with their latest builds measured
    ~4.9s and just under a megabyte — which is exactly why the caller caches it
    (`cli._PipelineCache`) and why this is the one list whose truncation is worth
    reporting: a client-side filter over a truncated inventory is a filter over an
    incomplete list, and `PipelineList.truncated` is what tells the app to push
    the filter server-side instead.
    """
    pipelines: list[Pipeline] = []
    token = ""
    calls = 0
    for _ in range(MAX_PIPELINE_PAGES):
        payload = invoke(
            project.org,
            "build",
            "definitions",
            route={"project": project.route},
            params=api.definitions_params(continuation=token, name_filter=name_filter),
        )
        calls += 1
        pipelines += api.parse_pipelines(payload, project.org, project.route)
        token = api.continuation_token(payload)
        if not token:
            break
    return (
        PipelineList(
            pipelines=tuple(sort_pipelines(pipelines)),
            total=len(pipelines),
            truncated=bool(token),
        ),
        calls,
    )


def fetch_snapshot(
    project: Project,
    *,
    limit: int = api.DEFAULT_LIMIT,
    states: tuple[str, ...] = (),
    projects: list[Project] | None = None,
    pipelines: PipelineList | None = None,
    pipeline_filter: str = "",
) -> Snapshot:
    """One poll of a project: its runs and its pipeline inventory, fanned out.

    The calls run concurrently rather than in sequence, because six serial `az`
    invocations measured ~9s against 1.7s parallel — the difference between a tool
    that feels alive and one that feels stuck.

    **In-flight runs are fetched as their own call, always.** The main window is
    ordered by queue time and bounded, so a build that has been running since last
    week — this org had three when the tool was written — is not in it. Asking for
    `statusFilter=inProgress,notStarted` separately is one extra call that
    guarantees the dashboard's central claim: if something is running, it is on
    screen. The two lists are merged by run id, newest first.

    That second call is skipped when `states` is set, because an explicit
    `--state` filter is the user saying which states they want and silently adding
    two back would be ignoring them.

    Passing `pipelines` reuses an already-known inventory and skips fetching it —
    the caller owns that policy; see `cli._PipelineCache`. It is a `PipelineList`
    rather than a bare tuple so a reused list keeps saying it was truncated.
    """
    started = time.monotonic()
    want_in_flight = not states

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        runs_future = pool.submit(
            _walk_runs, project, limit=limit, states=states
        )
        in_flight_future = (
            pool.submit(
                invoke,
                project.org,
                "build",
                "builds",
                route={"project": project.route},
                params=api.builds_params(
                    top=api.MAX_TOP, states=("inProgress", "notStarted", "cancelling")
                ),
            )
            if want_in_flight
            else None
        )
        pipelines_future = (
            pool.submit(_walk_pipelines, project, name_filter=pipeline_filter)
            if pipelines is None
            else None
        )
        runs, run_calls, more = runs_future.result()
        in_flight: list[Run] = []
        in_flight_calls = 0
        if in_flight_future is not None:
            in_flight = api.parse_runs(
                in_flight_future.result(), project.org, project.route
            )
            in_flight_calls = 1
        known = pipelines or PipelineList()
        pipeline_calls = 0
        if pipelines_future is not None:
            known, pipeline_calls = pipelines_future.result()
    elapsed = time.monotonic() - started

    # Keyed rather than appended: the in-flight call and the main window overlap
    # by design, and a repeat would be a double-counted run and — since the UI
    # keys table rows by `run.key` — a DuplicateKey crash on the next render.
    # In-flight goes in first so its fresher copy of a run wins over the window's.
    by_key: dict[str, Run] = {}
    for run in [*in_flight, *runs]:
        by_key.setdefault(run.key, run)

    return Snapshot(
        project=project,
        projects=tuple(projects or [project]),
        runs=tuple(sort_runs(list(by_key.values()))),
        pipelines=known.pipelines,
        calls=run_calls + in_flight_calls + pipeline_calls,
        elapsed=elapsed,
        runs_more=more,
        pipelines_total=known.total or len(known.pipelines),
        pipelines_truncated=known.truncated,
    )


# --- the drill-down ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunTimeline:
    """One run's timeline records, placed in tree order.

    `rows` is what the record pane lists; `records` is the same set flat, for the
    charts and the issue overlay.
    """

    records: tuple[Record, ...]
    rows: tuple[RecordRow, ...]
    calls: int


def fetch_run_timeline(project: Project, run: Run) -> RunTimeline:
    """The timeline of one run, in tree order — the first drill step.

    One call, and the cheapest useful thing in the tool: it carries every stage,
    job and task with its state, its timings, its log id *and* its issues, so the
    "which step failed and what did it say" question is answered without opening a
    single log.
    """
    payload = invoke(
        project.org,
        "build",
        "timeline",
        route={"project": project.route, "buildId": str(run.id)},
    )
    records = api.parse_timeline(payload)
    return RunTimeline(
        records=tuple(records), rows=tuple(order_records(records)), calls=1
    )


def fetch_log(project: Project, run: Run, record: Record) -> RunLog:
    """One record's log — the second drill step, and the end of the loop.

    A record with no log of its own (a stage) returns an empty log rather than
    raising: the pane says "this step has no log", which is the truth, and a stage
    is a perfectly reasonable thing to have the cursor on.

    The body arrives whole (there is no range parameter the CLI can pass), so the
    only defence against a pathological log is to stop *holding* one: past
    `MAX_LOG_CHARS` the content is cut and the RunLog says so, rather than keeping
    tens of megabytes alive for a pane that renders a few thousand lines of it.
    """
    if record.log_id is None:
        return RunLog(content="", log_id=-1, line_count=0)
    payload = invoke(
        project.org,
        "build",
        "logs",
        route={
            "project": project.route,
            "buildId": str(run.id),
            "logId": str(record.log_id),
        },
        params=api.log_params(),
        timeout=LOG_TIMEOUT,
    )
    return _bounded(api.parse_log(payload, record.log_id))


def _bounded(log: RunLog) -> RunLog:
    """A RunLog trimmed to `MAX_LOG_CHARS`, marked when it had to be trimmed.

    The cut lands on a line boundary so the last shown line is not half a line,
    and `line_count` keeps the *server's* count so the pane can report the gap.
    """
    if len(log.content) <= MAX_LOG_CHARS:
        return log
    head = log.content[:MAX_LOG_CHARS]
    cut = head.rfind("\n")
    return dataclasses.replace(
        log, content=head[:cut] if cut > 0 else head, truncated=True
    )


# --- the investigation bundle -----------------------------------------------

# How many log fetches one investigation may spend. Every failed record's log is
# fetched (the failure is the point of the exercise) and every job's, since a
# job's log is the concatenation of its tasks' and so summarizes a whole leg of
# the run cheaply. Past this ceiling the bundle lists what was skipped, so the
# report can say so instead of implying completeness.
MAX_INVESTIGATION_LOGS = 40


@dataclass(frozen=True, slots=True)
class RecordLog:
    """One record's log inside a `RunBundle` — or the concise, already-redacted
    reason it could not be fetched. A failed fetch is a fact worth reporting, not
    grounds to abandon the rest of the bundle."""

    record: Record
    log: RunLog | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunBundle:
    """Everything the `i` hand-off gathers about one run before writing the report
    `gw` is pointed at: the timeline (in tree order, as `rows`), the logs worth
    reading, and the honesty fields — what was skipped and what the gathering
    cost.
    """

    run: Run
    records: tuple[Record, ...]
    rows: tuple[RecordRow, ...]
    logs: tuple[RecordLog, ...]
    skipped: tuple[str, ...]
    calls: int
    elapsed: float


def _worth_reading(row: RecordRow) -> bool:
    """Whether this record's log belongs in an investigation report.

    Failed records first and always — that is what is being investigated. Jobs
    too, because a job's log contains its tasks' output and so covers the parts of
    the run that *worked* in one fetch instead of a dozen. Everything that
    succeeded as an individual task is left out: a report an agent has to read
    should not be four megabytes of `apt-get` output.
    """
    record = row.record
    if not record.has_log:
        return False
    if record.failed or record.issues:
        return True
    return record.type == "Job"


def fetch_run_bundle(
    project: Project,
    run: Run,
    *,
    max_logs: int = MAX_INVESTIGATION_LOGS,
) -> RunBundle:
    """One run's full context — timeline plus the logs worth reading — for the
    report `gw scratch` is handed.

    Log fetches fan out in parallel like everything else here. A single log that
    cannot be fetched becomes a note in the bundle rather than a failure of the
    whole gather — the run may be worth summarizing precisely because one of its
    steps is misbehaving.
    """
    started = time.monotonic()
    timeline = fetch_run_timeline(project, run)
    wanted = [row.record for row in timeline.rows if _worth_reading(row)]
    to_fetch, dropped = wanted[:max_logs], wanted[max_logs:]

    logs: list[RecordLog] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            (record, pool.submit(fetch_log, project, run, record))
            for record in to_fetch
        ]
        for record, future in futures:
            try:
                logs.append(RecordLog(record=record, log=future.result()))
            except (AzdoError, api.AzdoApiError) as exc:
                logs.append(
                    RecordLog(record=record, error=classify_azdo_error(exc).message)
                )
    return RunBundle(
        run=run,
        records=timeline.records,
        rows=timeline.rows,
        logs=tuple(logs),
        skipped=tuple(f"{record.type} {record.name}" for record in dropped),
        calls=timeline.calls + len(to_fetch),
        elapsed=time.monotonic() - started,
    )


# --- mutations -------------------------------------------------------------
#
# Every one of these is reached only through the app's confirmation modal, and
# every one is recorded in the activity log. The request itself — method, body and
# route — is built by `api.mutation_request`; this module only sends it and reads
# the outcome.


def _request_for(action: Action) -> tuple[str, dict, dict]:
    """The `(method, body, params)` for an action, as a typed transport error.

    `api.mutation_request` refuses a request that must not be sent — an unknown
    kind, a missing id, an unnamed stage — with an `AzdoApiError`, because it
    knows nothing about this module's error taxonomy. Converting it here is what
    keeps those refusals on the same path as every other failure the UI shows.
    """
    return api.mutation_request(action)


def perform(project: Project, action: Action) -> str:
    """Run a confirmed action and return the one-line outcome to log.

    The returned line is what the activity log records, which is how a user
    answers "did my cancel actually fire?" — the question that matters more in a
    tool that can change state than in a read-only one.
    """
    method, body, params = _request_for(action)
    if action.kind == "queue":
        payload = invoke(
            project.org,
            "build",
            "builds",
            route={"project": project.route},
            method=method,
            body=body,
            params=params,
        )
    elif action.kind == "cancel":
        payload = invoke(
            project.org,
            "build",
            "builds",
            route={"project": project.route, "buildId": str(action.run_id)},
            method=method,
            body=body,
            params=params,
        )
    else:  # retry_stage
        payload = invoke(
            project.org,
            "build",
            "stages",
            route={
                "project": project.route,
                "buildId": str(action.run_id),
                "stageRefName": action.stage_name,
            },
            method=method,
            body=body,
            params=params,
        )
    return f"{action.summary} — {_outcome(action, payload)}"


def _outcome(action: Action, payload: dict) -> str:
    """Describe what the response says happened.

    A queue answers with the new build, which is the useful thing to report — its
    number is what the user will look for in the list a moment later. Cancel and
    stage-retry answer with an empty body on success, so "requested" is the honest
    word: the orchestrator has been told, and the next poll shows the result.
    """
    if action.kind == "queue":
        queued = api.parse_run(payload)
        if queued is not None:
            return f"queued run {queued.build_number or queued.id}"
        return "requested"
    return "requested — the next refresh will show it"
