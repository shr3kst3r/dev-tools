"""Plain data models for the azdo-watch dashboard.

Kept free of any I/O so they are trivial to construct in tests, and free of any
wire-shape knowledge so the parsing stays in one place (`api.py`): these are the
*normalized* shapes the UI renders.

Two things are worth stating up front, because both are load-bearing:

**States are plain strings, never enums.** Azure DevOps has invented states
before (`postponed`, `succeededWithIssues`) and will again, so nothing here
validates one; `ui.state_style` buckets an unrecognized value into a neutral
fallback instead. Copied deliberately from airflow-watch, where the same rule
kept an Airflow 3 release from taking the dashboard down.

**Azure DevOps splits "what state is this in?" across two fields** — `status`
(is it running?) and `result` (how did it end?) — and neither answers the
question alone: a build with `status: completed` says nothing about success, and
one with `result: null` may be running or may never have started. `run_state`
and `record_state` fold the pair into the single string every list, chart and
filter keys off. That fold is the one place the two-field wire shape is
flattened, so it is a pure function with its own tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# The run states this build has heard of, after the `status`/`result` fold.
# Not a closed set and never used for validation: anything absent still renders,
# in `ui.state_style`'s fallback bucket. This list is the `--state` flag's help
# text, and the test suite asserts `ui.state_style` has a colour for every entry,
# so a state added here without a colour is a failing test rather than a
# question-marked row.
KNOWN_RUN_STATES = (
    "succeeded",
    "partiallySucceeded",
    "failed",
    "canceled",
    "running",
    "queued",
    "cancelling",
    "completing",
    "postponed",
    "none",
)

# The timeline-record states, same fold, same rules. `succeededWithIssues` is
# azdo's "green but it logged warnings" — a distinct outcome worth its own
# colour, since it is the one green state you sometimes need to look at.
KNOWN_RECORD_STATES = (
    "succeeded",
    "succeededWithIssues",
    "failed",
    "canceled",
    "skipped",
    "abandoned",
    "running",
    "pending",
    "none",
)

# The states worth looking at first: a run in one of these is why you opened the
# tool. Drives the summary bar's counts and the attention dot.
ATTENTION_RUN_STATES = ("failed", "partiallySucceeded", "running", "queued")

# Run states that mean "this one is still going" — in flight, so its duration is
# live and cancelling it is meaningful.
IN_FLIGHT_RUN_STATES = ("running", "queued", "cancelling", "completing", "postponed")

# Record states that mean "this is the thing that broke".
FAILED_RECORD_STATES = ("failed", "abandoned")

# What Azure DevOps glues a run's commit subject onto its `buildNumber` with, when
# the pipeline has `appendCommitMessageToRunName` set. Splitting on it is what keeps
# `Run.number` short — see `Run.number` and `Run.description`.
_RUN_NAME_SPLIT = " • "

# The timeline record types, outermost first. Azure DevOps nests
# Stage → Phase → Job → Task, with `Checkpoint` records hanging off a stage for
# approvals and gates. Used for labelling only — the tree itself is built from
# `parent_id`, never from this order, because a pipeline may skip a level.
RECORD_TYPES = ("Stage", "Phase", "Job", "Task", "Checkpoint")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def run_state(status: str, result: str) -> str:
    """One state string for a run, from azdo's `status` + `result` pair.

    A completed run *is* its result — that is the only reading of `completed`
    that says anything. Anything still in flight is its status, mapped to the
    vocabulary the rest of the tool shares with airflow-watch (`running`,
    `queued`) where the two agree, and left as azdo's own word where they do not
    (`cancelling`, `completing`, `postponed`).

    Total by construction: an unrecognized status comes back unchanged rather
    than being forced into a bucket it does not belong in, and an empty pair
    becomes "none". A monitoring tool that renamed a state it did not recognize
    would be lying about production.
    """
    stage = (status or "").strip()
    outcome = (result or "").strip()
    if stage == "completed":
        return outcome or "none"
    if stage == "inProgress":
        return "running"
    if stage == "notStarted":
        return "queued"
    return stage or outcome or "none"


def record_state(state: str, result: str) -> str:
    """One state string for a timeline record, from its `state` + `result`.

    The same fold as `run_state`, against the record vocabulary: a record's
    in-flight states are `pending` and `inProgress`, and its results add
    `succeededWithIssues`, `skipped` and `abandoned` to the run ones.
    """
    stage = (state or "").strip()
    outcome = (result or "").strip()
    if stage == "completed":
        return outcome or "none"
    if stage == "inProgress":
        return "running"
    return stage or outcome or "none"


@dataclass(frozen=True, slots=True)
class Project:
    """One Azure DevOps project inside the org being watched.

    The switchable unit, and so the analogue of airflow-watch's deployment: an
    org is named once on the command line (or taken from `az devops configure`),
    while which project you are looking at is a decision you change while the
    tool is open.
    """

    id: str
    name: str
    org: str = ""
    description: str = ""
    state: str = ""
    visibility: str = ""

    @property
    def label(self) -> str:
        return f"{self.org} / {self.name}" if self.org else self.name

    @property
    def key(self) -> str:
        """Stable identity for selection and layout persistence.

        The GUID, not the name: a project can be renamed, and the REST API takes
        either — so the id is both stabler and no harder to address.
        """
        return self.id or self.name

    @property
    def route(self) -> str:
        """What to put in a REST route's `project` parameter.

        The GUID is preferred for the same reason it is the key, and is what the
        Azure DevOps web UI itself puts in a build-results URL.
        """
        return self.id or self.name

    @property
    def web_url(self) -> str:
        return f"https://dev.azure.com/{self.org}/{self.route}" if self.org else ""


@dataclass(frozen=True, slots=True)
class Issue:
    """One error or warning a timeline record reported.

    `log_line` is azdo's `data.logFileLineNumber`, which is what makes an issue
    *navigable* rather than merely alarming: it says where in that record's log
    the failure was printed. Carried as an int so the log pane can jump to it.
    """

    type: str = "error"
    category: str = ""
    message: str = ""
    log_line: int | None = None

    @property
    def is_error(self) -> bool:
        return self.type.strip().lower() == "error"


@dataclass(frozen=True, slots=True)
class Run:
    """One pipeline run (a *build*, in the REST API's older vocabulary).

    `pipeline_name` is denormalized onto the run on purpose: every build the API
    returns carries its own `definition`, so the runs list never has to join
    against the pipeline inventory to render a row — which is what lets the runs
    list stay live while the (much more expensive) inventory is served from cache.
    """

    id: int
    build_number: str = ""
    pipeline_id: int = 0
    pipeline_name: str = ""
    status: str = ""
    result: str = ""
    reason: str = ""
    branch: str = ""
    commit: str = ""
    requested_for: str = ""
    queue_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    queue_name: str = ""
    tags: tuple[str, ...] = ()
    # Set when the run was triggered by a pull request. Both come from
    # `triggerInfo`, which is the only place the PR's own number and title
    # appear — the branch is `refs/pull/<n>/merge`, which names the PR but says
    # nothing about what it is.
    pr_number: str = ""
    pr_title: str = ""
    web_url: str = ""

    @property
    def key(self) -> str:
        return str(self.id)

    @property
    def state(self) -> str:
        return run_state(self.status, self.result)

    @property
    def duration(self) -> float | None:
        """Seconds the run took, or None while it is still going.

        A run that started but has not finished has no duration yet — the UI
        computes live elapsed time from `start_time` and its own clock, so this
        stays a pure function of the record.
        """
        if self.start_time is None or self.finish_time is None:
            return None
        return max(0.0, (self.finish_time - self.start_time).total_seconds())

    @property
    def queued_for(self) -> float | None:
        """How long the run waited before an agent picked it up.

        Worth its own field because a pipeline that is *slow* and one that is
        *starved of agents* look identical from a duration column, and the fix
        for each is nothing like the fix for the other.
        """
        if self.queue_time is None or self.start_time is None:
            return None
        return max(0.0, (self.start_time - self.queue_time).total_seconds())

    @property
    def in_flight(self) -> bool:
        return self.state in IN_FLIGHT_RUN_STATES

    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION_RUN_STATES

    @property
    def branch_label(self) -> str:
        """The branch as a human refers to it: `main`, not `refs/heads/main`.

        A PR build's branch is `refs/pull/<n>/merge`, which is the merge commit
        rather than anything anyone typed; those render as `PR <n>` instead, so
        the column answers "what is this building?" in both cases.
        """
        ref = self.branch
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/") :]
        if ref.startswith("refs/pull/"):
            parts = ref.split("/")
            number = self.pr_number or (parts[2] if len(parts) > 2 else "")
            return f"PR {number}" if number else ref
        if ref.startswith("refs/tags/"):
            return f"tag {ref[len('refs/tags/') :]}"
        return ref

    @property
    def number(self) -> str:
        """The run's number as azdo assigns it — `20260730.5`.

        Split off the description, because `buildNumber` is not always only the
        number: a pipeline with `appendCommitMessageToRunName` set has its commit
        subject glued onto the end after a bullet, so the field that identifies a
        run can be a hundred characters of commit message. This is the short,
        sortable half.
        """
        if not self.build_number:
            return str(self.id)
        return self.build_number.split(_RUN_NAME_SPLIT, 1)[0].strip()

    @property
    def description(self) -> str:
        """What this run is *about*, beyond its number — or "" when nothing adds to it.

        Azure DevOps has no single field for it, and where the answer lives depends
        on how the run was triggered: a pipeline with `appendCommitMessageToRunName`
        puts the commit subject into `buildNumber` after a bullet, and a PR build
        knows the PR's title. The branch is deliberately *not* a fallback — it has
        its own column, and repeating it there would spend the width a real
        description needs on something already on screen.
        """
        if _RUN_NAME_SPLIT in self.build_number:
            return self.build_number.split(_RUN_NAME_SPLIT, 1)[1].strip()
        return self.pr_title

    @property
    def trigger(self) -> str:
        """Why this run happened, in the words the azdo UI uses."""
        return {
            "manual": "manually run",
            "individualCI": "CI",
            "batchedCI": "batched CI",
            "schedule": "scheduled",
            "pullRequest": "PR",
            "buildCompletion": "triggered by a build",
            "resourceTrigger": "resource trigger",
            "checkInShelveset": "gated check-in",
            "validateShelveset": "shelveset validation",
        }.get(self.reason, self.reason or "unknown")

    @property
    def short_commit(self) -> str:
        return self.commit[:8]

    @property
    def happened_at(self) -> datetime | None:
        """When the run happened: started if it did, else when it was queued.

        A queued run has no start time and must still have a place in time —
        otherwise the newest thing on the dashboard, a run waiting for an agent,
        would sort to the bottom.
        """
        return self.start_time or self.queue_time

    @property
    def sort_date(self) -> datetime:
        """Newest-first ordering key, with an undatable run pinned to the epoch
        — the bottom of a newest-first list."""
        return self.happened_at or _EPOCH

    @property
    def search_text(self) -> str:
        """What the `/` filter matches a run row against."""
        return " ".join(
            (
                self.pipeline_name,
                self.build_number,
                self.state,
                self.reason,
                self.branch_label,
                self.branch,
                self.requested_for,
                self.pr_number,
                self.pr_title,
                self.short_commit,
                *self.tags,
            )
        )


@dataclass(frozen=True, slots=True)
class Pipeline:
    """One pipeline (a build *definition*), as the Pipelines view shows it.

    `last_run` is the definition's own `latestBuild`, which is what makes the
    Pipelines view answer "when did each of these last run, and did it pass?"
    for a pipeline whose last run is a year old and therefore nowhere near the
    loaded runs window. The app overlays a fresher run from that window when it
    has one — see `Snapshot.latest_run_for`.

    `queue_status` is azdo's `enabled` / `paused` / `disabled`. A paused or
    disabled pipeline is *labelled*, never hidden, for the same reason a paused
    DAG is in airflow-watch: a row silently absent is the failure mode a
    monitoring tool must not have.
    """

    id: int
    name: str
    path: str = "\\"
    queue_status: str = "enabled"
    type: str = "build"
    revision: int = 0
    authored_by: str = ""
    last_run: Run | None = None
    web_url: str = ""

    @property
    def key(self) -> str:
        return str(self.id)

    @property
    def folder(self) -> str:
        """The pipeline's folder, as the azdo UI shows it under the name — empty
        for one at the root (whose `path` is a lone backslash)."""
        return self.path.strip("\\").replace("\\", " / ")

    @property
    def is_paused(self) -> bool:
        return self.queue_status.strip().lower() == "paused"

    @property
    def is_disabled(self) -> bool:
        return self.queue_status.strip().lower() == "disabled"

    @property
    def is_runnable(self) -> bool:
        return not (self.is_paused or self.is_disabled)

    @property
    def needs_attention(self) -> bool:
        run = self.last_run
        return run is not None and run.state in ("failed", "partiallySucceeded")

    @property
    def search_text(self) -> str:
        """What the `/` filter matches a pipeline row against."""
        run = self.last_run
        return " ".join(
            (
                self.name,
                self.folder,
                self.queue_status,
                run.search_text if run is not None else "",
            )
        )


@dataclass(frozen=True, slots=True)
class Record:
    """One timeline record inside a run: a stage, phase, job, task or checkpoint.

    The direct analogue of airflow-watch's task instance, and a closer one than
    it looks: azdo's timeline is an explicit tree (`parent_id` plus a sibling
    `order`), so the dependency ordering airflow-watch has to *derive* from a
    DAG's edges is simply read off the wire here.

    `log_id` is the log this record owns — a task's own output, or for a job the
    whole job's log. A record with none (a stage, usually) has nothing to open.
    """

    id: str
    name: str = ""
    # The record's YAML identifier — `planTerraform` where `name` is
    # "Terraform - Plan". Two different strings for the same step, and the
    # difference matters: the stage-update route takes the *refName*, so retrying a
    # stage by its display name is a 404. Only stages reliably carry one.
    ref_name: str = ""
    type: str = "Task"
    state: str = ""
    result: str = ""
    parent_id: str | None = None
    order: int = 0
    start_time: datetime | None = None
    finish_time: datetime | None = None
    log_id: int | None = None
    attempt: int = 1
    worker_name: str = ""
    percent_complete: int | None = None
    issues: tuple[Issue, ...] = ()
    error_count: int = 0
    warning_count: int = 0

    @property
    def key(self) -> str:
        return self.id

    @property
    def display_state(self) -> str:
        return record_state(self.state, self.result)

    @property
    def duration(self) -> float | None:
        if self.start_time is None or self.finish_time is None:
            return None
        return max(0.0, (self.finish_time - self.start_time).total_seconds())

    @property
    def failed(self) -> bool:
        return self.display_state in FAILED_RECORD_STATES

    @property
    def has_log(self) -> bool:
        return self.log_id is not None

    @property
    def route_name(self) -> str:
        """How to address this record in a REST route — its refName.

        Falls back to the display name only because a route with an empty segment is
        worse than one with a wrong-but-visible value: the request then fails with
        something a user can read, instead of hitting a different resource.
        """
        return self.ref_name or self.name

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.is_error)

    @property
    def search_text(self) -> str:
        """What the `/` filter matches a record row against — including its
        issue messages, so `/timeout` finds the step that timed out."""
        return " ".join(
            (
                self.name,
                self.type,
                self.display_state,
                self.worker_name,
                *(issue.message for issue in self.issues),
            )
        )


@dataclass(frozen=True, slots=True)
class RunLog:
    """One record's log, as fetched for the log pane.

    `truncated` says the fetch stopped short of the whole body
    (`azdo.MAX_LOG_CHARS`), so the pane can say so instead of implying the log
    ends where our buffer did.

    `line_count` is the number of lines the *server* said the log has, which is
    not always the number we hold: a truncated fetch holds fewer. Kept so the
    pane can report the gap rather than pretending it is showing everything.
    """

    content: str
    log_id: int
    line_count: int = 0
    truncated: bool = False

    @property
    def lines(self) -> list[str]:
        return self.content.splitlines()


@dataclass(frozen=True, slots=True)
class PipelineList:
    """A pipeline inventory together with what the server said about it.

    The three travel as one value because they are only true together: 58 of
    1,200 pipelines is honest, the same list without `total` and `truncated` is
    not. Passing the pipelines alone between polls — which is what a cache does
    — is how a truncation quietly stops being reported, so the cache holds this.
    """

    pipelines: tuple[Pipeline, ...] = ()
    total: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything one poll of a project produced.

    `calls` and `elapsed` are observability, not data: the activity log reports
    how many `az` invocations a refresh cost and how long they took, which is
    the only way to tell a slow org from a stuck tool.

    `runs_more` is Azure DevOps' answer to "is there more?": its build list pages
    by an opaque continuation token rather than by offset off a total, so there
    is no count of every build that ever ran — only "the server handed back a
    token, so more exist". That is enough to say `N loaded, more available`, and
    it is all the API offers; see the truncation note in `api.py`.
    """

    project: Project
    projects: tuple[Project, ...] = ()
    runs: tuple[Run, ...] = ()
    pipelines: tuple[Pipeline, ...] = ()
    calls: int = 0
    elapsed: float = 0.0
    runs_more: bool = False
    pipelines_total: int = 0
    pipelines_truncated: bool = False

    @property
    def paused_count(self) -> int:
        return sum(1 for pipeline in self.pipelines if pipeline.is_paused)

    @property
    def disabled_count(self) -> int:
        return sum(1 for pipeline in self.pipelines if pipeline.is_disabled)

    def pipeline(self, pipeline_id: int) -> Pipeline | None:
        for candidate in self.pipelines:
            if candidate.id == pipeline_id:
                return candidate
        return None

    def run(self, run_id: int) -> Run | None:
        for candidate in self.runs:
            if candidate.id == run_id:
                return candidate
        return None

    def state_counts(self, state: str) -> dict[int, int]:
        """How many runs in `state` each pipeline has, from this snapshot's runs
        window. Derived, not fetched — it is what the `R` state filter narrows
        the Pipelines view by, and only says anything about the loaded window."""
        counts: dict[int, int] = {}
        for run in self.runs:
            if run.state == state:
                counts[run.pipeline_id] = counts.get(run.pipeline_id, 0) + 1
        return counts

    def in_flight_counts(self) -> dict[int, int]:
        """How many runs each pipeline currently has in flight.

        Derived from the runs window rather than fetched, and safe to derive: the
        poll asks for in-flight runs explicitly (see `azdo.fetch_snapshot`), so
        an in-flight run is in the window whatever its age.
        """
        counts: dict[int, int] = {}
        for run in self.runs:
            if run.in_flight:
                counts[run.pipeline_id] = counts.get(run.pipeline_id, 0) + 1
        return counts

    def latest_run_for(self, pipeline: Pipeline) -> Run | None:
        """The most recent run of `pipeline` we know about.

        The cached inventory's `latestBuild` and the live runs window are each
        incomplete in a different direction — the inventory can be up to its TTL
        stale, the window only reaches back so far — so the newer of the two
        wins. Without this, a pipeline that started a run since the inventory was
        cached would show its *previous* run for the rest of the TTL, which on a
        dashboard whose whole job is "what is running now" is the one error that
        matters.
        """
        newest = pipeline.last_run
        for run in self.runs:
            if run.pipeline_id != pipeline.id:
                continue
            if newest is None or run.sort_date > newest.sort_date:
                newest = run
        return newest


