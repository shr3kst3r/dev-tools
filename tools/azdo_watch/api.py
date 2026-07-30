"""The Azure DevOps wire layer: routes, query parameters, and response parsing.

Pure functions only — nothing here spawns a process or touches the network, which
is what makes every parser directly unit-testable against a captured payload.
`azdo.py` owns the I/O; this module owns "what does the REST API call this, and
what shape does it come back in".

Two properties of the API shape the whole design and are worth stating here
rather than discovering twice:

**Paging is by opaque continuation token, not by offset.** The build list returns
`x-ms-continuationtoken` (surfaced by `az devops invoke` as a `continuation_token`
key in the JSON it prints) and *no total count* — unlike Airflow, which reports
`total_entries` and lets a client compute every offset up front and fetch them in
parallel. So a deeper window here is a **serial** walk: page two's token only
exists once page one has come back. The consequence is a design constraint rather
than an inconvenience — one big `$top` beats several small pages, so the default
window is a single large call (`$top=1000` measured 2.9s against 1.6s for 200),
and "is there more?" is answered by the presence of a token instead of by
arithmetic. It also means the honest phrasing is `N loaded · more available`, not
`N of M`: M does not exist.

**Nothing is nullable in the way you expect.** A build has both `status` and
`result` and neither answers "how is it doing" alone; `definition.name` is
present on every build but `repository.id` is null on a plain definitions list;
`startTime` is absent for a queued run and `finishTime` for a running one. Every
parser here is written to survive a missing key rather than to assert a schema —
see `_text`, `_int` and `_stamp`, which are the only three ways a scalar is ever
read off the wire.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    Action,
    Issue,
    Pipeline,
    Project,
    Record,
    Run,
    RunLog,
    clean_log_line,
)

__all__ = [
    "API_VERSION",
    "DEFAULT_LIMIT",
    "MAX_TOP",
    "build_url",
    "builds_params",
    "cancel_request",
    "continuation_token",
    "definitions_params",
    "log_params",
    "parse_error_detail",
    "parse_log",
    "parse_pipelines",
    "parse_projects",
    "parse_run",
    "parse_runs",
    "parse_timeline",
    "pipeline_url",
    "queue_request",
    "retry_stage_request",
]

# The REST API version every call pins. Pinned rather than left to the CLI's
# default because the timeline and stage-update resources have moved between
# previews, and a monitoring tool that silently follows whatever the installed
# `az` extension prefers is a tool that breaks on an extension upgrade. 7.1 is
# the current GA version and carries everything used here.
API_VERSION = "7.1"

# How many runs one poll fetches by default. A single call, deliberately: paging
# is serial here (see the module docstring), so one large `$top` costs 2.9s where
# five pages of 200 would cost ~8s for the same rows.
DEFAULT_LIMIT = 1000

# The largest `$top` we will ask for in one call. The service accepts 1000
# without complaint and without truncating; beyond that it is undocumented, and a
# request that is quietly capped is worse than one that pages.
MAX_TOP = 1000


class AzdoApiError(ValueError):
    """A response that cannot be read as the shape it should be.

    A `ValueError` because it is a parse failure, not a transport failure —
    `azdo.py` converts it at the boundary like every other refusal.
    """


# --- scalars ---------------------------------------------------------------
#
# The only three ways a value is read off the wire. Everything else composes
# these, so "the API returned null where the docs say string" is handled once.


def _text(payload: dict, *path: str) -> str:
    """A string at `path`, or "" for anything missing, null, or non-scalar."""
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value)


def _int(payload: dict, *path: str, default: int = 0) -> int:
    """An int at `path`, or `default`. Tolerates the string form, which the API
    uses for `triggerInfo` values and for `data.logFileLineNumber`."""
    raw = _text(payload, *path)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _stamp(payload: dict, *path: str) -> datetime | None:
    """A UTC datetime at `path`, or None.

    Azure DevOps stamps times as ISO 8601 with a `Z` suffix and up to seven
    fractional digits — one more than `datetime` accepts — so the fraction is
    truncated to six rather than rejected. A naive result is *assumed UTC*, which
    is what the API documents and what `az devops project list` returns
    (`+00:00`) when it normalizes for us.
    """
    raw = _text(payload, *path)
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        offset = tail[len(digits) :] if len(tail) > len(digits) else ""
        # Whatever followed the digits is the timezone (`+00:00`), which must be
        # kept: dropping it would turn a UTC stamp into a local-time one.
        suffix = offset.lstrip("0123456789")
        text = f"{head}.{digits}{suffix}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _records(payload: dict, key: str = "value") -> list[dict]:
    """The list of records in a collection response.

    `az devops invoke` prints the service's envelope verbatim, so a collection is
    `{"count": n, "value": [...]}`. A bare list is accepted too, because some
    resources (the timeline's `records`, a log's `value`) are addressed directly
    and a caller may have already unwrapped one.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def continuation_token(payload: dict) -> str:
    """The paging token a collection response carried, or "".

    `az devops invoke` lifts the `x-ms-continuationtoken` response header into the
    JSON body as `continuation_token`; the service itself also echoes it as
    `continuationToken` on some resources. Both spellings are read, because which
    one appears depends on the resource rather than on anything we control.
    """
    for key in ("continuation_token", "continuationToken"):
        token = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(token, str) and token:
            return token
    return ""


