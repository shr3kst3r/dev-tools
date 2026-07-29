"""Plain data models for the airflow-watch dashboard.

Kept free of any I/O so they are trivial to construct in tests, and free of any
API-version knowledge so the version seam stays in one place (`api.py`): these
are the *normalized* shapes the UI renders, not the wire shapes.

Run and task states are deliberately **plain strings, not enums**. A monitoring
tool must survive an Airflow release inventing a state it has never heard of —
Airflow 3's `awaiting_input` is the worked example — so nothing here validates a
state; `ui.state_style` buckets unknown values into a neutral fallback instead
(see the airflow-2-only-behind-a-version-seam ADR and the
airflow-3-joins-the-version-seam ADR that widened it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# The states this build has heard of. Not a closed set and never used for
# validation: anything absent from here still renders, in `ui.state_style`'s
# fallback bucket. `KNOWN_RUN_STATES` is the `--state` flag's help text;
# both lists are what the test suite asserts `ui.state_style` has a colour for,
# so a state added here without a colour is a failing test rather than a
# question-marked row.
KNOWN_RUN_STATES = ("queued", "running", "success", "failed")
KNOWN_TASK_STATES = (
    "success",
    "running",
    "failed",
    "upstream_failed",
    "skipped",
    "up_for_retry",
    "up_for_reschedule",
    "queued",
    "scheduled",
    "deferred",
    "removed",
    "restarting",
    "none",
)

# The states worth looking at first: a run in one of these is why you opened
# the tool. Drives the default sort and the summary bar's counts.
ATTENTION_RUN_STATES = ("failed", "running", "queued")

# Task states that mean "this is the thing that broke".
FAILED_TASK_STATES = ("failed", "upstream_failed")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class Deployment:
    """One Astro deployment (or one plain self-hosted Airflow, addressed by
    `api_url` with an empty `id`).

    `airflow_version` comes from discovery, which is what lets every subsequent
    `astro` call pin `--airflow-version` and skip version auto-detection. Note
    there is deliberately no `supported` property here: deciding whether a
    version is supported is version-specific knowledge and lives behind the
    seam, as `api.supports()`.
    """

    id: str
    name: str
    workspace_name: str = ""
    airflow_version: str = ""
    status: str = ""
    api_url: str = ""
    # Astro reports hibernation in `scalingStatus.hibernationStatus`; a
    # hibernating deployment has no running webserver, so its Airflow API is
    # simply absent rather than broken.
    hibernating: bool = False

    @property
    def is_hibernating(self) -> bool:
        return self.hibernating or self.status.upper() == "HIBERNATING"

    @property
    def is_astro(self) -> bool:
        """True when this is an Astro deployment addressed by id; False for a
        plain Airflow addressed by URL."""
        return bool(self.id)

    @property
    def label(self) -> str:
        if self.workspace_name:
            return f"{self.workspace_name} / {self.name}"
        return self.name

    @property
    def key(self) -> str:
        """Stable identity for selection and layout persistence."""
        return self.id or self.api_url


@dataclass(frozen=True, slots=True)
class DagRun:
    """One DAG run, as the runs list shows it."""

    dag_id: str
    run_id: str
    state: str
    run_type: str = ""
    logical_date: datetime | None = None
    # When the run was eligible to run. Airflow 3 stamps this on every run and
    # allows `logical_date` to be null (a manually triggered run often has no
    # logical date at all); Airflow 2 has no such field, so it stays None there.
    # Version-free data: which wire name fills it is the seam's business.
    run_after: datetime | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    note: str | None = None

    @property
    def key(self) -> str:
        return f"{self.dag_id}\x00{self.run_id}"

    @property
    def duration(self) -> float | None:
        """Seconds the run took, or None when it never started.

        A run that started but has not ended has no duration yet — the UI
        computes live elapsed time from `start_date` and its own clock, so this
        stays a pure function of the record.
        """
        if self.start_date is None or self.end_date is None:
            return None
        return max(0.0, (self.end_date - self.start_date).total_seconds())

    @property
    def needs_attention(self) -> bool:
        return self.state in ATTENTION_RUN_STATES

    @property
    def happened_at(self) -> datetime | None:
        """When the run happened: started if it did, else its logical date.

        `run_after` is the last resort for an Airflow 3 run that has not started
        and has no logical date — without it, a just-triggered run would have no
        place in time at all. None means the run truly cannot be dated.
        """
        return self.start_date or self.logical_date or self.run_after

    @property
    def sort_date(self) -> datetime:
        """Newest-first ordering key: `happened_at`, with an undatable run
        pinned to the epoch — the bottom of a newest-first list."""
        return self.happened_at or _EPOCH

    @property
    def search_text(self) -> str:
        """What the `/` filter matches a run row against."""
        return " ".join((self.dag_id, self.run_id, self.state, self.run_type))


@dataclass(frozen=True, slots=True)
class TaskInstance:
    """One task instance inside a run."""

    task_id: str
    state: str
    try_number: int = 0
    max_tries: int = 0
    operator: str = ""
    start_date: datetime | None = None
    end_date: datetime | None = None
    pool: str = ""
    map_index: int = -1

    @property
    def key(self) -> str:
        return f"{self.task_id}\x00{self.map_index}"

    @property
    def display_id(self) -> str:
        """`task_id`, with the map index appended for a mapped task."""
        return self.task_id if self.map_index < 0 else f"{self.task_id}[{self.map_index}]"

    @property
    def duration(self) -> float | None:
        if self.start_date is None or self.end_date is None:
            return None
        return max(0.0, (self.end_date - self.start_date).total_seconds())

    @property
    def failed(self) -> bool:
        return self.state in FAILED_TASK_STATES

    @property
    def search_text(self) -> str:
        """What the `/` filter matches a task row against."""
        return " ".join((self.task_id, self.state, self.operator, self.pool))

    @property
    def tries(self) -> tuple[int, ...]:
        """The try numbers whose logs can be fetched, oldest first.

        Airflow reports `try_number` as the *current* attempt and `max_tries`
        as the retry budget; logs exist for attempts 1..try_number. A task that
        never ran (try_number 0) still has an attempt-1 log endpoint, which
        returns the "no logs found" body rather than a 404.
        """
        return tuple(range(1, max(1, self.try_number) + 1))


@dataclass(frozen=True, slots=True)
class Dag:
    """One DAG, as the DAG list shows it.

    Paused and *stale* DAGs are both first-class rows. A stale DAG
    (`is_active` false) is one whose file is no longer in the bundle; Airflow's
    own list endpoint hides those by default, and hiding a row is exactly what a
    monitoring tool must not do — so we ask for them and label them instead.
    """

    dag_id: str
    is_paused: bool = False
    owners: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    has_import_errors: bool = False
    next_dagrun: datetime | None = None
    description: str = ""
    is_active: bool = True
    schedule: str = ""
    fileloc: str = ""

    @property
    def key(self) -> str:
        return self.dag_id

    @property
    def is_stale(self) -> bool:
        """True when Airflow still knows this DAG but its file is gone."""
        return not self.is_active

    def import_error_is_live(self, live_files: frozenset[str]) -> bool:
        """Whether this DAG's `has_import_errors` flag is worth showing.

        Airflow sets the flag on the DAG row when its file fails to parse, but
        nothing ever clears it: once the file is gone, no parse re-runs, so the
        flag is left behind for good. Measured on a real deployment: 51 DAGs
        carried the flag while `/importErrors` held **zero** entries, and every
        one of the 51 was already stale.

        So the flag alone is not evidence. It is live if the file is currently in
        the import-error list, and otherwise only trusted for a DAG Airflow still
        considers present — where the flag can be real and `/importErrors` may
        merely be truncated. For a stale DAG we believe the absence instead,
        because "the file is gone" already explains the flag.
        """
        if self.fileloc and self.fileloc in live_files:
            return True
        return self.has_import_errors and not self.is_stale

    @property
    def needs_attention(self) -> bool:
        return self.has_import_errors or self.is_stale

    @property
    def search_text(self) -> str:
        """What the `/` filter matches a DAG row against."""
        return " ".join((self.dag_id, self.description, *self.owners, *self.tags))


@dataclass(frozen=True, slots=True)
class ImportErrorEntry:
    """A DAG file that failed to parse. Named with the trailing word spelled
    out rather than shadowing the builtin `ImportError`."""

    filename: str
    stacktrace: str = ""
    timestamp: datetime | None = None

    @property
    def key(self) -> str:
        return self.filename

    @property
    def short_filename(self) -> str:
        return self.filename.rsplit("/", 1)[-1]


def live_import_error_files(
    errors: tuple[ImportErrorEntry, ...],
) -> frozenset[str]:
    """The DAG files Airflow is *currently* failing to parse.

    Paired with `Dag.import_error_is_live` to tell a real import error from the
    flag Airflow leaves behind on a DAG whose file has since been deleted.
    """
    return frozenset(error.filename for error in errors if error.filename)


@dataclass(frozen=True, slots=True)
class TaskLog:
    """One attempt's log, as fetched for the log pane.

    `continuation_token` is what Airflow returns to resume a log read from where
    this one stopped. It is carried, not used: a `full_content` request is
    answered with the entire body and a token regardless, so there is nothing
    here to page through — and the token is signed with the webserver's secret,
    so we cannot read the `end_of_log` flag inside it. Incremental tailing of a
    running task's log would use it; nothing does today.

    `truncated` says the fetch stopped short of the whole body
    (`astro.MAX_LOG_CHARS`), so the pane can say so instead of implying the log
    ends where our buffer did.
    """

    content: str
    try_number: int
    continuation_token: str | None = None
    truncated: bool = False

    @property
    def lines(self) -> list[str]:
        return self.content.splitlines()


@dataclass(frozen=True, slots=True)
class DagList:
    """A DAG list together with what the server said about it.

    The three travel as one value because they are only true together: a list of
    1,200 DAGs out of 10,000 is honest, the same list without `total` and
    `truncated` is not. Passing the DAGs alone between polls — which is what a
    cache does — is how a truncation quietly stops being reported, so the cache
    holds this, not a bare tuple.
    """

    dags: tuple[Dag, ...] = ()
    total: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything one poll of a deployment produced.

    `calls` and `elapsed` are observability, not data: the activity log reports
    how many `astro` invocations a refresh cost and how long they took, which
    is the only way to tell a slow deployment from a stuck tool.

    The `*_total` fields are what the server said exists, as opposed to what we
    hold. They exist so a truncated list can *say* it is truncated: Airflow caps
    a page at 100 whatever you ask for, so "50 rows" is meaningless on its own.
    """

    deployment: Deployment
    deployments: tuple[Deployment, ...] = ()
    runs: tuple[DagRun, ...] = ()
    dags: tuple[Dag, ...] = ()
    import_errors: tuple[ImportErrorEntry, ...] = ()
    calls: int = 0
    elapsed: float = 0.0
    runs_total: int = 0
    dags_total: int = 0
    # Set when the run list was cut short by our page ceiling rather than by the
    # requested limit — the signal that scrolling for more runs cannot get more.
    runs_truncated: bool = False
    # Set when the DAG list was cut short by our page ceiling, which is what
    # makes a client-side DAG filter incomplete and a server-side one necessary.
    dags_truncated: bool = False

    @property
    def paused_count(self) -> int:
        return sum(1 for dag in self.dags if dag.is_paused)

    @property
    def stale_count(self) -> int:
        return sum(1 for dag in self.dags if dag.is_stale)

    def dag(self, dag_id: str) -> Dag | None:
        for candidate in self.dags:
            if candidate.dag_id == dag_id:
                return candidate
        return None

    def state_counts(self, state: str) -> dict[str, int]:
        """How many runs in `state` each DAG has, from this snapshot's runs
        window. Derived, not fetched — it is what the `R` state filter narrows
        the DAGs view by, and only says anything about the loaded window."""
        counts: dict[str, int] = {}
        for run in self.runs:
            if run.state == state:
                counts[run.dag_id] = counts.get(run.dag_id, 0) + 1
        return counts

    def running_counts(self) -> dict[str, int]:
        """How many currently-running runs each DAG has, from this snapshot's
        runs window. Derived, not fetched: the DAGs view shows it, and a
        running run is by nature recent enough to be inside the window."""
        return self.state_counts("running")