@dataclass(frozen=True, slots=True)
class PollRequest:
    """What the app asks one poll for.

    `project` is None only on the very first poll, when the app has no selection
    yet and the caller's closure picks the default. `run_limit` is how many runs
    the app wants once scrolling to the bottom of the runs list has grown it past
    the caller's own `--limit`; None leaves the caller's default in charge.
    """

    project: Project | None = None
    run_limit: int | None = None


# The mutating actions, in the order the confirmation modal describes them.
# `kind` is the discriminator the client dispatches on; every one is gated behind
# an explicit confirmation and recorded in the activity log.
ACTION_KINDS = ("queue", "cancel", "retry_stage")


@dataclass(frozen=True, slots=True)
class Action:
    """A requested mutation, fully specified before anyone confirms it.

    Built by the app, described to the user by the confirmation modal, and only
    then handed to the client — so what is confirmed is exactly what is sent.

    Azure DevOps has no dry-run for any of these, which is why the modal's
    wording is blunt rather than reassuring: unlike Airflow's clear endpoint,
    there is no "tell me what this would do" to offer first.
    """

    kind: str
    pipeline_id: int = 0
    pipeline_name: str = ""
    run_id: int = 0
    branch: str = ""
    stage_name: str = ""

    @property
    def target(self) -> str:
        parts: list[str] = []
        if self.pipeline_name:
            parts.append(self.pipeline_name)
        if self.run_id:
            parts.append(f"run {self.run_id}")
        if self.branch:
            parts.append(self.branch)
        if self.stage_name:
            parts.append(f"stage {self.stage_name}")
        return " · ".join(parts) or str(self.pipeline_id)

    @property
    def title(self) -> str:
        return {
            "queue": "Queue a new run",
            "cancel": "Cancel this run",
            "retry_stage": "Re-run a failed stage",
        }.get(self.kind, self.kind)

    @property
    def summary(self) -> str:
        """The one-liner the activity log records."""
        return f"{self.title}: {self.target}"


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One line in the activity log: what a poll or an action did, and when.

    `level` is "info" (a normal refresh), "warn" (a backoff), "error" (a failed
    poll), or "action" (a mutation the user confirmed). Messages reaching here
    have already been through `azdo._redact`.
    """

    time: datetime
    level: str
    message: str


def sort_runs(runs: list[Run]) -> list[Run]:
    """Newest first, by start time (falling back to queue time).

    The list reads as a timeline: the run that started most recently is on top,
    and scrolling down goes back in time — which is also what makes "the bottom
    of the list loads older runs" coherent. Failed runs are *marked*, not
    floated: the attention dot and the summary counts carry "what needs looking
    at", so the ordering can carry "when".
    """
    return sorted(runs, key=lambda run: -run.sort_date.timestamp())


def sort_pipelines(pipelines: list[Pipeline]) -> list[Pipeline]:
    """Most recently run first, then never-run ones by name.

    This is the order the Azure DevOps "Recent" tab uses, and it is the right
    default for the same reason: a pipeline that ran ten minutes ago is what you
    came to look at, and one that has not run since last year is not.
    """

    def key(pipeline: Pipeline) -> tuple[float, str]:
        run = pipeline.last_run
        when = run.sort_date.timestamp() if run is not None else _EPOCH.timestamp()
        return (-when, pipeline.name.casefold())

    return sorted(pipelines, key=key)


# --- the timeline tree -------------------------------------------------------
#
# The record pane shows a run's timeline as the tree Azure DevOps already
# describes: a stage above its jobs, a job above its tasks, indented to show the
# nesting. Everything below is a pure function of the record list, so it is
# unit-testable with no I/O.

# The tree glyphs, in the order a renderer needs them.
_TEE = "├─ "
_ELBOW = "└─ "
_PIPE = "│  "
_GAP = "   "


@dataclass(frozen=True, slots=True)
class RecordRow:
    """One row of the record pane: a timeline record, placed in the run's tree.

    `position` is the 1-based row number, `depth` the nesting depth, and `prefix`
    the tree glyphs to print before the name. `unplaced` marks a record the tree
    could not position — one whose parent is missing from the timeline, or a
    member of a parent cycle. Those still render, in a marked trailing group,
    because dropping a row from a monitoring view is a correctness bug.
    """

    record: Record
    position: int
    depth: int = 0
    prefix: str = ""
    unplaced: bool = False

    @property
    def label(self) -> str:
        return f"{self.prefix}{self.record.name}"


def _children_of(records: list[Record]) -> dict[str | None, list[Record]]:
    """`{parent_id: children}`, each sibling group in the order it should render.

    Siblings are ordered by azdo's own `order`, then by start time, then by name.
    All three, in that order, and none of them alone: `order` is per-parent and
    can repeat or be absent, start time is null for anything that never ran, and
    name is the only total tiebreak. Ties that fell through to nothing would let
    rows reshuffle between refreshes, which on a live dashboard reads as the
    pipeline having changed.

    A record whose `parent_id` names something absent from the timeline is
    re-parented to None here — a root — rather than dropped: `_placed` then
    treats it as a top-level row, which is a truthful placement for a record
    whose parent we were simply not given.
    """
    known = {record.id for record in records}
    groups: dict[str | None, list[Record]] = {}
    for record in records:
        parent = record.parent_id if record.parent_id in known else None
        groups.setdefault(parent, []).append(record)
    for group in groups.values():
        group.sort(
            key=lambda record: (
                record.order,
                (record.start_time or _EPOCH).timestamp(),
                record.name,
            )
        )
    return groups


def _placed(
    records: list[Record],
) -> tuple[list[tuple[Record, str | None, int]], list[Record]]:
    """Walk the tree depth-first, returning `([(record, parent_id, depth)], unplaced)`.

    Depth-first because that is what makes the pane read as the pipeline's shape
    rather than as four flat layers. A record reachable from no root — which
    means it is in a parent cycle, since a missing parent was already
    re-parented to None — comes back as unplaced rather than being lost.
    """
    groups = _children_of(records)
    ordered: list[tuple[Record, str | None, int]] = []
    seen: set[str] = set()

    def walk(record: Record, parent: str | None, depth: int) -> None:
        if record.id in seen:
            return  # a cycle; the second visit is where it would loop forever
        seen.add(record.id)
        ordered.append((record, parent, depth))
        for child in groups.get(record.id, ()):
            walk(child, record.id, depth + 1)

    for root in groups.get(None, ()):
        walk(root, None, 0)
    unplaced = [record for record in records if record.id not in seen]
    return ordered, unplaced


def _prefixes(ordered: list[tuple[Record, str | None, int]]) -> dict[str, str]:
    """The tree glyphs for each placed record.

    A record is the last child of its parent when no later row shares that
    parent; the vertical bars at the intermediate levels come from walking its
    ancestor chain and asking the same question of each ancestor.
    """
    parent_of = {record.id: parent for record, parent, _ in ordered}
    depth_of = {record.id: depth for record, _, depth in ordered}
    last_child_at: dict[str | None, str] = {}
    for record, parent, _ in ordered:
        last_child_at[parent] = record.id  # the final assignment wins

    def is_last(record_id: str) -> bool:
        return last_child_at.get(parent_of.get(record_id)) == record_id

    prefixes: dict[str, str] = {}
    for record, _, depth in ordered:
        if depth == 0:
            prefixes[record.id] = ""
            continue
        chain: list[str] = []
        walker = parent_of.get(record.id)
        while walker is not None and depth_of.get(walker, 0) > 0:
            chain.append(walker)
            walker = parent_of.get(walker)
        bars = "".join(_GAP if is_last(a) else _PIPE for a in reversed(chain))
        prefixes[record.id] = bars + (_ELBOW if is_last(record.id) else _TEE)
    return prefixes


def order_records(records: list[Record]) -> list[RecordRow]:
    """Place a run's timeline records in their tree order.

    Total by construction: every record handed in comes back out exactly once,
    whether or not the tree could place it — which is what the trailing assert
    is there to keep true. A flat list with no parents at all degrades to one
    depth-0 row per record, which is still useful; never to an error and never to
    a short list.
    """
    if not records:
        return []
    ordered, unplaced = _placed(records)
    prefixes = _prefixes(ordered)
    rows = [
        RecordRow(
            record=record,
            position=index,
            depth=depth,
            prefix=prefixes[record.id],
        )
        for index, (record, _, depth) in enumerate(ordered, start=1)
    ]
    rows += [
        RecordRow(record=record, position=len(rows) + offset, unplaced=True)
        for offset, record in enumerate(unplaced, start=1)
    ]
    assert len(rows) == len(records)  # totality is the point; never lose a row
    return rows


def collect_issues(records: list[Record]) -> list[tuple[Record, Issue]]:
    """Every issue in a run, paired with the record that reported it.

    Errors before warnings, and within each, tree order — so the `e` overlay
    reads as "here is what broke, in the order it broke". This is the whole
    reason the timeline is worth fetching in one piece: azdo already knows which
    step failed and with what message, so finding it should not mean grepping
    logs.
    """
    ordered = [row.record for row in order_records(records)]
    pairs = [
        (record, issue) for record in ordered for issue in record.issues
    ]
    return sorted(pairs, key=lambda pair: not pair[1].is_error)


@dataclass(frozen=True, slots=True)
class Drill:
    """Where the detail pane currently is: which run, record and log.

    Empty (`level == "runs"`) until the user drills in. Held as one value so the
    app can push/pop a level without several fields drifting apart.

    `rows` is the record list already placed in tree order — see
    `order_records`. `records` is the same set unplaced, for the charts.
    """

    level: str = "runs"
    run: Run | None = None
    record: Record | None = None
    records: tuple[Record, ...] = field(default_factory=tuple)
    rows: tuple[RecordRow, ...] = field(default_factory=tuple)
    log: RunLog | None = None
    loading: bool = False
    error: str | None = None


# --- filtering -------------------------------------------------------------
#
# `/` opens an incremental filter over whatever list is on screen. It is applied
# client-side to already-loaded rows, so it costs no API call and feels instant.

# The lists a filter can be active on, each remembering its own query. The
# Watched view keeps its own, so narrowing it never narrows the runs list.
FILTER_TARGETS = ("runs", "pipelines", "watched", "records", "log")

# The preset `E` drops into the log filter: the markers Azure Pipelines prints
# around a failure. A preset rather than a separate mode, so `esc` clears it and
# the match count reads the same way as any other query — and so it can be typed
# over. Space-separated terms are AND-ed by `matches`, so this is deliberately
# one term: the marker azdo writes for every error, whatever printed it.
LOG_ERROR_QUERY = "##[error]"


def matches(query: str, text: str) -> bool:
    """Case-insensitive substring match, with an empty query matching everything.

    Space-separated terms must *all* appear, in any order — so `failed lint`
    finds a failed run of a lint stage without caring which word comes first.
    """
    terms = query.casefold().split()
    if not terms:
        return True
    folded = text.casefold()
    return all(term in folded for term in terms)


def filter_log(content: str, query: str) -> tuple[list[tuple[int, str]], int]:
    """The log lines matching `query`, as `[(line_number, text)]`, plus the total.

    Line numbers are 1-based and refer to the *unfiltered* log, so a filtered
    view still tells you where in the log you are.
    """
    lines = content.splitlines()
    if not query.strip():
        return list(enumerate(lines, start=1)), len(lines)
    hits = [
        (number, line)
        for number, line in enumerate(lines, start=1)
        if matches(query, line)
    ]
    return hits, len(lines)


# --- azdo log noise ----------------------------------------------------------
#
# Azure Pipelines logs are not plain text. Every line is prefixed with an ISO
# timestamp the agent stamped on, and task runners emit ANSI colour codes inside
# them. Both are noise in a pane that has its own line numbers and its own
# colours — and the ANSI codes are worse than noise, since Rich renders the raw
# escape bytes as mojibake. Stripped at parse time (`api.parse_log`) so
# everything downstream, including the `/` filter and the gw report, sees clean
# text.

# `2026-07-30T16:51:53.1532775Z ` — the agent's per-line timestamp.
_LOG_STAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")

# CSI escape sequences, which is every colour code a test runner emits.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean_log_line(line: str) -> str:
    """One log line with the agent's timestamp and any ANSI codes removed.

    The timestamp goes because the pane already numbers lines and the wall-clock
    of one line inside a task is rarely the question; the escape codes go because
    they are control bytes, and passing control bytes to a terminal UI is how a
    log pane starts painting over the rest of the screen.
    """
    return _ANSI_RE.sub("", _LOG_STAMP_RE.sub("", line)).rstrip("\r")


# --- links in a log --------------------------------------------------------
#
# A pipeline log is where a step tells you where the real work happened — a
# published artifact, a test report, a deployed environment, a Databricks job.
# So URLs are found as spans (the UI turns them into clickable links) rather
# than reformatted, leaving each line exactly as the log wrote it.

# A URL runs to the first character that cannot be in one. Quotes, angle
# brackets and backticks stop it because logs wrap URLs in them.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")

# Punctuation a log tends to put *after* a URL rather than in it. Trimmed from
# the end so "see https://host/build/1." does not link the full stop.
_URL_TRAILING = ".,;:!?"

# A closing bracket only ends the URL when its opener is not inside it, so
# "https://host/a_(b)" keeps its parenthesis and "(https://host/a)" does not.
_URL_BRACKETS = {")": "(", "]": "[", "}": "{"}


def _trim_url(url: str) -> str:
    """`url` without the trailing punctuation the surrounding prose owns."""
    while url:
        last = url[-1]
        if last in _URL_TRAILING:
            url = url[:-1]
        elif last in _URL_BRACKETS and url.count(_URL_BRACKETS[last]) < url.count(last):
            url = url[:-1]
        else:
            break
    return url


def find_urls(line: str) -> tuple[tuple[int, int, str], ...]:
    """Every http(s) URL in `line`, as `(start, end, url)` spans into it.

    Spans rather than strings so the log pane can style the URL in place,
    leaving the line's text exactly as the log wrote it.
    """
    found: list[tuple[int, int, str]] = []
    for match in _URL_RE.finditer(line):
        url = _trim_url(match.group())
        if url:
            found.append((match.start(), match.start() + len(url), url))
    return tuple(found)
