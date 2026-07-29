"""Rich rendering for airflow-watch.

Everything here is a pure function of the data plus a clock value, so every
layout can be snapshotted in `--once` mode and asserted on in tests. The live
app (see app.py) owns the polling and the windows; nothing in this module knows
about I/O, Textual, or API versions.

The one rule worth stating: `state_style` is the *only* place a state string
turns into a colour, and it never rejects one. An Airflow release that invents a
task state — 3.x's `awaiting_input`, say — renders it in a neutral bucket, per
the airflow-2-only-behind-a-version-seam ADR and the
airflow-3-joins-the-version-seam ADR that widened it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from rich.align import Align
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from tools.pr_watch.ui import format_duration, format_relative

from . import api
from .models import (
    Action,
    Dag,
    DagRun,
    Deployment,
    Drill,
    ImportErrorEntry,
    LogEntry,
    Snapshot,
    TaskInstance,
    TaskLog,
    TaskRow,
    databricks_run_url,
    filter_log,
    find_urls,
    live_import_error_files,
)

# state -> (glyph, rich style). Known values only; `state_style` falls back for
# anything else rather than raising, which is what keeps a future Airflow state
# from taking the dashboard down.
_STATE_STYLE: dict[str, tuple[str, str]] = {
    # run + task states that mean the same thing in both
    "success": ("✔", "bold green"),
    "failed": ("✖", "bold red"),
    "running": ("●", "bold cyan"),
    "queued": ("◌", "yellow"),
    # task-only states
    "upstream_failed": ("⊘", "red"),
    "skipped": ("○", "dim"),
    "up_for_retry": ("↻", "bold yellow"),
    "up_for_reschedule": ("↻", "yellow"),
    "scheduled": ("◌", "yellow"),
    "deferred": ("⏸", "magenta"),
    "restarting": ("↻", "yellow"),
    "removed": ("✂", "dim"),
    "none": ("—", "dim"),
}

# What an unrecognized state renders as. Magenta and question-marked so it reads
# as "the tool doesn't know this one" rather than as a normal state — visible,
# but never fatal.
FALLBACK_STATE_STYLE = ("?", "magenta")

_LIST_COLUMNS = ("", "DAG", "Run", "Type", "State", "Started", "Duration")
# The task pane is a dependency tree, so it leads with a position number: the
# indentation shows *what* feeds what, the number gives you something to say out
# loud ("row 3 is the one that failed").
_TASK_COLUMNS = ("#", "", "Task", "State", "Try", "Operator", "Started", "Duration")
_DAG_COLUMNS = ("", "DAG", "Running", "Schedule", "Owners", "Tags", "Next run")

_DAG_ID_WIDTH = 34
_RUN_ID_WIDTH = 26

# The views the master list can show, in the order `v` cycles through. The
# Watched view is the runs list narrowed to the runs `w` has marked — same
# columns, same drill-down, its own cursor and filter.
VIEWS = ("runs", "dags", "watched")
VIEW_LABELS = {"runs": "DAG runs", "dags": "DAGs", "watched": "Watched"}

# The views whose rows are DAG runs. Everything that renders or selects a run
# asks this rather than testing for "runs", so the Watched view inherits the
# whole run machinery instead of re-implementing it.
RUN_VIEWS = ("runs", "watched")

# What `R` cycles the run-state filter through, None meaning "all". Running
# comes first — `R` grew up as a running-only toggle, so one press still
# answers "what is in flight?" — then the rest in attention order.
STATE_FILTERS: tuple[str | None, ...] = (None, "running", "failed", "queued", "success")


def state_style(value: str) -> tuple[str, str]:
    """(glyph, style) for a run or task state.

    Never raises and never rejects: an unknown state — including one no release
    of Airflow has shipped yet — gets the neutral fallback bucket.
    """
    return _STATE_STYLE.get((value or "none").strip().lower(), FALLBACK_STATE_STYLE)


def state_cell(value: str) -> Text:
    glyph, style = state_style(value)
    return Text(f"{glyph} {value or 'none'}", style=style)


def _elide(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _duration_cell(
    duration: float | None, start: datetime | None, now: datetime
) -> Text:
    """How long it took, or how long it has been going.

    A record with a start but no end is still running, so we count from `now` —
    which is passed in, keeping this pure.
    """
    if duration is not None:
        return Text(format_duration(duration), style="dim")
    if start is not None:
        return Text(format_duration((now - start).total_seconds()), style="yellow")
    return Text("—", style="dim")


# --- the runs list ---------------------------------------------------------


def list_columns() -> tuple[str, ...]:
    return _LIST_COLUMNS


def attention_cell(run: DagRun, watched: bool = False) -> Text:
    """Red dot: failed. Cyan dot: in flight. Blank: settled and fine.

    A watched run additionally carries a star, whatever its state — the mark
    has to survive the run settling, or a watched run that finished would look
    like one that was never marked.
    """
    if run.state == "failed":
        cell = Text("●", style="bold red")
    elif run.state in ("running", "queued"):
        cell = Text("●", style="bold cyan")
    else:
        cell = Text(" ")
    if watched:
        cell.append("★", style="bold yellow")
    return cell


def list_row(run: DagRun, now: datetime, watched: bool = False) -> tuple[Text, ...]:
    """The cells for one run row, in `list_columns()` order."""
    return (
        attention_cell(run, watched),
        Text(_elide(run.dag_id, _DAG_ID_WIDTH), style="cyan"),
        Text(_elide(run.run_id, _RUN_ID_WIDTH), style="dim"),
        Text(run.run_type or "—"),
        state_cell(run.state),
        Text(format_relative(run.happened_at, now), style="dim"),
        _duration_cell(run.duration, run.start_date, now),
    )


def task_columns() -> tuple[str, ...]:
    return _TASK_COLUMNS


def task_row(row: TaskRow, now: datetime) -> tuple[Text, ...]:
    """One row of the dependency tree.

    The task id carries its tree prefix, so the pane reads as the DAG's shape:
    an upstream task above the tasks it feeds, each indented under its parent. A
    task the graph could not place is marked rather than dropped.
    """
    task = row.task
    label = Text()
    if row.prefix:
        label.append(row.prefix, style="dim")
    label.append(task.display_id, style="cyan")
    if row.unplaced:
        label.append("  (unlinked)", style="dim yellow")
    return (
        Text(str(row.position), style="dim"),
        Text("●", style="bold red") if task.failed else Text(" "),
        label,
        state_cell(task.state),
        Text(f"{task.try_number}/{task.max_tries}" if task.max_tries else str(task.try_number)),
        Text(_elide(task.operator or "—", 26), style="dim"),
        Text(format_relative(task.start_date, now), style="dim"),
        _duration_cell(task.duration, task.start_date, now),
    )


# --- the DAG list ----------------------------------------------------------


def dag_columns() -> tuple[str, ...]:
    return _DAG_COLUMNS


def dag_marker(dag: Dag, live_errors: frozenset[str] = frozenset()) -> Text:
    """Why this DAG might need you: a broken file, a deleted file, or paused.

    Paused DAGs are *labelled*, never filtered out. Stale DAGs are hidden from
    the DAGs view by default (`s` shows them, and the summary bar always counts
    them) — but we still deliberately ask Airflow's list endpoint *not* to hide
    them, so the count is real and showing them costs nothing, and the marker
    here is what identifies them once shown (and in `--once` output, which
    hides nothing).

    A live import error outranks staleness, but a *stale* DAG's leftover
    `has_import_errors` flag does not: see `Dag.import_error_is_live`. Getting
    that precedence wrong showed 51 stale DAGs as red import errors on a
    deployment with none, hiding the one true signal behind a false alarm.
    """
    if dag.import_error_is_live(live_errors):
        return Text("⚠", style="bold red")
    if dag.is_stale:
        return Text("✂", style="yellow")
    if dag.is_paused:
        return Text("⏸", style="yellow")
    return Text(" ")


def running_cell(count: int) -> Text:
    """The DAGs view's "is it running right now?" column: a live dot, with the
    count when more than one run is in flight at once."""
    if count <= 0:
        return Text("—", style="dim")
    return Text("●" if count == 1 else f"● {count}", style="bold cyan")


def dag_row(
    dag: Dag,
    now: datetime,
    live_errors: frozenset[str] = frozenset(),
    running: int = 0,
) -> tuple[Text, ...]:
    name = Text(_elide(dag.dag_id, _DAG_ID_WIDTH), style="cyan")
    if dag.is_paused:
        name.append("  paused", style="dim yellow")
    if dag.is_stale:
        name.append("  stale", style="dim yellow")
    if dag.import_error_is_live(live_errors):
        name.append("  import error", style="dim red")
    return (
        dag_marker(dag, live_errors),
        name,
        running_cell(running),
        Text(_elide(dag.schedule or "—", 18), style="dim"),
        Text(_elide(", ".join(dag.owners) or "—", 16), style="dim"),
        Text(_elide(", ".join(dag.tags) or "—", 20), style="dim"),
        Text(format_relative(dag.next_dagrun, now) if dag.next_dagrun else "—", style="dim"),
    )


def render_dag(
    dag: Dag, now: datetime, live_errors: frozenset[str] = frozenset()
) -> RenderableType:
    """The selected DAG in the detail pane.

    Same shape as the Run pane: the facts as `key: value` lines, one per line
    (see `render_run` for why a column grid lost this job).
    """
    body = Text()
    body.append(dag.dag_id, style="bold cyan")
    for label, style in (
        ("paused", "yellow") if dag.is_paused else ("", ""),
        ("stale — file no longer in the bundle", "yellow") if dag.is_stale else ("", ""),
        ("import error", "bold red")
        if dag.import_error_is_live(live_errors)
        else ("", ""),
    ):
        if label:
            body.append(f"  [{label}]", style=style)
    if dag.description:
        body.append(f"\n{dag.description}", style="dim")

    stats: list[tuple[str, Text]] = [
        ("schedule", Text(dag.schedule or "—")),
        ("owners", Text(", ".join(dag.owners) or "—")),
        ("tags", Text(", ".join(dag.tags) or "—")),
        ("next run", Text(_stamp(dag.next_dagrun))),
    ]
    grid = key_value_lines(stats)

    return Panel(
        Group(
            body,
            Text(),
            grid,
            Text(),
            Text("p pause/unpause · t trigger · v back to runs", style="dim italic"),
        ),
        title=Text("DAG", style="bold"),
        border_style="red" if dag.needs_attention else "cyan",
        padding=(1, 2),
    )


# --- the summary bar -------------------------------------------------------


def view_tabs(view: str) -> Text:
    """The view switcher: every view's label, with the active one lit up."""
    tabs = Text()
    for index, name in enumerate(VIEWS):
        if index:
            tabs.append(" │ ", style="dim")
        tabs.append(
            VIEW_LABELS[name],
            style="bold reverse cyan" if name == view else "dim",
        )
    return tabs