# --- routes and parameters ---------------------------------------------------
#
# `az devops invoke` addresses a resource by (area, resource, route parameters)
# rather than by URL, so these builders return the *query* parameters only; the
# area/resource pair and the route live in `azdo.py` next to the call itself.


def builds_params(
    *,
    top: int = DEFAULT_LIMIT,
    continuation: str = "",
    states: Iterable[str] = (),
    definition_ids: Iterable[int] = (),
    branch: str = "",
) -> dict[str, str]:
    """Query parameters for the build list.

    `queryOrder=queueTimeDescending` is not the default and matters: the default
    orders by finish time, which puts every *running* build — the ones with no
    finish time at all — in an order the service does not document. Queue time is
    the one stamp every build has from the moment it exists, so ordering by it is
    what makes "newest first" true for a queued run as well as a finished one.

    `states` are azdo `statusFilter` values (`inProgress`, `notStarted`,
    `completed`, `cancelling`, `postponed`, `none`), comma-joined as the API
    expects. Deliberately not validated against a closed set: a service that
    invents a status should narrow the list, not raise.
    """
    params: dict[str, str] = {
        "$top": str(max(1, min(top, MAX_TOP))),
        "queryOrder": "queueTimeDescending",
    }
    wanted = [state for state in states if state]
    if wanted:
        params["statusFilter"] = ",".join(wanted)
    ids = [str(identifier) for identifier in definition_ids if identifier]
    if ids:
        params["definitions"] = ",".join(ids)
    if branch:
        params["branchName"] = branch
    if continuation:
        params["continuationToken"] = continuation
    return params


def definitions_params(
    *, top: int = MAX_TOP, name_filter: str = "", continuation: str = ""
) -> dict[str, str]:
    """Query parameters for the pipeline inventory.

    `includeLatestBuilds=true` is the whole reason this call is worth its 4.9s: it
    folds each pipeline's most recent run into the same response, which is what
    lets the Pipelines view answer "and did it pass?" for a pipeline whose last
    run is a year old and therefore nowhere near the loaded runs window. Fetching
    it per pipeline instead would be one call each.

    `name_filter` is the server-side name filter, used only when the inventory
    came back truncated — below that the whole list is loaded and filtering it
    client-side is instant and free.
    """
    params: dict[str, str] = {
        "$top": str(max(1, min(top, MAX_TOP))),
        "includeLatestBuilds": "true",
        "queryOrder": "lastModifiedDescending",
    }
    if name_filter:
        # The service matches this as a prefix unless it is wildcarded, which is
        # not what a user typing into a filter box means.
        params["name"] = f"*{name_filter}*"
    if continuation:
        params["continuationToken"] = continuation
    return params


def log_params() -> dict[str, str]:
    """Query parameters for a record's log.

    `$format=json` returns the log as a JSON array of lines. The alternative is
    `text/plain`, which `az devops invoke` cannot hand back — it parses every
    response as JSON — so this is not a preference.
    """
    return {"$format": "json"}