@dataclass(frozen=True, slots=True)
class PollRequest:
    """What the app asks one poll for.

    `deployment` is None only on the very first poll, when the app has no
    selection yet and the caller's closure picks the default. `dag_pattern` is
    forwarded to the server-side DAG filter — used when the DAG list is too large
    to have been fully loaded, so filtering cannot be done client-side alone.
    `run_limit` is how many runs the app wants, once scrolling to the bottom of
    the runs list has grown it past the caller's own `--limit`; None leaves the
    caller's default in charge.
    """

    deployment: Deployment | None = None
    dag_pattern: str = ""
    run_limit: int | None = None


# The mutating actions, in the order the confirmation modal describes them.
# `kind` is the discriminator the client dispatches on; every one of these is
# gated behind an explicit confirmation and recorded in the activity log.
ACTION_KINDS = ("pause", "unpause", "trigger", "clear", "mark")


@dataclass(frozen=True, slots=True)
class Action:
    """A requested mutation, fully specified before anyone confirms it.

    Built by the app, described to the user by the confirmation modal, and only
    then handed to the client. `dry_run` is carried explicitly rather than left
    to a default, because "did it actually fire?" has to be a property of the
    request: the seam maps the flag to whichever dry-run mechanism the target's
    version has, and every one of those defaults to *doing nothing*.

    `map_index` is the selected task instance's own — `-1` for an unmapped task,
    which is Airflow's sentinel too. It is the instance's coordinate rather than
    a wire name, so it belongs here; what a version *does* with it (a body
    field, a path segment, or nothing) is the seam's business.
    """

    kind: str
    dag_id: str
    run_id: str | None = None
    task_ids: tuple[str, ...] = ()
    state: str = ""
    dry_run: bool = False
    map_index: int = -1

    @property
    def mutates(self) -> bool:
        """False for a dry run: it reports what *would* change, and changes
        nothing."""
        return not self.dry_run

    @property
    def target(self) -> str:
        parts = [self.dag_id]
        if self.run_id:
            parts.append(self.run_id)
        if self.task_ids:
            parts.append(", ".join(self.task_ids))
        return " · ".join(parts)

    @property
    def title(self) -> str:
        titles = {
            "pause": "Pause DAG",
            "unpause": "Unpause DAG",
            "trigger": "Trigger a new DAG run",
            "clear": "Clear (retry) task instances",
            "mark": f"Mark task instances {self.state or '?'}",
        }
        title = titles.get(self.kind, self.kind)
        return f"{title} — dry run" if self.dry_run else title

    @property
    def summary(self) -> str:
        """The one-liner the activity log records."""
        return f"{self.title}: {self.target}"


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One line in the activity log: what a poll or an action did, and when.

    `level` is "info" (a normal refresh), "warn" (a backoff), "error" (a failed
    poll), or "action" (a mutation the user confirmed). Messages reaching here
    have already been through `astro._redact`.
    """

    time: datetime
    level: str
    message: str


def sort_runs(runs: list[DagRun]) -> list[DagRun]:
    """Newest first, by start time.

    The list reads as a timeline: the run that started most recently is on
    top, and scrolling down goes back in time — which is also what makes
    "the bottom of the list loads older runs" coherent. (`sort_date` falls
    back to the logical date, then `run_after`, for a run that has not
    started; a run with no date at all sinks to the bottom.) Failed runs are
    *marked*, not floated: the attention dot and the summary counts carry
    "what needs looking at", so the ordering can carry "when".
    """
    return sorted(runs, key=lambda run: -run.sort_date.timestamp())


def sort_task_instances(tasks: list[TaskInstance]) -> list[TaskInstance]:
    """Start-order fallback for when a DAG's structure is unknown.

    Failed tasks first, then by start time, then by name. Used when the task
    graph could not be fetched; the normal path is `order_task_instances`, which
    shows dependency order instead.
    """

    def key(task: TaskInstance) -> tuple[bool, float, str]:
        return (
            not task.failed,
            (task.start_date or _EPOCH).timestamp(),
            task.display_id,
        )

    return sorted(tasks, key=key)


# --- dependency ordering ---------------------------------------------------
#
# The task pane shows tasks in *dependency* order, not start order: an upstream
# task sits above the tasks it feeds, indented to show the edge. Everything below
# is a pure function of `{task_id: downstream_task_ids}` plus the task-instance
# list, so it is unit-testable with no I/O.

# The tree glyphs, in the order a renderer needs them.
_TEE = "├─ "
_ELBOW = "└─ "
_PIPE = "│  "
_GAP = "   "


@dataclass(frozen=True, slots=True)
class TaskRow:
    """One row of the task pane: a task instance, placed in the DAG's graph.

    `position` is the 1-based row number, `depth` the dependency depth, and
    `prefix` the tree glyphs to print before the task id. `unplaced` marks a task
    the graph could not position — a cycle member, or a task whose upstream has
    no instance in this run. Those still render, in a marked trailing group,
    because dropping a task from a monitoring view is a correctness bug.
    """

    task: TaskInstance
    position: int
    depth: int = 0
    prefix: str = ""
    unplaced: bool = False

    @property
    def label(self) -> str:
        return f"{self.prefix}{self.task.display_id}"


def _restrict(
    graph: dict[str, tuple[str, ...]], present: set[str]
) -> dict[str, tuple[str, ...]]:
    """Keep only edges where both ends have a task instance in this run.

    A task defined in the DAG but absent from the run (a new task, a task behind
    a branch) must not strand its downstream: dropping the edge makes that
    downstream a root, which is placeable, rather than leaving it unplaced.
    """
    return {
        task_id: tuple(d for d in downstream if d in present)
        for task_id, downstream in graph.items()
        if task_id in present
    }


def _topological_order(
    graph: dict[str, tuple[str, ...]], present: set[str]
) -> tuple[list[tuple[str, str | None, int]], list[str]]:
    """Order task ids so every task follows all of its upstreams.

    Returns `([(task_id, parent_id, depth)], unplaced_ids)`.

    A depth-first-flavoured Kahn: a task becomes *ready* only once every upstream
    has been emitted (which is what keeps upstreams above downstreams even in a
    diamond), and among ready tasks we take the most recently unlocked one, which
    is what makes the result read as a tree rather than as layers. Ties break on
    task id in both places, so the rows never reshuffle between refreshes.

    Anything still blocked when nothing is ready is in a cycle (or downstream of
    one) and is returned as unplaced rather than dropped.
    """
    edges = _restrict(graph, present)
    downstream_of: dict[str, tuple[str, ...]] = {
        task_id: edges.get(task_id, ()) for task_id in sorted(present)
    }
    indegree: dict[str, int] = {task_id: 0 for task_id in present}
    for children in downstream_of.values():
        for child in children:
            indegree[child] += 1

    # LIFO, seeded in reverse id order so the smallest id pops first.
    stack: list[tuple[str, str | None, int]] = [
        (task_id, None, 0)
        for task_id in sorted(present, reverse=True)
        if indegree[task_id] == 0
    ]
    ordered: list[tuple[str, str | None, int]] = []
    emitted: set[str] = set()
    while stack:
        task_id, parent, depth = stack.pop()
        if task_id in emitted:
            continue
        emitted.add(task_id)
        ordered.append((task_id, parent, depth))
        unlocked: list[str] = []
        for child in downstream_of[task_id]:
            indegree[child] -= 1
            if indegree[child] == 0 and child not in emitted:
                unlocked.append(child)
        for child in sorted(unlocked, reverse=True):
            stack.append((child, task_id, depth + 1))

    unplaced = sorted(task_id for task_id in present if task_id not in emitted)
    return ordered, unplaced


def _prefixes(ordered: list[tuple[str, str | None, int]]) -> dict[str, str]:
    """The tree glyphs for each ordered task id.

    A task is the last child of its parent when no later row shares that parent;
    the vertical bars at the intermediate levels come from walking its ancestor
    chain and asking the same question of each ancestor.
    """
    parent_of = {task_id: parent for task_id, parent, _ in ordered}
    depth_of = {task_id: depth for task_id, _, depth in ordered}
    last_child_at: dict[str | None, str] = {}
    for task_id, parent, _ in ordered:
        last_child_at[parent] = task_id  # the final assignment wins

    def is_last(task_id: str) -> bool:
        return last_child_at.get(parent_of.get(task_id)) == task_id

    prefixes: dict[str, str] = {}
    for task_id, _, depth in ordered:
        if depth == 0:
            prefixes[task_id] = ""
            continue
        # Ancestors from the child's parent upwards, excluding the root level.
        chain: list[str] = []
        walker = parent_of.get(task_id)
        while walker is not None and depth_of.get(walker, 0) > 0:
            chain.append(walker)
            walker = parent_of.get(walker)
        bars = "".join(_GAP if is_last(a) else _PIPE for a in reversed(chain))
        prefixes[task_id] = bars + (_ELBOW if is_last(task_id) else _TEE)
    return prefixes


def order_task_instances(
    tasks: list[TaskInstance],
    graph: dict[str, tuple[str, ...]] | None = None,
) -> list[TaskRow]:
    """Place a run's task instances in their DAG's dependency order.

    Total by construction: every task instance handed in comes back out exactly
    once, whether or not the graph could place it. Without a graph (the call
    failed, or a DAG with no edges) it degrades to `sort_task_instances` order at
    depth 0, which is still useful — never to an error and never to a short list.

    Mapped tasks share one `task_id`, so all of a task's instances sit together
    at that task's position, ordered by map index.
    """
    if not tasks:
        return []
    by_id: dict[str, list[TaskInstance]] = {}
    for task in tasks:
        by_id.setdefault(task.task_id, []).append(task)
    for instances in by_id.values():
        instances.sort(key=lambda t: t.map_index)

    if not graph:
        return [
            TaskRow(task=task, position=index)
            for index, task in enumerate(sort_task_instances(tasks), start=1)
        ]

    ordered, unplaced = _topological_order(graph, set(by_id))
    prefixes = _prefixes(ordered)
    rows: list[TaskRow] = []
    for task_id, _, depth in ordered:
        for instance in by_id[task_id]:
            rows.append(
                TaskRow(
                    task=instance,
                    position=len(rows) + 1,
                    depth=depth,
                    prefix=prefixes[task_id],
                )
            )
    for task_id in unplaced:
        for instance in by_id[task_id]:
            rows.append(
                TaskRow(task=instance, position=len(rows) + 1, unplaced=True)
            )
    assert len(rows) == len(tasks)  # totality is the point; never lose a task
    return rows


@dataclass(frozen=True, slots=True)
class Drill:
    """Where the detail pane currently is: which run, task and attempt.

    Empty (`level == "runs"`) until the user drills in. Held as one value so
    the app can push/pop a level without several fields drifting apart.

    `rows` is the task list already placed in dependency order — see
    `order_task_instances`. `tasks_total` is what the server said the run has, so
    a truncated task list can say so.
    """

    level: str = "runs"
    run: DagRun | None = None
    task: TaskInstance | None = None
    try_number: int = 1
    tasks: tuple[TaskInstance, ...] = field(default_factory=tuple)
    rows: tuple[TaskRow, ...] = field(default_factory=tuple)
    tasks_total: int = 0
    log: TaskLog | None = None
    loading: bool = False
    error: str | None = None


# --- filtering -------------------------------------------------------------
#
# `/` opens an incremental filter over whatever list is on screen. It is applied
# client-side to already-loaded rows, so it costs no API call and feels instant.

# The lists a filter can be active on, each remembering its own query. The
# Watched view keeps its own, so narrowing it never narrows the runs list.
FILTER_TARGETS = ("runs", "dags", "watched", "tasks", "log")


def matches(query: str, text: str) -> bool:
    """Case-insensitive substring match, with an empty query matching everything.

    Space-separated terms must *all* appear, in any order — so `failed sync`
    finds a failed run of a sync DAG without caring which word comes first.
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