def shown_of(shown: int, total: int, noun: str) -> Text:
    """"N of M runs" — the phrasing that makes a truncated list impossible to
    mistake for a complete one.

    Airflow caps a page at 100 whatever you asked for, so a bare count is not
    evidence of anything. When we hold everything, the total is dropped as noise.
    """
    if total and total > shown:
        return Text(f"{shown} of {total:,} {noun}", style="bold")
    return Text(f"{shown} {noun}", style="bold")


def render_summary(
    snapshot: Snapshot | None,
    error: str | None,
    *,
    view: str = "runs",
    shown: int | None = None,
    stale_hidden: bool = False,
    state_filter: str | None = None,
    watched_runs: tuple[DagRun, ...] = (),
    watched_total: int = 0,
) -> Text:
    """The one-line status bar docked at the top: which view, which deployment,
    what state its rows are in, and whether any DAG file is failing to parse.

    `shown` is how many rows the list is actually displaying after any `/` filter,
    so the bar never claims more than is on screen. `stale_hidden` marks the
    stale count as hidden rows — the count itself never goes away, because rows
    silently absent is exactly the failure mode this bar exists to prevent.
    `state_filter` marks the `R` narrowing for the same reason: a list showing
    only one state must never read as a deployment with nothing else.

    `watched_runs` are the watched runs currently inside the loaded run window
    and `watched_total` is the whole watch list — the gap between them is a
    watched run the poll no longer holds, which the Watched view cannot show
    and therefore must count out loud.
    """
    bar = view_tabs(view)
    if snapshot is not None:
        bar.append("  ")
        bar.append(snapshot.deployment.label, style="bold cyan")
        bar.append("  ")
        bar.append(f"Airflow {snapshot.deployment.airflow_version}", style="dim")

    if error is not None:
        bar.append(f"   ✖ {error}", style="bold red")
        return bar
    if snapshot is None:
        bar.append("   Contacting Astro…", style="dim italic")
        return bar

    bar.append("  ·  ")
    if view == "dags":
        bar.append_text(
            shown_of(
                len(snapshot.dags) if shown is None else shown,
                snapshot.dags_total,
                "dags",
            )
        )
        bar.append("   ")
        bar.append(
            f"⏸ {snapshot.paused_count} paused",
            style="yellow" if snapshot.paused_count else "dim",
        )
        bar.append("   ")
        stale = f"✂ {snapshot.stale_count} stale"
        if stale_hidden and snapshot.stale_count:
            stale += " hidden · s shows"
        bar.append(stale, style="yellow" if snapshot.stale_count else "dim")
    elif view == "watched":
        failed = sum(1 for run in watched_runs if run.state == "failed")
        running = sum(1 for run in watched_runs if run.state == "running")
        bar.append_text(
            shown_of(
                len(watched_runs) if shown is None else shown,
                watched_total,
                "watched",
            )
        )
        bar.append("   ")
        bar.append(f"✖ {failed} failed", style="bold red" if failed else "dim")
        bar.append("   ")
        bar.append(f"● {running} running", style="bold cyan" if running else "dim")
        outside = watched_total - len(watched_runs)
        if outside > 0:
            # Watched but no longer inside the loaded run window — the one way
            # a row can be absent from this view without being unwatched.
            bar.append("   ")
            bar.append(f"⋯ {outside} outside the loaded runs", style="bold yellow")
    else:
        runs = snapshot.runs
        failed = sum(1 for run in runs if run.state == "failed")
        running = sum(1 for run in runs if run.state == "running")
        queued = sum(1 for run in runs if run.state == "queued")
        bar.append_text(
            shown_of(len(runs) if shown is None else shown, snapshot.runs_total, "runs")
        )
        bar.append("   ")
        bar.append(f"✖ {failed} failed", style="bold red" if failed else "dim")
        bar.append("   ")
        bar.append(f"● {running} running", style="bold cyan" if running else "dim")
        bar.append("   ")
        bar.append(f"◌ {queued} queued", style="yellow" if queued else "dim")
        if snapshot.runs_truncated:
            bar.append("   ")
            bar.append("⋯ run list truncated", style="bold yellow")
    if state_filter is not None:
        glyph, style = state_style(state_filter)
        bar.append("   ")
        bar.append(f"{glyph} {state_filter} only · R cycles", style=style)
    if snapshot.import_errors:
        bar.append("   ")
        bar.append(
            f"⚠ {len(snapshot.import_errors)} import errors", style="bold red"
        )
    if snapshot.dags_truncated:
        bar.append("   ")
        bar.append("⋯ DAG list truncated", style="bold yellow")
    return bar