def build_url(org: str, project: str, build_id: int) -> str:
    """The web page for one run. Computed rather than only read off `_links`
    because a run reconstructed from a partial payload still needs to be
    openable, and this URL shape has been stable for the life of the service."""
    return f"https://dev.azure.com/{org}/{project}/_build/results?buildId={build_id}"


def pipeline_url(org: str, project: str, definition_id: int) -> str:
    """The web page for one pipeline's run history."""
    return f"https://dev.azure.com/{org}/{project}/_build?definitionId={definition_id}"


# --- parsing ---------------------------------------------------------------


def parse_projects(payload: dict, org: str = "") -> list[Project]:
    """Every project in the org, name-ordered.

    Ordered here rather than server-side because the projects call has no
    `queryOrder`, and a switcher whose entries move between polls is a switcher
    where "press 2" means something different each time you open it.
    """
    projects = [
        Project(
            id=_text(item, "id"),
            name=_text(item, "name"),
            org=org,
            description=_text(item, "description"),
            state=_text(item, "state"),
            visibility=_text(item, "visibility"),
        )
        for item in _records(payload)
    ]
    return sorted(projects, key=lambda project: project.name.casefold())


def parse_run(payload: dict, org: str = "", project: str = "") -> Run | None:
    """One build. None when the payload carries no build at all.

    Returns None rather than raising because the callers that reach here with an
    empty dict — a queue response the service answered without a body, a
    definition with no `latestBuild` — are all asking "is there one?", and an
    exception is the wrong answer to that question.
    """
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    build_id = _int(payload, "id")
    trigger = payload.get("triggerInfo")
    trigger = trigger if isinstance(trigger, dict) else {}
    tags = payload.get("tags")
    web = _text(payload, "_links", "web", "href")
    return Run(
        id=build_id,
        build_number=_text(payload, "buildNumber"),
        pipeline_id=_int(payload, "definition", "id"),
        pipeline_name=_text(payload, "definition", "name"),
        status=_text(payload, "status"),
        result=_text(payload, "result"),
        reason=_text(payload, "reason"),
        branch=_text(payload, "sourceBranch"),
        commit=_text(payload, "sourceVersion"),
        # `requestedFor` is who the run is *for* (the commit's author, or the
        # GitHub app for a PR build); `requestedBy` is whatever service actually
        # queued it and is almost always "Microsoft.VisualStudio.Services.TFS",
        # which tells a reader nothing.
        requested_for=_text(payload, "requestedFor", "displayName"),
        queue_time=_stamp(payload, "queueTime"),
        start_time=_stamp(payload, "startTime"),
        finish_time=_stamp(payload, "finishTime"),
        queue_name=_text(payload, "queue", "name"),
        tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
        pr_number=_text(trigger, "pr.number"),
        pr_title=_text(trigger, "pr.title"),
        web_url=web or build_url(org, project, build_id),
    )


def parse_runs(payload: dict, org: str = "", project: str = "") -> list[Run]:
    """Every build in a list response, in the order the service returned them."""
    parsed = [parse_run(item, org, project) for item in _records(payload)]
    return [run for run in parsed if run is not None]


def parse_pipelines(payload: dict, org: str = "", project: str = "") -> list[Pipeline]:
    """Every pipeline in a definitions response, with its latest run folded in.

    `latestBuild` is preferred over `latestCompletedBuild` because a run in
    flight is exactly what the dashboard exists to show; the completed one is
    what the azdo UI falls back to, and falling back to it here would hide the
    running build behind the last finished one.
    """
    pipelines: list[Pipeline] = []
    for item in _records(payload):
        definition_id = _int(item, "id")
        latest = item.get("latestBuild")
        latest = latest if isinstance(latest, dict) else {}
        if not latest:
            completed = item.get("latestCompletedBuild")
            latest = completed if isinstance(completed, dict) else {}
        pipelines.append(
            Pipeline(
                id=definition_id,
                name=_text(item, "name"),
                path=_text(item, "path") or "\\",
                queue_status=_text(item, "queueStatus") or "enabled",
                type=_text(item, "type") or "build",
                revision=_int(item, "revision"),
                authored_by=_text(item, "authoredBy", "displayName"),
                last_run=parse_run(latest, org, project),
                web_url=_text(item, "_links", "web", "href")
                or pipeline_url(org, project, definition_id),
            )
        )
    return pipelines