# --- links in a log --------------------------------------------------------
#
# An Airflow log is where a task tells you where the real work happened. The
# Databricks operators are the case that matters here: the run they submit does
# its work in Databricks, and the only pointer to it is a run-page URL logged
# once, somewhere in thousands of lines. So URLs are found as spans (the UI
# turns them into clickable links) and the Databricks run page is singled out.

# A URL runs to the first character that cannot be in one. Quotes, angle
# brackets and backticks stop it because logs wrap URLs in them.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")

# Punctuation a log tends to put *after* a URL rather than in it. Trimmed from
# the end so "…logs at https://host/#job/1/run/2." does not link the full stop.
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


def is_databricks_url(url: str) -> bool:
    """Whether `url` points at a Databricks workspace.

    Matched on the host containing "databricks", which covers every workspace
    domain Databricks hands out — `dbc-….cloud.databricks.com`,
    `adb-….azuredatabricks.net`, `….gcp.databricks.com`. A vanity domain that
    hides the word is not recognized, and cannot be without guessing.
    """
    host = url.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return "databricks" in host.casefold()


# What a Databricks *run page* looks like in either of the two shapes the
# operators log: the legacy fragment (`#job/123/run/456`) and the current path
# (`/jobs/123/runs/456`).
_DATABRICKS_RUN_RE = re.compile(r"#job/\d+/run/\d+|/jobs/\d+/runs/\d+")


def databricks_run_url(content: str) -> str | None:
    """The Databricks run page a log points at, or None if it names none.

    A run page is preferred over any other Databricks URL in the log — the
    operators log the run page once and workspace links (docs, cluster pages)
    several times — but a workspace URL is still returned when that is all
    there is, since it at least lands you in the right place.
    """
    fallback: str | None = None
    for line in content.splitlines():
        for _, _, url in find_urls(line):
            if not is_databricks_url(url):
                continue
            if _DATABRICKS_RUN_RE.search(url):
                return url
            if fallback is None:
                fallback = url
    return fallback