# --- the detail pane ------------------------------------------------------


def render_run(run: DagRun, dag: Dag | None, now: datetime) -> RenderableType:
    """The selected run at level "runs": what it is and how to drill in.

    The facts read as `key: value` lines rather than a row of columns — a run
    has too many fields for a horizontal grid to survive a narrow pane, and a
    vertical list scans the same way whatever the split is set to.
    """
    body = Text()
    body.append(run.dag_id, style="bold cyan")
    if dag is not None and dag.is_paused:
        body.append("  [paused]", style="yellow")
    body.append(f"\n{run.run_id}\n\n", style="dim")
    body.append_text(state_cell(run.state))
    body.append(f"    {run.run_type or 'unknown'} run", style="dim")

    stats: list[tuple[str, Text]] = [
        ("logical date", Text(_stamp(run.logical_date))),
        ("started", Text(format_relative(run.start_date, now))),
        ("ended", Text(format_relative(run.end_date, now))),
        ("duration", _duration_cell(run.duration, run.start_date, now)),
    ]
    if run.run_after is not None:
        # Airflow 3 only; a run with a null logical date still says when it
        # belongs. Absent on Airflow 2, so the pane is unchanged there.
        stats.insert(1, ("run after", Text(_stamp(run.run_after))))
    if dag is not None:
        stats.append(("owners", Text(", ".join(dag.owners) or "—")))
        stats.append(("next run", Text(_stamp(dag.next_dagrun))))
    grid = key_value_lines(stats)

    hint = Text("enter → task instances", style="dim italic")
    if run.note:
        hint = Text(f"note: {run.note}", style="italic")
    return Panel(
        Group(body, Text(), grid, Text(), hint),
        title=Text("Run", style="bold"),
        border_style="cyan",
        padding=(1, 2),
    )