def parse_issues(payload: dict) -> tuple[Issue, ...]:
    """The issues one timeline record reported.

    `data.logFileLineNumber` is the field that makes an issue navigable, and it
    arrives as a *string* — hence `_int` rather than a cast. A missing or
    unparseable line number becomes None, which the log pane reads as "this issue
    knows what went wrong but not where".
    """
    raw = payload.get("issues")
    if not isinstance(raw, list):
        return ()
    issues: list[Issue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        line = _int(item, "data", "logFileLineNumber", default=0)
        issues.append(
            Issue(
                type=_text(item, "type") or "error",
                category=_text(item, "category"),
                message=" ".join(_text(item, "message").split()),
                log_line=line or None,
            )
        )
    return tuple(issues)


def parse_timeline(payload: dict) -> list[Record]:
    """One run's timeline records, unordered — `models.order_records` places them.

    Deliberately keeps *every* record type, including the `Checkpoint` records
    azdo emits for approvals and gates: a run stuck waiting for an approval looks
    identical to a run stuck on a job unless the checkpoint is on screen.

    Records from superseded attempts are kept too. Azure DevOps leaves the earlier
    attempt's records in the timeline with their own ids when a job is re-run, and
    each carries its own `attempt`, so the tree shows the retry alongside what it
    replaced — which is the history you want when asking why a flaky job passed
    the second time.
    """
    records: list[Record] = []
    for item in _records(payload, "records"):
        identifier = _text(item, "id")
        if not identifier:
            continue  # nothing can be keyed, ordered or fetched without one
        issues = parse_issues(item)
        records.append(
            Record(
                id=identifier,
                name=_text(item, "name"),
                # `identifier` and `refName` are the same value under two spellings,
                # and which one a resource returns has moved between API versions.
                ref_name=_text(item, "identifier") or _text(item, "refName"),
                type=_text(item, "type") or "Task",
                state=_text(item, "state"),
                result=_text(item, "result"),
                parent_id=_text(item, "parentId") or None,
                order=_int(item, "order"),
                start_time=_stamp(item, "startTime"),
                finish_time=_stamp(item, "finishTime"),
                # `log.id` is absent for a record that produced no log of its own
                # (a stage, or a task that never ran). 0 is a *valid* log id — the
                # build's own container log — so absence has to be None, not 0.
                log_id=_int(item, "log", "id", default=-1)
                if isinstance(item.get("log"), dict)
                else -1,
                attempt=_int(item, "attempt", default=1),
                worker_name=_text(item, "workerName"),
                percent_complete=_int(item, "percentComplete", default=-1),
                issues=issues,
                error_count=_int(item, "errorCount"),
                warning_count=_int(item, "warningCount"),
            )
        )
    return [_normalize_record(record) for record in records]


def _normalize_record(record: Record) -> Record:
    """Turn the sentinel values `parse_timeline` uses for "absent" into None.

    Done as a second pass rather than inline because `-1` has to survive the read
    (0 is a real log id and a real percentage) but must never reach the UI, which
    would render it as a log to open or a progress bar at minus one percent.
    """
    return dataclasses.replace(
        record,
        log_id=None if record.log_id is not None and record.log_id < 0 else record.log_id,
        percent_complete=(
            None
            if record.percent_complete is not None and record.percent_complete < 0
            else record.percent_complete
        ),
    )


def parse_log(payload: dict, log_id: int) -> RunLog:
    """One record's log, from the `$format=json` array of lines.

    Each line is cleaned as it is read (`models.clean_log_line`): the agent's ISO
    timestamp prefix and any ANSI colour codes come off here, once, rather than in
    every consumer. The `/` filter, the issue line numbers and the gw report all
    then agree on what line N says — which they would not if one of them were
    matching against escape bytes.

    Line numbers are preserved by construction: cleaning is per line and never
    joins or drops one, so line N of the pane is line N of the log azdo has, and
    an issue's `logFileLineNumber` points where it says it does.
    """
    lines: Any = payload.get("value") if isinstance(payload, dict) else payload
    if isinstance(lines, str):
        # A log fetched without `$format=json` comes back as one blob; accepted so
        # a caller that got plain text is not left with nothing.
        lines = lines.splitlines()
    if not isinstance(lines, list):
        return RunLog(content="", log_id=log_id, line_count=0)
    cleaned = [clean_log_line(str(line)) for line in lines]
    return RunLog(content="\n".join(cleaned), log_id=log_id, line_count=len(cleaned))


def parse_error_detail(payload: object) -> str | None:
    """The service's own explanation, out of an error body.

    Azure DevOps answers a failure with `{"message": …, "typeKey": …}` — its
    `message` is a complete, quotable sentence, which is exactly what the UI
    should show instead of a status code. `$id`/`innerException` shapes appear on
    older resources and are read too.

    Typed `object` rather than `dict` on purpose: the only caller feeds it
    whatever `json.loads` found inside an error string, which is by definition
    unvalidated — so the isinstance check is load-bearing, not decorative.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("message", "Message", "value"):
        message = payload.get(key)
        if isinstance(message, str) and message.strip():
            return " ".join(message.split())
    inner = payload.get("innerException")
    if isinstance(inner, dict):
        return parse_error_detail(inner)
    return None


# --- mutations -------------------------------------------------------------
#
# Each returns `(method, body, params)` for one confirmed action. Pure, so the
# request a confirmation modal describes is exactly the request the test asserts
# on — the transport only sends it.
#
# All three are wire shapes that have been stable across API versions. None of
# them has a dry run: Azure DevOps offers no "tell me what this would do" for
# queueing, cancelling or retrying, so the confirmation modal is the only gate
# there is, and it says so.


def queue_request(pipeline_id: int, branch: str = "") -> tuple[str, dict, dict]:
    """Queue a new run of a pipeline.

    An empty `branch` lets the service use the pipeline's own default, which is
    the right behaviour for "run this pipeline" — guessing `refs/heads/main`
    would be wrong for every repo that uses `master` or `develop`, and this org
    uses all three.
    """
    if pipeline_id <= 0:
        raise AzdoApiError("A pipeline id is required to queue a run.")
    body: dict[str, object] = {"definition": {"id": pipeline_id}}
    if branch:
        body["sourceBranch"] = branch
    return "POST", body, {}


def cancel_request(run_id: int) -> tuple[str, dict, dict]:
    """Cancel a run in flight.

    `cancelling` rather than `cancelled`: the caller asks the orchestrator to
    stop, and the run passes through `cancelling` on its way to
    `completed`/`canceled` on its own. Asking for the terminal state directly is
    rejected by the service.
    """
    if run_id <= 0:
        raise AzdoApiError("A run id is required to cancel a run.")
    return "PATCH", {"status": "cancelling"}, {}


def retry_stage_request(run_id: int, stage_name: str) -> tuple[str, dict, dict]:
    """Re-run one stage of a finished run — azdo's "rerun failed jobs".

    Scoped to a named stage because that is the only shape the API offers, and a
    good thing too: re-running the one stage that failed is what you want, and
    re-running the whole pipeline is `queue`.
    """
    if run_id <= 0:
        raise AzdoApiError("A run id is required to retry a stage.")
    if not stage_name.strip():
        raise AzdoApiError("A stage name is required to retry a stage.")
    return "PATCH", {"state": "retry", "forceRetryAllJobs": False}, {}


def mutation_request(action: Action) -> tuple[str, dict, dict]:
    """The `(method, body, params)` for one confirmed action.

    One entry point so the transport never contains a chain of `if kind ==`, and
    so an unknown kind is refused *before* anything is sent rather than falling
    through to a default that does something.
    """
    if action.kind == "queue":
        return queue_request(action.pipeline_id, action.branch)
    if action.kind == "cancel":
        return cancel_request(action.run_id)
    if action.kind == "retry_stage":
        return retry_stage_request(action.run_id, action.stage_name)
    raise AzdoApiError(f"Unknown action {action.kind!r}.")