def key_value_lines(stats: list[tuple[str, Text]]) -> Table:
    """`key: value`, one per line, keys right-aligned so the values line up."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", no_wrap=True)
    grid.add_column()
    for label, value in stats:
        grid.add_row(Text(f"{label}:", style="dim"), value)
    return grid


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else "—"


def render_task(task: TaskInstance, run: DagRun, now: datetime) -> RenderableType:
    """The selected task instance at level "tasks": what it did, and how to
    reach its log."""
    body = Text()
    body.append(task.display_id, style="bold cyan")
    body.append(f"\n{run.dag_id} · {_elide(run.run_id, 48)}\n\n", style="dim")
    body.append_text(state_cell(task.state))
    if task.operator:
        body.append(f"    {task.operator}", style="dim")

    grid = Table.grid(expand=True, padding=(0, 2))
    stats: list[tuple[str, Text]] = [
        ("attempt", Text(f"{task.try_number} of {task.max_tries or task.try_number}")),
        ("started", Text(format_relative(task.start_date, now))),
        ("ended", Text(format_relative(task.end_date, now))),
        ("duration", _duration_cell(task.duration, task.start_date, now)),
        ("pool", Text(task.pool or "—")),
    ]
    for _ in stats:
        grid.add_column(justify="center")
    grid.add_row(*(Text(label.upper(), style="dim") for label, _ in stats))
    grid.add_row(*(value for _, value in stats))

    hint = Text("enter → log · c clear · m mark state", style="dim italic")
    return Panel(
        Group(body, Text(), grid, Text(), hint),
        title=Text("Task instance", style="bold"),
        border_style="red" if task.failed else "cyan",
        padding=(1, 2),
    )


# How many log lines the pane will render at once. The CLI transport cannot
# stream, so a log arrives as one buffered body; rendering an unbounded one would
# stall the UI. Past this the pane states how many lines it is not showing —
# `/` narrows to the ones you want — rather than silently showing part of the log.
MAX_LOG_LINES = 4000


def highlight(text: str, query: str) -> Text:
    """`text` with every occurrence of `query` marked.

    Each space-separated term is highlighted independently, matching how
    `models.matches` decides a line is a hit — so what is highlighted is exactly
    what caused the match.
    """
    marked = Text(text)
    for term in query.casefold().split():
        start = 0
        folded = text.casefold()
        while True:
            found = folded.find(term, start)
            if found < 0:
                break
            marked.stylize("bold black on yellow", found, found + len(term))
            start = found + len(term)
    return marked


# --- clickable links ---------------------------------------------------------
#
# A URL in a log is styled two ways at once, because two different things can
# handle the click. `link` is the OSC 8 terminal hyperlink, which a terminal
# that speaks it opens itself (⌘-click in iTerm2, Ghostty, WezTerm). The
# `@click` meta is Textual's own: clicking a span carrying one runs the action
# named in it, which is the only path that works while the app holds the mouse.
#
# LINK_ACTION is the one place this module names the app it renders into —
# `AirflowWatchApp.action_open_link`, which a test asserts still exists. `repr`
# quotes the URL so a query string full of `&`, `=` and `#` survives Textual's
# action parse.
LINK_ACTION = "app.open_link"


def link_style(url: str) -> Style:
    """The style that makes a span of text a clickable link to `url`."""
    return Style(link=url, underline=True) + Style.from_meta(
        {"@click": f"{LINK_ACTION}({url!r})"}
    )


def linkify(text: Text, line: str) -> Text:
    """`text` with every URL in `line` made clickable, in place.

    Takes the source line as well as the `Text` because the spans are offsets
    into the original — the caller may already have styled it (a search
    highlight, say), and the two stack rather than replace each other.
    """
    for start, end, url in find_urls(line):
        text.stylize(link_style(url), start, end)
    return text


def log_line(line: str, query: str) -> Text:
    """One rendered log line: search hits marked, URLs clickable."""
    return linkify(highlight(line, query) if query.strip() else Text(line), line)


def databricks_banner(log: TaskLog | None) -> Text | None:
    """The "this task ran in Databricks" line, or None if it did not say so.

    The run page is logged once, in the middle of a log thousands of lines
    long; hoisting it to the top of the pane is the difference between a link
    you can use and one you have to go looking for.
    """
    url = databricks_run_url(log.content) if log is not None else None
    if url is None:
        return None
    line = Text("↗ ", style="bold magenta")
    line.append(
        "Databricks run", style=link_style(url) + Style(color="magenta", bold=True)
    )
    line.append("   click, or o to open", style="dim italic")
    return line


def render_log(
    task: TaskInstance,
    log: TaskLog | None,
    *,
    loading: bool = False,
    query: str = "",
) -> RenderableType:
    """One attempt's log, with the try selector and any `/` filter applied.

    A filter here shows only matching lines, keeping each line's *original* number
    so a filtered view still tells you where in the log you are, and highlighting
    what matched.

    Any URL in the log is a link you can click; a Databricks run page is also
    hoisted to a line above the log, since that is where the task's real work
    happened and the log names it exactly once.
    """
    tries = task.tries
    selector = Text()
    for number in tries:
        if selector.plain:
            selector.append(" ", style="dim")
        active = log is not None and number == log.try_number
        selector.append(
            f" {number} ", style="bold reverse cyan" if active else "dim"
        )
    selector.append("   < > to change attempt", style="dim italic")

    if loading:
        body: RenderableType = Align.center(
            Text("Fetching log…", style="dim italic")
        )
    elif log is None:
        body = Align.center(Text("No log loaded.", style="dim"))
    elif not log.content.strip():
        body = Align.center(
            Text("Airflow returned an empty log for this attempt.", style="dim")
        )
    else:
        hits, total = filter_log(log.content, query)
        if query.strip() and not hits:
            body = Align.center(
                Text(f"No log line matches {query!r} ({total} lines).", style="dim")
            )
        else:
            body = _log_body(hits, total, query, log)
        if query.strip():
            selector = Text(f"{len(hits)} of {total} lines match ", style="bold yellow")
            selector.append(f"{query!r}", style="yellow")
            selector.append("   esc clears", style="dim italic")

    banner = databricks_banner(log)
    if banner is not None:
        body = Group(banner, Text(), body)

    return Panel(
        body,
        title=Text(f"Log · {task.display_id} · attempt {log.try_number if log else '?'}", style="bold"),
        subtitle=selector,
        border_style="red" if task.failed else "cyan",
        padding=(0, 1),
    )


def _log_body(
    hits: list[tuple[int, str]], total: int, query: str, log: TaskLog
) -> RenderableType:
    """The log lines themselves, numbered, bounded, and honest about it."""
    shown = hits[:MAX_LOG_LINES]
    table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    table.add_column(justify="right", style="dim", no_wrap=True)  # line number
    table.add_column(ratio=1, overflow="fold")
    for number, line in shown:
        table.add_row(str(number), log_line(line, query))
    parts: list[RenderableType] = [table]
    if len(hits) > len(shown):
        parts.append(
            Text(
                f"… {len(hits) - len(shown)} more lines not shown "
                f"({total} in this attempt).",
                style="bold yellow",
            )
        )
    if log.truncated:
        # The *fetch* stopped short, not just the render: say so, or the log
        # looks like it ends mid-sentence for no reason.
        parts.append(
            Text(
                "… log too large to hold in full — this is the first "
                f"{total:,} lines of attempt {log.try_number}.",
                style="bold yellow",
            )
        )
    elif len(hits) <= len(shown) and not query.strip():
        parts.append(
            Text(
                f"end of attempt {log.try_number} ({total} lines)", style="dim italic"
            )
        )
    return Group(*parts)


def render_import_errors(errors: tuple[ImportErrorEntry, ...]) -> RenderableType:
    """The import-errors overlay: which DAG files are failing to parse, and why.

    An import error means a DAG is silently absent, which is the failure mode
    most easily mistaken for "nothing scheduled".
    """
    if not errors:
        return Panel(
            Align.center(Text("No DAG import errors 🎉", style="green")),
            title=Text("Import errors", style="bold"),
            subtitle=Text("e / esc to close", style="dim"),
            border_style="green",
            padding=(1, 2),
        )
    table = Table(
        show_header=False, box=None, padding=(0, 1), expand=True, show_lines=True
    )
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(ratio=1)
    for entry in errors:
        trace = " ".join(entry.stacktrace.split())
        table.add_row(entry.short_filename, Text(_elide(trace, 600), style="red"))
    return Panel(
        table,
        title=Text(f"Import errors ({len(errors)})", style="bold"),
        subtitle=Text("e / esc to close", style="dim"),
        border_style="red",
        padding=(1, 2),
    )


def render_detail_placeholder(
    snapshot: Snapshot | None,
    error: str | None,
    *,
    loading: bool = False,
    view: str = "runs",
    shown: int | None = None,
    query: str = "",
) -> RenderableType:
    """What the detail pane shows when there is no selected row to render.

    `shown` is how many rows survived the `/` filter. It matters: a list emptied
    by a filter and a deployment with nothing in it look identical from here
    otherwise, and telling the user "nothing matches" is the useful message.
    `query` disambiguates the same two cases in the Watched view, where an
    empty unfiltered list means "nothing marked yet" and deserves to say how
    to mark something.
    """
    noun = "watched runs" if view == "watched" else ("DAGs" if view == "dags" else "DAG runs")
    loaded = () if snapshot is None else (snapshot.dags if view == "dags" else snapshot.runs)
    rows: tuple[object, ...] | range = loaded if shown is None else range(shown)
    if loading:
        message = Text("Contacting Astro…", style="dim italic")
    elif error is not None:
        message = Text(error, style="red")
    elif not rows and view == "watched" and not query.strip():
        message = Text(
            "Nothing watched — w on a run marks it, W clears the list.",
            style="dim",
        )
    elif not rows:
        message = Text(f"No {noun} match the current filter.", style="dim")
    else:
        message = Text(f"Select {'a DAG' if view == 'dags' else 'a run'} on the left.", style="dim")
    return Panel(
        Align.center(message),
        title="airflow-watch",
        border_style="cyan",
        padding=(1, 2),
    )


def render_drill_error(message: str) -> RenderableType:
    """A drill-down that failed. Distinct from a poll failure: the list is
    still good, only this one fetch is not."""
    return Panel(
        Text(message, style="red"),
        title=Text("Could not load", style="bold"),
        border_style="red",
        padding=(1, 2),
    )


def render_loading(what: str) -> RenderableType:
    """The visible loading state every drill-down needs.

    Each level costs a process spawn (~0.75s), so a pane that changed instantly
    to blank would read as broken. Naming what is being fetched is the whole
    point.
    """
    return Panel(
        Align.center(Text(f"Fetching {what}…", style="dim italic")),
        title="airflow-watch",
        border_style="cyan",
        padding=(1, 2),
    )


def render_detail(
    drill: Drill,
    snapshot: Snapshot | None,
    run: DagRun | None,
    task: TaskInstance | None,
    now: datetime,
    *,
    error: str | None = None,
    view: str = "runs",
    dag: Dag | None = None,
    query: str = "",
    shown: int | None = None,
) -> RenderableType:
    """The detail pane for whatever view and drill level is active.

    One pure function so the view/level→pane mapping is testable without the app.

    `drill.error` is a failed *drill-down* — the list is still good, only this
    one fetch is not. `error` is a failed *poll*, which only reaches the pane
    when there is no last-good row to show instead; otherwise the summary bar
    carries it and the stale row stays readable.
    """
    if drill.error is not None:
        return render_drill_error(drill.error)
    if drill.level == "log":
        if drill.task is None:
            return render_loading("log")
        return render_log(drill.task, drill.log, loading=drill.loading, query=query)
    if drill.level == "tasks":
        if drill.loading:
            return render_loading("task instances")
        if run is None:
            return render_detail_placeholder(snapshot, error)
        if task is None:
            message = (
                f"No task instance matches {query!r}."
                if query.strip()
                else "This run has no task instances."
            )
            return Panel(
                Align.center(Text(message, style="dim")),
                title=Text("Task instances", style="bold"),
                border_style="cyan",
                padding=(1, 2),
            )
        return render_task(task, run, now)
    if view == "dags":
        if dag is None:
            return render_detail_placeholder(
                snapshot,
                error,
                loading=snapshot is None and error is None,
                view=view,
                shown=shown,
            )
        return render_dag(
            dag,
            now,
            live_import_error_files(snapshot.import_errors if snapshot else ()),
        )
    if run is None:
        return render_detail_placeholder(
            snapshot,
            error,
            loading=snapshot is None and error is None,
            view=view,
            shown=shown,
            query=query,
        )
    known = snapshot.dag(run.dag_id) if snapshot is not None else None
    return render_run(run, known, now)


# --- the chart strip ---------------------------------------------------------
#
# Two charts stacked under the detail pane (`g` toggles the strip): an
# in-flight chart counting how many runs (or tasks) were going at once, above a
# stacked-bar timeline of activity — starts bucketed over time, one bucket per
# column, coloured by state. Bucketing, stacking, and overlap counting are pure
# functions asserted on directly in tests; only the bucket count depends on the
# pane's width, which is why each body is a renderable (resolved at render
# time) rather than a function of the data alone.

# chart group -> style, in stacking order: the bottom of the bar first. Failure
# sits at the bottom so a tall stack of successes can never hide it.
CHART_GROUPS: tuple[tuple[str, str], ...] = (
    ("failed", "red"),
    ("running", "cyan"),
    ("queued", "yellow"),
    ("success", "green"),
    ("other", "magenta"),
)

# state -> chart group. Coarser than `_STATE_STYLE` on purpose: a one-character
# column can carry four or five colours legibly, not thirteen.
_CHART_GROUP_OF = {
    "failed": "failed",
    "upstream_failed": "failed",
    "running": "running",
    "restarting": "running",
    "queued": "queued",
    "scheduled": "queued",
    "up_for_retry": "queued",
    "up_for_reschedule": "queued",
    "deferred": "queued",
    "success": "success",
}

# When the point happened (None for one that cannot be dated), and its state.
ChartPoint = tuple[datetime | None, str]

# The chart body's fixed geometry: how many rows of bars, and the y-axis gutter
# ("  12┤") to the left of them.
CHART_BAR_ROWS = 5
_CHART_GUTTER = 5


def chart_group(state: str) -> str:
    """Which chart colour a state contributes to. Total, like `state_style`:
    an unknown state lands in "other" rather than raising."""
    return _CHART_GROUP_OF.get((state or "none").strip().lower(), "other")


def chart_counts(
    points: Sequence[ChartPoint], now: datetime, buckets: int
) -> list[dict[str, int]]:
    """Group counts per bucket, over `buckets` equal slices of the time window.

    The window runs from the oldest dated point to `now` (or to the newest
    point, should one somehow sit in the future). Undated points are skipped —
    there is nowhere on a time axis to put them; `render_chart` reports them.
    """
    counts: list[dict[str, int]] = [{} for _ in range(max(1, buckets))]
    timed = [(when, state) for when, state in points if when is not None]
    if not timed:
        return counts
    start = min(when for when, _ in timed)
    end = max(now, max(when for when, _ in timed))
    span = max((end - start).total_seconds(), 1.0)
    for when, state in timed:
        index = int((when - start).total_seconds() / span * len(counts))
        index = min(max(index, 0), len(counts) - 1)
        group = chart_group(state)
        counts[index][group] = counts[index].get(group, 0) + 1
    return counts


def stack_cells(counts: dict[str, int], height: int) -> tuple[str, ...]:
    """One bucket's bar: `height` cells bottom-up, each named for its group.

    Proportional, with one guarantee worth its own code: every non-empty group
    gets at least one cell, granted in `CHART_GROUPS` order. A bucket of 19
    successes and 1 failure must still show red — rounding away the failure
    would hide exactly what a monitoring chart exists to show.
    """
    total = sum(counts.values())
    if total <= 0 or height <= 0:
        return ()
    present = [group for group, _ in CHART_GROUPS if counts.get(group, 0) > 0]
    cells = dict.fromkeys(present[:height], 1)  # the visibility guarantee
    remaining = height - len(cells)
    if remaining > 0:
        # The rest goes out proportionally, largest fractional part first;
        # `sorted` is stable, so ties keep `CHART_GROUPS` order.
        quotas = {group: counts[group] / total * remaining for group in present}
        for group in present:
            cells[group] += int(quotas[group])
            remaining -= int(quotas[group])
        by_remainder = sorted(
            present, key=lambda group: quotas[group] % 1, reverse=True
        )
        for group in by_remainder[:remaining]:
            cells[group] += 1
    stacked: list[str] = []
    for group, _ in CHART_GROUPS:
        stacked.extend([group] * cells.get(group, 0))
    return tuple(stacked)


@dataclass(frozen=True)
class ChartBody:
    """The bars, the y-axis peak label, and the time axis.

    `points` must already be dated (no None timestamps) and non-empty —
    `render_chart` owns the empty state. Width-adaptive: the console's width at
    render time decides the bucket count, so resizing the pane rescales the
    chart instead of clipping it.
    """

    points: tuple[ChartPoint, ...]
    now: datetime

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        buckets = options.max_width - _CHART_GUTTER
        if buckets < 4:
            return
        counts = chart_counts(self.points, self.now, buckets)
        totals = [sum(bucket.values()) for bucket in counts]
        peak = max(totals)
        styles = dict(CHART_GROUPS)
        columns = [
            stack_cells(bucket, round(total / peak * CHART_BAR_ROWS) or 1)
            if total
            else ()
            for bucket, total in zip(counts, totals)
        ]
        for row in range(CHART_BAR_ROWS - 1, -1, -1):
            top = row == CHART_BAR_ROWS - 1
            line = Text(f"{peak:>4}┤" if top else "    │", style="dim")
            for cells in columns:
                if len(cells) > row:
                    line.append("█", style=styles[cells[row]])
                else:
                    line.append(" ")
            yield line
        start = min(when for when, _ in self.points if when is not None)
        yield from _time_axis(start, self.now, buckets)


def _time_axis(start: datetime, now: datetime, buckets: int) -> RenderResult:
    """The x axis both chart bodies share: the rule, then "<oldest> … now"."""
    yield Text("    └" + "─" * buckets, style="dim")
    left = format_relative(start, now)
    pad = buckets - len(left) - len("now")
    labels = f"     {left}{' ' * pad}now" if pad >= 1 else f"     {left}"
    yield Text(labels, style="dim")


def render_chart(
    drill: Drill, runs: tuple[DagRun, ...], now: datetime
) -> RenderableType:
    """The chart pane under the detail pane: activity over time.

    Follows the drill the way the detail pane does — the recent runs at the top
    level, the drilled-into run's task instances inside one. Undated points
    cannot sit on a time axis, so when nothing can be placed the pane says so
    rather than drawing an empty axis.
    """
    if drill.level in ("tasks", "log") and drill.run is not None:
        title = f"Tasks over time · {drill.run.dag_id}"
        points = tuple((task.start_date, task.state) for task in drill.tasks)
        empty = "No task instance has started yet."
    else:
        title = "Runs over time"
        points = tuple((run.happened_at, run.state) for run in runs)
        empty = "No dated runs to graph."
    timed = tuple((when, state) for when, state in points if when is not None)
    if not timed:
        body: RenderableType = Align.center(Text(empty, style="dim"))
        legend = Text()
    else:
        body = ChartBody(points=timed, now=now)
        legend = Text()
        for group, style in CHART_GROUPS:
            if any(chart_group(state) == group for _, state in timed):
                if legend.plain:
                    legend.append("  ")
                legend.append(f"█ {group}", style=style)
    return Panel(
        body,
        title=Text(title, style="bold"),
        subtitle=legend or None,
        border_style="cyan",
        padding=(0, 1),
    )


# When a run or task started (None if it never did) and when it ended (None
# while it is still going).
ChartSpan = tuple[datetime | None, datetime | None]


def in_flight_counts(
    spans: Sequence[ChartSpan], now: datetime, buckets: int
) -> list[int]:
    """How many spans were in flight during each of `buckets` equal slices.

    The window matches `chart_counts`: oldest start to `now` (or to the latest
    end, should one somehow sit in the future). A span with no end is still
    going, so it runs to `now` — the same reading `_duration_cell` gives a
    record with a start and no end. A span that never started is skipped: it
    was never in flight.
    """
    counts = [0] * max(1, buckets)
    started = [(start, end) for start, end in spans if start is not None]
    if not started:
        return counts
    window_start = min(start for start, _ in started)
    window_end = max(now, max(end or now for _, end in started))
    window = max((window_end - window_start).total_seconds(), 1.0)
    for start, end in started:
        lo = int((start - window_start).total_seconds() / window * len(counts))
        hi = int(((end or now) - window_start).total_seconds() / window * len(counts))
        first = min(max(lo, 0), len(counts) - 1)
        last = min(max(hi, first), len(counts) - 1)
        for index in range(first, last + 1):
            counts[index] += 1
    return counts


@dataclass(frozen=True)
class InFlightBody:
    """The concurrency bars, the y-axis peak label, and the time axis.

    One colour, cyan — the in-flight colour everywhere else — scaled to the
    peak, with any non-zero bucket getting at least one cell. `spans` must
    already have starts (no Nones) and be non-empty —
    `render_in_flight_chart` owns the empty state. Width-adaptive the way
    `ChartBody` is, and for the same reason.
    """

    spans: tuple[tuple[datetime, datetime | None], ...]
    now: datetime

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        buckets = options.max_width - _CHART_GUTTER
        if buckets < 4:
            return
        counts = in_flight_counts(self.spans, self.now, buckets)
        peak = max(counts)
        heights = [
            (round(count / peak * CHART_BAR_ROWS) or 1) if count else 0
            for count in counts
        ]
        for row in range(CHART_BAR_ROWS - 1, -1, -1):
            top = row == CHART_BAR_ROWS - 1
            line = Text(f"{peak:>4}┤" if top else "    │", style="dim")
            for height in heights:
                if height > row:
                    line.append("█", style="cyan")
                else:
                    line.append(" ")
            yield line
        yield from _time_axis(
            min(start for start, _ in self.spans), self.now, buckets
        )


def render_in_flight_chart(
    drill: Drill, runs: tuple[DagRun, ...], now: datetime
) -> RenderableType:
    """The companion chart: how many were running at once, over time.

    The activity chart answers "what happened and when"; this one answers "how
    much was going on at once" — the concurrency the deployment was sustaining.
    Each run (or, drilled in, task instance) occupies its start→end interval,
    to `now` while it is still going, and every bucket counts the intervals
    crossing it. Follows the drill the way the activity chart does.
    """
    if drill.level in ("tasks", "log") and drill.run is not None:
        title = f"Tasks in flight · {drill.run.dag_id}"
        spans: tuple[ChartSpan, ...] = tuple(
            (task.start_date, task.end_date) for task in drill.tasks
        )
        empty = "No task instance has started yet."
    else:
        title = "Runs in flight"
        spans = tuple((run.start_date, run.end_date) for run in runs)
        empty = "No run has started yet."
    started = tuple(
        (start, end) for start, end in spans if start is not None
    )
    if not started:
        body: RenderableType = Align.center(Text(empty, style="dim"))
        subtitle = None
    else:
        body = InFlightBody(spans=started, now=now)
        subtitle = Text("█ in flight at once", style="cyan")
    return Panel(
        body,
        title=Text(title, style="bold"),
        subtitle=subtitle,
        border_style="cyan",
        padding=(0, 1),
    )


def render_filter_prompt(target: str, query: str) -> RenderableType:
    """The `/` bar: what is being filtered, and what has been typed so far.

    Client-side over the rows already loaded, so it costs no API call and updates
    on every keystroke.
    """
    body = Text()
    body.append("/", style="bold cyan")
    body.append(query or " ", style="bold")
    body.append(f"    filtering {target}", style="dim")
    body.append("    enter keeps · esc clears", style="dim italic")
    return body


def render_loading_older(held: int, total: int) -> Text:
    """The bottom bar while a scroll-triggered run extension is in flight.

    The refresh countdown gives up its row for the duration: "the list is about
    to grow" is the one thing the user just asked for by scrolling, and without
    a notice the extra second or two of fetching reads as nothing happening.
    """
    notice = Text(justify="center")
    notice.append("⇣ loading older runs… ", style="bold cyan")
    notice.append(f"({held} of {total:,} loaded)", style="dim")
    return notice


# --- overlays --------------------------------------------------------------


def render_deployments(
    deployments: tuple[Deployment, ...], selected: str
) -> RenderableType:
    """The deployment switcher. Unsupported and hibernating deployments are
    listed with their reason rather than hidden, so "where is my deployment?"
    always has an answer on screen."""
    if not deployments:
        body: RenderableType = Text("No deployments visible.", style="dim italic")
    else:
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column(no_wrap=True)  # cursor
        table.add_column(ratio=1)  # name
        table.add_column(no_wrap=True)  # version / note
        for index, deployment in enumerate(deployments, start=1):
            active = deployment.key == selected
            note, style = _deployment_note(deployment)
            table.add_row(
                Text(f"{index}", style="bold cyan" if active else "dim"),
                Text(
                    deployment.label,
                    style="bold reverse cyan" if active else "",
                ),
                Text(note, style=style),
            )
        body = table
    return Panel(
        body,
        title=Text("Deployments", style="bold"),
        subtitle=Text("1-9 to switch · D / esc to close", style="dim"),
        border_style="cyan",
        padding=(1, 2),
    )


def _deployment_note(deployment: Deployment) -> tuple[str, str]:
    """The right-hand annotation in the switcher: why you can or cannot use it.

    Asks the version seam (`api.supports`) rather than testing a version number
    itself — no module outside `api.py` may contain a version conditional, and
    which majors are supported is exactly the thing that moves.
    """
    if deployment.is_hibernating:
        return "hibernating", "yellow"
    if not api.supports(deployment.airflow_version):
        return f"Airflow {deployment.airflow_version} — unsupported", "red"
    return f"Airflow {deployment.airflow_version}  {deployment.status.lower()}", "dim"


def render_confirm(action: Action) -> RenderableType:
    """The confirmation modal's body: exactly what is about to happen.

    Every mutating action passes through here first, naming its target, so
    nothing can fire on a single keystroke.
    """
    body = Text()
    body.append(action.title, style="bold")
    body.append("\n\n")
    body.append("target  ", style="dim")
    body.append(action.target, style="bold cyan")
    body.append("\n\n")
    if action.dry_run:
        body.append(
            "Dry run: Airflow will report what this would affect and change "
            "nothing.",
            style="green",
        )
    else:
        body.append("This changes state in Airflow.", style="bold yellow")
    return Panel(
        Group(body, Text(), Text("y / enter confirm · n / esc cancel", style="dim")),
        title=Text("Confirm", style="bold"),
        border_style="yellow" if action.mutates else "green",
        padding=(1, 2),
    )


# --- the menu bar ------------------------------------------------------------


@dataclass(frozen=True)
class MenuEntry:
    """One command row in a menu drop-down: the direct key, what it does, and
    the app action (`action_<name>`) that selecting it runs."""

    key: str
    label: str
    action: str


def menu_option(entry: MenuEntry) -> Text:
    """One menu row: the key in the gutter, then the label."""
    text = Text()
    text.append(f"{entry.key:>5}", style="bold cyan")
    text.append("  ")
    text.append(entry.label)
    return text


@dataclass(frozen=True)
class MenuCategory:
    """One drop-down of the menu bar: a title and the commands under it."""

    title: str
    entries: tuple[MenuEntry, ...]


def _state_filter_menu_label(state_filter: str | None) -> str:
    """The `R` entry's label: what pressing it moves the state filter *to*,
    like every other toggle in the bar — the menu must never point the wrong
    direction."""
    index = STATE_FILTERS.index(state_filter) if state_filter in STATE_FILTERS else 0
    following = STATE_FILTERS[(index + 1) % len(STATE_FILTERS)]
    if following is None:
        return "Show all runs and DAGs"
    if state_filter is None:
        return f"Show only {following} runs / DAGs"
    return f"State filter: {state_filter} → {following}"


def menu_categories(
    *,
    chart_shown: bool = True,
    stale_shown: bool = False,
    state_filter: str | None = None,
) -> tuple[MenuCategory, ...]:
    """Every command, organized for the menu bar's drop-downs.

    The bar is the complete map: every command appears, grouped by what it
    acts on, so it is also where a new user discovers what exists — the footer
    once tried to advertise every key and ran out of room. Selecting a command
    that does not apply right now is the same no-op its key would be; the
    toggles still label themselves by state so the menu never points the wrong
    direction, and every entry names its direct key so the menu keeps teaching
    the shortcuts.
    """
    return (
        MenuCategory(
            "App",
            (
                MenuEntry("r", "Refresh now", "poll_now"),
                MenuEntry("D", "Switch deployment", "switch_deployment"),
                MenuEntry("l", "Activity log", "toggle_log"),
                MenuEntry("e", "DAG import errors", "show_import_errors"),
                MenuEntry("?", "Help", "help"),
                MenuEntry("q", "Quit", "quit"),
            ),
        ),
        MenuCategory(
            "Runs",
            (
                MenuEntry("enter", "Drill into the selected run's tasks", "drill_in"),
                MenuEntry("w", "Watch / unwatch the selected run", "toggle_watch"),
                MenuEntry("W", "Clear the watched runs", "clear_watched"),
                MenuEntry("t", "Trigger a run of the selected DAG", "trigger_run"),
                MenuEntry("p", "Pause / unpause the selected DAG", "toggle_pause"),
                MenuEntry("i", "Hand the selected run to gw for a summary", "investigate"),
            ),
        ),
        MenuCategory(
            "Tasks",
            (
                MenuEntry("enter", "Open the selected task's log", "drill_in"),
                MenuEntry("c", "Clear (retry) the selected task", "clear_tasks"),
                MenuEntry("m", "Mark the selected task success / failed", "mark_tasks"),
                MenuEntry(
                    "o", "Open the log's Databricks run in a browser", "open_databricks"
                ),
                MenuEntry("<", "Previous log attempt", "prev_try"),
                MenuEntry(">", "Next log attempt", "next_try"),
                MenuEntry("esc", "Back out one level", "escape"),
            ),
        ),
        MenuCategory(
            "View",
            (
                MenuEntry("v", "Switch view: DAG runs → DAGs → Watched", "switch_view"),
                MenuEntry("/", "Filter the list on screen", "start_filter"),
                MenuEntry(
                    "R",
                    _state_filter_menu_label(state_filter),
                    "cycle_state_filter",
                ),
                MenuEntry(
                    "s",
                    "Hide stale DAGs" if stale_shown else "Show stale DAGs",
                    "toggle_stale",
                ),
                MenuEntry("d", "Move / hide the detail pane", "cycle_detail"),
                MenuEntry(
                    "g",
                    "Hide the charts" if chart_shown else "Show the charts",
                    "toggle_chart",
                ),
                MenuEntry("[", "Shrink the list window", "shrink_list"),
                MenuEntry("]", "Grow the list window", "grow_list"),
            ),
        ),
    )


# The activity log's per-level glyph and style, keyed by LogEntry.level.
_LOG_LEVELS = {
    "info": ("·", "dim"),
    "warn": ("▲", "yellow"),
    "error": ("✖", "bold red"),
    "action": ("⚡", "bold magenta"),
}


def render_activity_log(entries: list[LogEntry]) -> RenderableType:
    """The `l` overlay: every poll and every confirmed action, newest first.

    This is how a user answers "did my retry actually fire?" — which matters
    more in a tool that can change state than in a read-only one, so actions get
    their own level and glyph.
    """
    if not entries:
        body: RenderableType = Text(
            "No activity yet — background polls will appear here.",
            style="dim italic",
        )
    else:
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column(justify="right", style="dim", no_wrap=True)  # time
        table.add_column(no_wrap=True)  # level glyph
        table.add_column(ratio=1)  # message
        for entry in reversed(entries):  # newest first
            glyph, style = _LOG_LEVELS.get(entry.level, ("·", "dim"))
            table.add_row(
                entry.time.strftime("%H:%M:%S"),
                Text(glyph, style=style),
                Text(entry.message, style="" if entry.level == "info" else style),
            )
        body = table
    return Panel(
        body,
        title=Text("Activity log", style="bold"),
        subtitle=Text("l / esc to close", style="dim"),
        border_style="cyan",
        padding=(1, 2),
    )


HELP_KEYS: tuple[tuple[str, str], ...] = (
    ("↑ / ↓", "Move through the list — the bottom of the runs list loads older runs"),
    ("M", "Open the menu bar — every command by category, ← → between them"),
    ("v", "Switch view: DAG runs → DAGs → Watched"),
    ("/", "Filter the list (or the log) — type to narrow, esc clears"),
    ("enter", "Drill in: run → task instances → log"),
    ("escape", "Back out one level, or clear the filter"),
    ("< / >", "Previous / next log attempt"),
    ("o", "Open the Databricks run the log points at — links are clickable too"),
    ("D", "Switch deployment"),
    ("e", "Show / hide DAG import errors"),
    ("s", "Show / hide stale DAGs (hidden by default)"),
    ("R", "Cycle the state filter: running → failed → queued → success → all"),
    ("w", "Watch / unwatch the selected run — the Watched view shows them"),
    ("W", "Clear the watched runs"),
    ("p", "Pause or unpause the selected DAG"),
    ("t", "Trigger a new run of the selected DAG"),
    ("c", "Clear (retry) the selected task instance"),
    ("m", "Mark the selected task instance success / failed"),
    ("i", "Hand the run to gw: gather metadata + task logs, summarize, wait"),
    ("d", "Cycle the detail pane: right → below → hidden"),
    ("g", "Show / hide the charts under the detail pane"),
    ("[ / ]", "Resize the windows: shrink / grow the list"),
    ("l", "Show / hide the activity log"),
    ("r", "Refresh now"),
    ("?", "Show / hide this help"),
    ("q", "Quit"),
)

HELP_NOTE = (
    "Task instances are listed in dependency order — an upstream task above the "
    "tasks it feeds. Paused DAGs are labelled, never hidden; stale DAGs are "
    "hidden by default but always counted in the summary bar, and `s` shows "
    "them. A list that could not be fetched in full says 'N of M'. Every "
    "action that changes Airflow asks first, offers a dry run, and is recorded "
    "in the activity log. Any URL a log prints is clickable, and a Databricks "
    "run page is hoisted to a line above the log so `o` can open it without "
    "hunting. The menu bar at the top opens with a click or `M` "
    "and lists every command by category; `w` marks runs to follow in the Watched view "
    "for this session. Layout and the selected deployment are restored on "
    "the next launch."
)


def render_help() -> RenderableType:
    """The keybinding reference shown by the `?` overlay."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(justify="right", style="bold cyan", no_wrap=True)
    table.add_column()
    for key, description in HELP_KEYS:
        table.add_row(key, description)
    return Panel(
        Group(table, Text(), Text(HELP_NOTE, style="dim italic")),
        title=Text("Help", style="bold"),
        subtitle=Text("esc to close", style="dim"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_once(
    snapshot: Snapshot, now: datetime, view: str = "runs"
) -> RenderableType:
    """A single-shot snapshot of the runs (or DAGs) list for `--once` / scripting."""
    table = Table(expand=True, header_style="bold", border_style="dim", padding=(0, 1))
    columns = dag_columns() if view == "dags" else list_columns()
    for column in columns:
        table.add_column(column, no_wrap=True)
    if view == "dags":
        live_errors = live_import_error_files(snapshot.import_errors)
        running = snapshot.running_counts()
        for dag in snapshot.dags:
            table.add_row(
                *dag_row(dag, now, live_errors, running=running.get(dag.dag_id, 0))
            )
    else:
        for run in snapshot.runs:
            table.add_row(*list_row(run, now))
    parts: list[RenderableType] = [render_summary(snapshot, None, view=view), table]
    if snapshot.import_errors:
        parts.append(render_import_errors(snapshot.import_errors))
    parts.append(
        Text(
            f"{snapshot.calls} astro calls in {snapshot.elapsed:.2f}s",
            style="dim italic",
        )
    )
    return Group(*parts)
