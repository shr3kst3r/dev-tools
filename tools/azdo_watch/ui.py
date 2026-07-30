"""Rich rendering for azdo-watch.

Everything here is a pure function of the data plus a clock value, so every layout
can be snapshotted in `--once` mode and asserted on in tests. The live app (see
app.py) owns the polling and the windows; nothing in this module knows about I/O
or Textual.

The one rule worth stating: `state_style` is the *only* place a state string turns
into a colour, and it never rejects one. Azure DevOps invented
`succeededWithIssues` and `postponed` after the fact and will invent more; an
unrecognized state renders in a neutral bucket, exactly as in airflow-watch.
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

from .models import (
    Action,
    Issue,
    LogEntry,
    Pipeline,
    Project,
    Record,
    Run,
    RunLog,
    Drill,
    RecordRow,
    Snapshot,
    collect_issues,
    filter_log,
    find_urls,
)

# state -> (glyph, rich style). Known values only; `state_style` falls back for
# anything else rather than raising, which is what keeps a future Azure DevOps
# state from taking the dashboard down.
#
# `partiallySucceeded` gets amber rather than green or red on purpose: it is
# azdo's "some jobs failed but the run is not a failure", which is precisely the
# outcome a green tick would hide and a red cross would overstate.
_STATE_STYLE: dict[str, tuple[str, str]] = {
    # run + record states that mean the same thing in both
    "succeeded": ("✔", "bold green"),
    "failed": ("✖", "bold red"),
    "running": ("●", "bold cyan"),
    "queued": ("◌", "yellow"),
    "canceled": ("⊘", "dim"),
    "partiallysucceeded": ("◐", "bold yellow"),
    # run-only states
    "cancelling": ("⊘", "yellow"),
    "completing": ("●", "cyan"),
    "postponed": ("⏸", "magenta"),
    # record-only states
    "succeededwithissues": ("⚠", "bold yellow"),
    "skipped": ("○", "dim"),
    "abandoned": ("✂", "red"),
    "pending": ("◌", "yellow"),
    "none": ("—", "dim"),
}

# What an unrecognized state renders as. Magenta and question-marked so it reads
# as "the tool doesn't know this one" rather than as a normal state — visible, but
# never fatal.
FALLBACK_STATE_STYLE = ("?", "magenta")

_RUN_COLUMNS = ("", "Pipeline", "Run", "State", "Trigger", "Branch", "Started", "Duration")
# The record pane is a tree, so it leads with a position number: the indentation
# shows *what* contains what, the number gives you something to say out loud
# ("row 7 is the one that failed").
_RECORD_COLUMNS = ("#", "", "Step", "State", "Type", "Agent", "Started", "Duration")
_PIPELINE_COLUMNS = ("", "Pipeline", "Live", "Last run", "State", "When", "Folder")

_PIPELINE_NAME_WIDTH = 30
_RUN_TITLE_WIDTH = 30
_BRANCH_WIDTH = 22

# The views the master list can show, in the order `v` cycles through. The Watched
# view is the runs list narrowed to the runs `w` has marked — same columns, same
# drill-down, its own cursor and filter.
VIEWS = ("runs", "pipelines", "watched")
VIEW_LABELS = {"runs": "Runs", "pipelines": "Pipelines", "watched": "Watched"}

# The views whose rows are runs. Everything that renders or selects a run asks
# this rather than testing for "runs", so the Watched view inherits the whole run
# machinery instead of re-implementing it.
RUN_VIEWS = ("runs", "watched")

# What `R` cycles the run-state filter through, None meaning "all". Running comes
# first — one press answers "what is in flight?" — then the rest in attention
# order.
STATE_FILTERS: tuple[str | None, ...] = (
    None,
    "running",
    "failed",
    "partiallySucceeded",
    "queued",
    "succeeded",
)


def state_style(value: str) -> tuple[str, str]:
    """(glyph, style) for a run or record state.

    Never raises and never rejects: an unknown state — including one no release of
    Azure DevOps has shipped yet — gets the neutral fallback bucket. Keyed
    case-insensitively because azdo's own vocabulary is camelCase
    (`partiallySucceeded`) while everything derived is not.
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


def run_columns() -> tuple[str, ...]:
    return _RUN_COLUMNS


def attention_cell(run: Run, watched: bool = False) -> Text:
    """Red dot: failed. Amber: partly failed. Cyan: in flight. Blank: fine.

    A watched run additionally carries a star, whatever its state — the mark has
    to survive the run finishing, or a watched run that passed would look like one
    that was never marked.
    """
    state = run.state
    if state == "failed":
        cell = Text("●", style="bold red")
    elif state == "partiallySucceeded":
        cell = Text("●", style="bold yellow")
    elif run.in_flight:
        cell = Text("●", style="bold cyan")
    else:
        cell = Text(" ")
    if watched:
        cell.append("★", style="bold yellow")
    return cell


def run_row(run: Run, now: datetime, watched: bool = False) -> tuple[Text, ...]:
    """The cells for one run row, in `run_columns()` order.

    The "Run" column carries the build number *and* what the run is about, because
    `20260730.5` on its own identifies a run without telling you anything about it
    — and what it is about is the reason you are looking.
    """
    label = Text(run.number, style="dim")
    if run.description:
        label.append(f"  {_elide(run.description, _RUN_TITLE_WIDTH)}", style="")
    return (
        attention_cell(run, watched),
        Text(_elide(run.pipeline_name, _PIPELINE_NAME_WIDTH), style="cyan"),
        label,
        state_cell(run.state),
        Text(run.trigger, style="dim"),
        Text(_elide(run.branch_label or "—", _BRANCH_WIDTH), style="dim"),
        Text(format_relative(run.happened_at, now), style="dim"),
        _duration_cell(run.duration, run.start_time, now),
    )


# --- the record (timeline) list ----------------------------------------------


def record_columns() -> tuple[str, ...]:
    return _RECORD_COLUMNS


def record_row(row: RecordRow, now: datetime) -> tuple[Text, ...]:
    """One row of the timeline tree.

    The step name carries its tree prefix, so the pane reads as the run's shape: a
    stage above its jobs, a job above its tasks. A record the tree could not place
    is marked rather than dropped, and a re-run attempt is labelled with its
    number — two rows with the same name and different attempts are the history of
    a retry, not a duplicate.
    """
    record = row.record
    label = Text()
    if row.prefix:
        label.append(row.prefix, style="dim")
    label.append(record.name or "(unnamed)", style="cyan" if record.has_log else "")
    if record.attempt > 1:
        label.append(f"  attempt {record.attempt}", style="dim yellow")
    if row.unplaced:
        label.append("  (unlinked)", style="dim yellow")
    if record.errors:
        label.append(f"  ⚠ {len(record.errors)}", style="bold red")
    elif record.warning_count:
        label.append(f"  ⚠ {record.warning_count}", style="yellow")
    return (
        Text(str(row.position), style="dim"),
        Text("●", style="bold red") if record.failed else Text(" "),
        label,
        state_cell(record.display_state),
        Text(record.type, style="dim"),
        Text(_elide(record.worker_name or "—", 16), style="dim"),
        Text(format_relative(record.start_time, now), style="dim"),
        _duration_cell(record.duration, record.start_time, now),
    )


# --- the pipelines list ------------------------------------------------------


def pipeline_columns() -> tuple[str, ...]:
    return _PIPELINE_COLUMNS


def pipeline_marker(pipeline: Pipeline, last_run: Run | None = None) -> Text:
    """Why this pipeline might need you: a failed last run, or a stopped queue.

    A paused or disabled pipeline is *labelled*, never filtered out — the same
    promise airflow-watch makes about a paused DAG, for the same reason: a row
    silently absent is the failure mode a monitoring tool must not have.
    """
    run = last_run if last_run is not None else pipeline.last_run
    if run is not None and run.state == "failed":
        return Text("✖", style="bold red")
    if run is not None and run.state == "partiallySucceeded":
        return Text("◐", style="bold yellow")
    if pipeline.is_disabled:
        return Text("⊘", style="yellow")
    if pipeline.is_paused:
        return Text("⏸", style="yellow")
    return Text(" ")


def live_cell(count: int) -> Text:
    """The Pipelines view's "is it running right now?" column: a live dot, with
    the count when more than one run is in flight at once."""
    if count <= 0:
        return Text("—", style="dim")
    return Text("●" if count == 1 else f"● {count}", style="bold cyan")


def pipeline_row(
    pipeline: Pipeline,
    now: datetime,
    last_run: Run | None = None,
    live: int = 0,
) -> tuple[Text, ...]:
    """One row of the Pipelines view — the shape of the azdo "Recent" tab.

    `last_run` is passed in rather than read off the pipeline because the app
    reconciles the cached inventory's copy against the live runs window (see
    `Snapshot.latest_run_for`), and the row must show whichever is newer.
    """
    run = last_run if last_run is not None else pipeline.last_run
    name = Text(_elide(pipeline.name, _PIPELINE_NAME_WIDTH), style="cyan")
    if pipeline.is_paused:
        name.append("  paused", style="dim yellow")
    if pipeline.is_disabled:
        name.append("  disabled", style="dim yellow")
    if run is None:
        return (
            pipeline_marker(pipeline, run),
            name,
            live_cell(live),
            Text("never run", style="dim"),
            Text("—", style="dim"),
            Text("—", style="dim"),
            Text(_elide(pipeline.folder or "—", 16), style="dim"),
        )
    detail = Text(run.number, style="dim")
    if run.description:
        detail.append(f"  {_elide(run.description, _RUN_TITLE_WIDTH)}", style="")
    return (
        pipeline_marker(pipeline, run),
        name,
        live_cell(live),
        detail,
        state_cell(run.state),
        Text(format_relative(run.happened_at, now), style="dim"),
        Text(_elide(pipeline.folder or "—", 16), style="dim"),
    )


def render_pipeline(
    pipeline: Pipeline, last_run: Run | None, now: datetime
) -> RenderableType:
    """The selected pipeline in the detail pane.

    Same shape as the Run pane: the facts as `key: value` lines, one per line (see
    `render_run` for why a column grid lost this job).
    """
    body = Text()
    body.append(pipeline.name, style="bold cyan")
    if pipeline.is_paused:
        body.append("  [paused — new runs will queue but not start]", style="yellow")
    if pipeline.is_disabled:
        body.append("  [disabled — it cannot be run]", style="yellow")
    if pipeline.folder:
        body.append(f"\n{pipeline.folder}", style="dim")

    stats: list[tuple[str, Text]] = [
        ("last run", Text(_run_summary(last_run, now))),
        ("state", state_cell(last_run.state) if last_run else Text("—", style="dim")),
        ("trigger", Text(last_run.trigger if last_run else "—")),
        ("branch", Text(last_run.branch_label if last_run else "—")),
        ("queue status", Text(pipeline.queue_status)),
        ("author", Text(pipeline.authored_by or "—")),
        ("id", Text(str(pipeline.id))),
    ]
    return Panel(
        Group(
            body,
            Text(),
            key_value_lines(stats),
            Text(),
            Text(
                "enter → this pipeline's runs · t queue a run · o open in azdo",
                style="dim italic",
            ),
        ),
        title=Text("Pipeline", style="bold"),
        border_style="red" if pipeline.needs_attention else "cyan",
        padding=(1, 2),
    )


def _run_summary(run: Run | None, now: datetime) -> str:
    if run is None:
        return "never run"
    when = format_relative(run.happened_at, now)
    return f"{run.number} · {when}"


# --- the summary bar -------------------------------------------------------


def view_tabs(view: str) -> Text:
    """The view switcher: every view's label, with the active one lit up."""
    tabs = Text()
    for index, name in enumerate(VIEWS):
        if index:
            tabs.append(" │ ", style="dim")
        tabs.append(
            VIEW_LABELS[name], style="bold reverse cyan" if name == view else "dim"
        )
    return tabs


def shown_of(shown: int, total: int, noun: str, *, more: bool = False) -> Text:
    """"N of M runs", or "N runs · more available".

    Two phrasings because Azure DevOps supports only one of them: the build list
    pages by opaque token and reports no total, so `more` is all that can honestly
    be said there, while the pipeline inventory *is* fully counted once loaded. A
    tool that invented an M for the runs list would be making it up.
    """
    if total and total > shown:
        return Text(f"{shown} of {total:,} {noun}", style="bold")
    label = Text(f"{shown} {noun}", style="bold")
    if more:
        label.append(" · more available", style="dim")
    return label


def render_summary(
    snapshot: Snapshot | None,
    error: str | None,
    *,
    view: str = "runs",
    shown: int | None = None,
    hidden_stopped: bool = False,
    state_filter: str | None = None,
    watched_runs: tuple[Run, ...] = (),
    watched_total: int = 0,
) -> Text:
    """The one-line status bar docked at the top: which view, which project, and
    what state its rows are in.

    `shown` is how many rows the list is actually displaying after any `/` filter,
    so the bar never claims more than is on screen. `state_filter` marks the `R`
    narrowing for the same reason: a list showing only one state must never read
    as a project with nothing else.

    `watched_runs` are the watched runs currently inside the loaded run window and
    `watched_total` is the whole watch list — the gap between them is a watched run
    the poll no longer holds, which the Watched view cannot show and therefore must
    count out loud.
    """
    bar = view_tabs(view)
    if snapshot is not None:
        bar.append("  ")
        bar.append(snapshot.project.label, style="bold cyan")

    if error is not None:
        bar.append(f"   ✖ {error}", style="bold red")
        return bar
    if snapshot is None:
        bar.append("   Contacting Azure DevOps…", style="dim italic")
        return bar

    bar.append("  ·  ")
    if view == "pipelines":
        bar.append_text(
            shown_of(
                len(snapshot.pipelines) if shown is None else shown,
                snapshot.pipelines_total,
                "pipelines",
            )
        )
        live = sum(snapshot.in_flight_counts().values())
        bar.append("   ")
        bar.append(f"● {live} in flight", style="bold cyan" if live else "dim")
        stopped = snapshot.paused_count + snapshot.disabled_count
        bar.append("   ")
        label = f"⏸ {stopped} paused/disabled"
        if hidden_stopped and stopped:
            label += " hidden · s shows"
        bar.append(label, style="yellow" if stopped else "dim")
    elif view == "watched":
        failed = sum(1 for run in watched_runs if run.state == "failed")
        live = sum(1 for run in watched_runs if run.in_flight)
        bar.append_text(
            shown_of(
                len(watched_runs) if shown is None else shown, watched_total, "watched"
            )
        )
        bar.append("   ")
        bar.append(f"✖ {failed} failed", style="bold red" if failed else "dim")
        bar.append("   ")
        bar.append(f"● {live} in flight", style="bold cyan" if live else "dim")
        outside = watched_total - len(watched_runs)
        if outside > 0:
            # Watched but no longer inside the loaded run window — the one way a
            # row can be absent from this view without being unwatched.
            bar.append("   ")
            bar.append(f"⋯ {outside} outside the loaded runs", style="bold yellow")
    else:
        runs = snapshot.runs
        failed = sum(1 for run in runs if run.state == "failed")
        partial = sum(1 for run in runs if run.state == "partiallySucceeded")
        live = sum(1 for run in runs if run.state == "running")
        queued = sum(1 for run in runs if run.state == "queued")
        bar.append_text(
            shown_of(
                len(runs) if shown is None else shown,
                0,
                "runs",
                more=snapshot.runs_more,
            )
        )
        bar.append("   ")
        bar.append(f"✖ {failed} failed", style="bold red" if failed else "dim")
        if partial:
            bar.append("   ")
            bar.append(f"◐ {partial} partial", style="bold yellow")
        bar.append("   ")
        bar.append(f"● {live} running", style="bold cyan" if live else "dim")
        bar.append("   ")
        bar.append(f"◌ {queued} queued", style="yellow" if queued else "dim")
    if state_filter is not None:
        glyph, style = state_style(state_filter)
        bar.append("   ")
        bar.append(f"{glyph} {state_filter} only · R cycles", style=style)
    if snapshot.pipelines_truncated:
        bar.append("   ")
        bar.append("⋯ pipeline list truncated", style="bold yellow")
    return bar


# --- the detail pane ------------------------------------------------------


def render_run(run: Run, pipeline: Pipeline | None, now: datetime) -> RenderableType:
    """The selected run at level "runs": what it is and how to drill in.

    The facts read as `key: value` lines rather than a row of columns — a run has
    too many fields for a horizontal grid to survive a narrow pane, and a vertical
    list scans the same way whatever the split is set to.
    """
    body = Text()
    body.append(run.pipeline_name, style="bold cyan")
    if pipeline is not None and pipeline.is_paused:
        body.append("  [pipeline paused]", style="yellow")
    body.append(f"\n{run.number}", style="dim")
    if run.description:
        body.append(f"  {run.description}")
    body.append("\n\n")
    body.append_text(state_cell(run.state))
    body.append(f"    {run.trigger}", style="dim")

    stats: list[tuple[str, Text]] = [
        ("branch", Text(run.branch_label or "—")),
        ("commit", Text(run.short_commit or "—")),
        ("for", Text(run.requested_for or "—")),
        ("queued", Text(format_relative(run.queue_time, now))),
        ("started", Text(format_relative(run.start_time, now))),
        ("finished", Text(format_relative(run.finish_time, now))),
        ("duration", _duration_cell(run.duration, run.start_time, now)),
    ]
    if run.queued_for is not None and run.queued_for >= 30:
        # Only when it is worth knowing: every run waits a few seconds for an
        # agent, and a row that always says "waited 6s" is a row nobody reads. Half
        # a minute is where waiting starts to be the explanation for a slow run.
        stats.append(("waited for an agent", Text(format_duration(run.queued_for))))
    if run.queue_name:
        stats.append(("pool", Text(run.queue_name)))
    if run.pr_number:
        stats.append(("pull request", Text(f"#{run.pr_number}")))
    if run.tags:
        stats.append(("tags", Text(", ".join(run.tags))))

    return Panel(
        Group(
            body,
            Text(),
            key_value_lines(stats),
            Text(),
            Text(
                "enter → stages, jobs and tasks · o open in azdo · i summarize in gw",
                style="dim italic",
            ),
        ),
        title=Text("Run", style="bold"),
        border_style="red" if run.needs_attention else "cyan",
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


def issue_line(issue: Issue) -> Text:
    """One issue, as the record pane and the `e` overlay both show it.

    The line number leads when there is one: it is what turns "this failed" into
    "this failed *here*", and it is the number the log pane's `/` filter can be
    pointed at.
    """
    glyph, style = ("✖", "bold red") if issue.is_error else ("⚠", "bold yellow")
    line = Text(f"{glyph} ", style=style)
    if issue.log_line is not None:
        line.append(f"line {issue.log_line}  ", style="dim")
    line.append(issue.message or "(no message)", style="red" if issue.is_error else "")
    return line


def render_record(record: Record, run: Run, now: datetime) -> RenderableType:
    """The selected timeline record at level "records": what it did, and how to
    reach its log.

    A record's issues are hoisted here rather than left in the log, because the
    timeline already knows them: this is the pane where "which step failed and
    what did it say" is answered without a second fetch.
    """
    body = Text()
    body.append(record.name or "(unnamed)", style="bold cyan")
    body.append(f"\n{run.pipeline_name} · {run.number}\n\n", style="dim")
    body.append_text(state_cell(record.display_state))
    body.append(f"    {record.type}", style="dim")
    if record.attempt > 1:
        body.append(f"    attempt {record.attempt}", style="yellow")

    stats: list[tuple[str, Text]] = [
        ("started", Text(format_relative(record.start_time, now))),
        ("finished", Text(format_relative(record.finish_time, now))),
        ("duration", _duration_cell(record.duration, record.start_time, now)),
        ("agent", Text(record.worker_name or "—")),
    ]
    if record.percent_complete is not None and record.display_state == "running":
        stats.append(("progress", Text(f"{record.percent_complete}%")))

    parts: list[RenderableType] = [body, Text(), key_value_lines(stats)]
    if record.issues:
        parts += [Text(), Text("Issues", style="bold")]
        parts += [issue_line(issue) for issue in record.issues]
    hint = (
        "enter → log"
        if record.has_log
        else "this step has no log of its own — open a job or task inside it"
    )
    if record.type == "Stage" and record.failed:
        hint += " · Y re-run this stage"
    parts += [Text(), Text(hint, style="dim italic")]
    return Panel(
        Group(*parts),
        title=Text(f"{record.type}", style="bold"),
        border_style="red" if record.failed else "cyan",
        padding=(1, 2),
    )


# How many log lines the pane will render at once. The transport cannot stream, so
# a log arrives as one buffered body; rendering an unbounded one would stall the
# UI. Past this the pane states how many lines it is not showing — `/` narrows to
# the ones you want — rather than silently showing part of the log.
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
# handle the click. `link` is the OSC 8 terminal hyperlink, which a terminal that
# speaks it opens itself (⌘-click in iTerm2, Ghostty, WezTerm). The `@click` meta
# is Textual's own: clicking a span carrying one runs the action named in it, which
# is the only path that works while the app holds the mouse.
#
# LINK_ACTION is the one place this module names the app it renders into —
# `AzdoWatchApp.action_open_link`, which a test asserts still exists. `repr` quotes
# the URL so a query string full of `&`, `=` and `#` survives Textual's action
# parse.
LINK_ACTION = "app.open_link"


def link_style(url: str) -> Style:
    """The style that makes a span of text a clickable link to `url`."""
    return Style(link=url, underline=True) + Style.from_meta(
        {"@click": f"{LINK_ACTION}({url!r})"}
    )


def linkify(text: Text, line: str) -> Text:
    """`text` with every URL in `line` made clickable, in place.

    Takes the source line as well as the `Text` because the spans are offsets into
    the original — the caller may already have styled it (a search highlight, say),
    and the two stack rather than replace each other.
    """
    for start, end, url in find_urls(line):
        text.stylize(link_style(url), start, end)
    return text


# The Azure Pipelines log markers, which are the structure inside an otherwise
# flat log: `##[section]` bounds a step, `##[error]` and `##[warning]` are the
# service's own annotations, `##[group]` folds output in the web UI. Styled so a
# log skims the way it does in the browser.
_LOG_MARKERS: tuple[tuple[str, str], ...] = (
    ("##[error]", "bold red"),
    ("##[warning]", "bold yellow"),
    ("##[section]", "bold cyan"),
    ("##[command]", "magenta"),
    ("##[group]", "dim cyan"),
    ("##[endgroup]", "dim cyan"),
    ("##[debug]", "dim"),
)


def marker_style(line: str) -> str | None:
    """The style for a log line that carries an Azure Pipelines marker, or None.

    Matched anywhere in the line rather than only at the start: the agent prefixes
    some markers with its own indentation, and an error styled only when it happens
    to be flush-left is an error that hides half the time.
    """
    for marker, style in _LOG_MARKERS:
        if marker in line:
            return style
    return None


def log_line(line: str, query: str) -> Text:
    """One rendered log line: markers coloured, search hits marked, URLs clickable.

    Ordering matters — the marker style is applied to the whole line first so a
    search highlight, which is a span, still stands out against it.
    """
    text = highlight(line, query) if query.strip() else Text(line)
    style = marker_style(line)
    if style is not None:
        text.stylize_before(style)
    return linkify(text, line)


def issue_banner(record: Record | None) -> RenderableType | None:
    """The "here is what this step reported" block above a log, or None.

    The whole point of hoisting it: azdo's timeline already knows the error and the
    line it was printed on, so the reader should not have to find it in four
    thousand lines. `E` then filters the log to the markers themselves.
    """
    if record is None or not record.issues:
        return None
    lines: list[RenderableType] = [issue_line(issue) for issue in record.issues]
    lines.append(Text("E filters the log to errors · esc clears", style="dim italic"))
    return Group(*lines)


def render_log(
    record: Record,
    log: RunLog | None,
    *,
    loading: bool = False,
    query: str = "",
) -> RenderableType:
    """One record's log, with any `/` filter applied.

    A filter here shows only matching lines, keeping each line's *original* number
    so a filtered view still tells you where in the log you are, and highlighting
    what matched — which is what makes an issue's `line 25` a number you can
    actually go and look at.
    """
    subtitle = Text(
        f"log {record.log_id}" if record.has_log else "no log", style="dim"
    )
    if loading:
        body: RenderableType = Align.center(Text("Fetching log…", style="dim italic"))
    elif not record.has_log:
        body = Align.center(
            Text(
                f"A {record.type.lower()} has no log of its own — open a job or "
                "task inside it.",
                style="dim",
            )
        )
    elif log is None:
        body = Align.center(Text("No log loaded.", style="dim"))
    elif not log.content.strip():
        body = Align.center(
            Text("Azure DevOps returned an empty log for this step.", style="dim")
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
            subtitle = Text(f"{len(hits)} of {total} lines match ", style="bold yellow")
            subtitle.append(f"{query!r}", style="yellow")
            subtitle.append("   esc clears", style="dim italic")

    banner = issue_banner(record)
    if banner is not None:
        body = Group(banner, Text(), body)

    return Panel(
        body,
        title=Text(f"Log · {record.name or record.type}", style="bold"),
        subtitle=subtitle,
        border_style="red" if record.failed else "cyan",
        padding=(0, 1),
    )


def _log_body(
    hits: list[tuple[int, str]], total: int, query: str, log: RunLog
) -> RenderableType:
    """The log lines themselves, numbered, bounded, and honest about it.

    Bounded from the *end* when unfiltered: a pipeline log explains its failure in
    its last hundred lines, so a log longer than the pane can hold should show the
    part that says why — showing the first four thousand lines of a fifty-thousand
    line log is showing the agent's setup.
    """
    shown = hits[-MAX_LOG_LINES:] if not query.strip() else hits[:MAX_LOG_LINES]
    table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    table.add_column(justify="right", style="dim", no_wrap=True)  # line number
    table.add_column(ratio=1, overflow="fold")
    parts: list[RenderableType] = []
    if len(hits) > len(shown) and not query.strip():
        parts.append(
            Text(
                f"… first {len(hits) - len(shown)} lines not shown — this is the "
                f"tail of {total:,}. / filters, E shows errors.",
                style="bold yellow",
            )
        )
    for number, line in shown:
        table.add_row(str(number), log_line(line, query))
    parts.append(table)
    if len(hits) > len(shown) and query.strip():
        parts.append(
            Text(
                f"… {len(hits) - len(shown)} more matching lines not shown "
                f"({total:,} in this log).",
                style="bold yellow",
            )
        )
    if log.truncated:
        # The *fetch* stopped short, not just the render: say so, or the log looks
        # like it ends mid-sentence for no reason.
        parts.append(
            Text(
                "… log too large to hold in full — this is the first "
                f"{total:,} lines.",
                style="bold yellow",
            )
        )
    elif len(hits) <= len(shown) and not query.strip():
        parts.append(Text(f"end of log ({total:,} lines)", style="dim italic"))
    return Group(*parts)


def render_issues(run: Run | None, records: tuple[Record, ...]) -> RenderableType:
    """The `e` overlay: every error and warning in the drilled-into run.

    The tool's best answer to "why did this fail?", and it costs nothing: the
    timeline fetched for the drill-down already carries every issue with the step
    that raised it and the log line it was printed on. Errors first, then warnings,
    each in tree order.
    """
    if run is None:
        return Panel(
            Align.center(
                Text(
                    "Drill into a run (enter) to see its errors and warnings.",
                    style="dim",
                )
            ),
            title=Text("Issues", style="bold"),
            subtitle=Text("e / esc to close", style="dim"),
            border_style="cyan",
            padding=(1, 2),
        )
    pairs = collect_issues(list(records))
    if not pairs:
        return Panel(
            Align.center(
                Text(
                    f"No errors or warnings in {run.number} 🎉",
                    style="green",
                )
            ),
            title=Text("Issues", style="bold"),
            subtitle=Text("e / esc to close", style="dim"),
            border_style="green",
            padding=(1, 2),
        )
    table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    table.add_column(no_wrap=True)  # glyph
    table.add_column(no_wrap=True)  # step
    table.add_column(ratio=1)  # message
    errors = sum(1 for _, issue in pairs if issue.is_error)
    for record, issue in pairs:
        glyph, style = ("✖", "bold red") if issue.is_error else ("⚠", "bold yellow")
        where = Text(_elide(record.name or record.type, 34), style="cyan")
        if issue.log_line is not None:
            where.append(f":{issue.log_line}", style="dim")
        table.add_row(Text(glyph, style=style), where, Text(issue.message))
    return Panel(
        table,
        title=Text(
            f"Issues · {run.pipeline_name} {run.number} "
            f"({errors} error{'s' if errors != 1 else ''}, "
            f"{len(pairs) - errors} warning{'s' if len(pairs) - errors != 1 else ''})",
            style="bold",
        ),
        subtitle=Text("e / esc to close", style="dim"),
        border_style="red" if errors else "yellow",
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

    `shown` is how many rows survived the `/` filter. It matters: a list emptied by
    a filter and a project with nothing in it look identical from here otherwise,
    and telling the user "nothing matches" is the useful message. `query`
    disambiguates the same two cases in the Watched view, where an empty unfiltered
    list means "nothing marked yet" and deserves to say how to mark something.
    """
    noun = {"watched": "watched runs", "pipelines": "pipelines"}.get(view, "runs")
    loaded = (
        ()
        if snapshot is None
        else (snapshot.pipelines if view == "pipelines" else snapshot.runs)
    )
    rows: tuple[object, ...] | range = loaded if shown is None else range(shown)
    if loading:
        message = Text("Contacting Azure DevOps…", style="dim italic")
    elif error is not None:
        message = Text(error, style="red")
    elif not rows and view == "watched" and not query.strip():
        message = Text(
            "Nothing watched — w on a run marks it, W clears the list.", style="dim"
        )
    elif not rows:
        message = Text(f"No {noun} match the current filter.", style="dim")
    else:
        message = Text(
            f"Select a {'pipeline' if view == 'pipelines' else 'run'} on the left.",
            style="dim",
        )
    return Panel(
        Align.center(message),
        title="azdo-watch",
        border_style="cyan",
        padding=(1, 2),
    )


def render_drill_error(message: str) -> RenderableType:
    """A drill-down that failed. Distinct from a poll failure: the list is still
    good, only this one fetch is not."""
    return Panel(
        Text(message, style="red"),
        title=Text("Could not load", style="bold"),
        border_style="red",
        padding=(1, 2),
    )


def render_loading(what: str) -> RenderableType:
    """The visible loading state every drill-down needs.

    Each level costs a process spawn (~1.5s), so a pane that changed instantly to
    blank would read as broken. Naming what is being fetched is the whole point.
    """
    return Panel(
        Align.center(Text(f"Fetching {what}…", style="dim italic")),
        title="azdo-watch",
        border_style="cyan",
        padding=(1, 2),
    )


def render_detail(
    drill: Drill,
    snapshot: Snapshot | None,
    run: Run | None,
    record: Record | None,
    now: datetime,
    *,
    error: str | None = None,
    view: str = "runs",
    pipeline: Pipeline | None = None,
    last_run: Run | None = None,
    query: str = "",
    shown: int | None = None,
) -> RenderableType:
    """The detail pane for whatever view and drill level is active.

    One pure function so the view/level→pane mapping is testable without the app.

    `drill.error` is a failed *drill-down* — the list is still good, only this one
    fetch is not. `error` is a failed *poll*, which only reaches the pane when
    there is no last-good row to show instead; otherwise the summary bar carries it
    and the stale row stays readable.
    """
    if drill.error is not None:
        return render_drill_error(drill.error)
    if drill.level == "log":
        if drill.record is None:
            return render_loading("log")
        return render_log(drill.record, drill.log, loading=drill.loading, query=query)
    if drill.level == "records":
        if drill.loading:
            return render_loading("stages, jobs and tasks")
        if run is None:
            return render_detail_placeholder(snapshot, error)
        if record is None:
            message = (
                f"No step matches {query!r}."
                if query.strip()
                else "This run has no timeline yet — it may not have started."
            )
            return Panel(
                Align.center(Text(message, style="dim")),
                title=Text("Timeline", style="bold"),
                border_style="cyan",
                padding=(1, 2),
            )
        return render_record(record, run, now)
    if view == "pipelines":
        if pipeline is None:
            return render_detail_placeholder(
                snapshot,
                error,
                loading=snapshot is None and error is None,
                view=view,
                shown=shown,
            )
        return render_pipeline(pipeline, last_run or pipeline.last_run, now)
    if run is None:
        return render_detail_placeholder(
            snapshot,
            error,
            loading=snapshot is None and error is None,
            view=view,
            shown=shown,
            query=query,
        )
    known = snapshot.pipeline(run.pipeline_id) if snapshot is not None else None
    return render_run(run, known, now)


# --- the chart strip ---------------------------------------------------------
#
# Two charts stacked under the detail pane (`g` toggles the strip): an in-flight
# chart counting how many runs (or, drilled in, timeline records) were going at
# once, above a stacked-bar timeline of activity — starts bucketed over time, one
# bucket per column, coloured by state. Bucketing, stacking, and overlap counting
# are pure functions asserted on directly in tests; only the bucket count depends
# on the pane's width, which is why each body is a renderable (resolved at render
# time) rather than a function of the data alone.
#
# The in-flight chart earns its place here more than it does in airflow-watch: a
# pipeline's jobs run in parallel across an agent pool, so "how many at once" is
# also "how much of the pool this run was holding".

# chart group -> style, in stacking order: the bottom of the bar first. Failure
# sits at the bottom so a tall stack of successes can never hide it.
CHART_GROUPS: tuple[tuple[str, str], ...] = (
    ("failed", "red"),
    ("running", "cyan"),
    ("queued", "yellow"),
    ("succeeded", "green"),
    ("other", "magenta"),
)

# state -> chart group. Coarser than `_STATE_STYLE` on purpose: a one-character
# column can carry four or five colours legibly, not thirteen.
_CHART_GROUP_OF = {
    "failed": "failed",
    "abandoned": "failed",
    "partiallysucceeded": "failed",
    "running": "running",
    "completing": "running",
    "queued": "queued",
    "pending": "queued",
    "postponed": "queued",
    "succeeded": "succeeded",
    "succeededwithissues": "succeeded",
}

# When the point happened (None for one that cannot be dated), and its state.
ChartPoint = tuple[datetime | None, str]

# The chart body's fixed geometry: how many rows of bars, and the y-axis gutter
# ("  12┤") to the left of them.
CHART_BAR_ROWS = 5
_CHART_GUTTER = 5


def chart_group(state: str) -> str:
    """Which chart colour a state contributes to. Total, like `state_style`: an
    unknown state lands in "other" rather than raising.

    `partiallySucceeded` groups with failure, not success: on a chart whose job is
    "where should I look", a run that failed some of its jobs belongs with the red.
    """
    return _CHART_GROUP_OF.get((state or "none").strip().lower(), "other")


def chart_counts(
    points: Sequence[ChartPoint], now: datetime, buckets: int
) -> list[dict[str, int]]:
    """Group counts per bucket, over `buckets` equal slices of the time window.

    The window runs from the oldest dated point to `now` (or to the newest point,
    should one somehow sit in the future). Undated points are skipped — there is
    nowhere on a time axis to put them; `render_chart` reports them.
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

    Proportional, with one guarantee worth its own code: every non-empty group gets
    at least one cell, granted in `CHART_GROUPS` order. A bucket of 19 successes
    and 1 failure must still show red — rounding away the failure would hide
    exactly what a monitoring chart exists to show.
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
        by_remainder = sorted(present, key=lambda group: quotas[group] % 1, reverse=True)
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
    render time decides the bucket count, so resizing the pane rescales the chart
    instead of clipping it.
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


def render_chart(drill: Drill, runs: tuple[Run, ...], now: datetime) -> RenderableType:
    """The chart pane under the detail pane: activity over time.

    Follows the drill the way the detail pane does — the recent runs at the top
    level, the drilled-into run's timeline inside one. Undated points cannot sit on
    a time axis, so when nothing can be placed the pane says so rather than drawing
    an empty axis.
    """
    if drill.level in ("records", "log") and drill.run is not None:
        title = f"Steps over time · {drill.run.pipeline_name}"
        points = tuple(
            (record.start_time, record.display_state) for record in drill.records
        )
        empty = "No step has started yet."
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


# When a run or record started (None if it never did) and when it ended (None
# while it is still going).
ChartSpan = tuple[datetime | None, datetime | None]


def in_flight_counts(
    spans: Sequence[ChartSpan], now: datetime, buckets: int
) -> list[int]:
    """How many spans were in flight during each of `buckets` equal slices.

    The window matches `chart_counts`: oldest start to `now` (or to the latest end,
    should one somehow sit in the future). A span with no end is still going, so it
    runs to `now` — the same reading `_duration_cell` gives a record with a start
    and no end. A span that never started is skipped: it was never in flight.
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

    One colour, cyan — the in-flight colour everywhere else — scaled to the peak,
    with any non-zero bucket getting at least one cell. `spans` must already have
    starts (no Nones) and be non-empty — `render_in_flight_chart` owns the empty
    state. Width-adaptive the way `ChartBody` is, and for the same reason.
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
                line.append("█" if height > row else " ", style="cyan")
            yield line
        yield from _time_axis(min(start for start, _ in self.spans), self.now, buckets)


def render_in_flight_chart(
    drill: Drill, runs: tuple[Run, ...], now: datetime
) -> RenderableType:
    """The companion chart: how many were running at once, over time.

    The activity chart answers "what happened and when"; this one answers "how much
    was going on at once" — the concurrency the agent pool was sustaining. Each run
    (or, drilled in, timeline record) occupies its start→end interval, to `now`
    while it is still going, and every bucket counts the intervals crossing it.
    """
    if drill.level in ("records", "log") and drill.run is not None:
        title = f"Steps in flight · {drill.run.pipeline_name}"
        spans: tuple[ChartSpan, ...] = tuple(
            (record.start_time, record.finish_time) for record in drill.records
        )
        empty = "No step has started yet."
    else:
        title = "Runs in flight"
        spans = tuple((run.start_time, run.finish_time) for run in runs)
        empty = "No run has started yet."
    started = tuple((start, end) for start, end in spans if start is not None)
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


def render_loading_older(held: int) -> Text:
    """The bottom bar while a scroll-triggered run extension is in flight.

    The refresh countdown gives up its row for the duration: "the list is about to
    grow" is the one thing the user just asked for by scrolling, and without a
    notice the seconds it takes read as nothing happening.
    """
    notice = Text(justify="center")
    notice.append("⇣ loading older runs… ", style="bold cyan")
    notice.append(f"({held} loaded)", style="dim")
    return notice


# --- overlays --------------------------------------------------------------


def render_projects(projects: tuple[Project, ...], selected: str) -> RenderableType:
    """The project switcher."""
    if not projects:
        body: RenderableType = Text("No projects visible.", style="dim italic")
    else:
        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column(no_wrap=True)  # cursor
        table.add_column(ratio=1)  # name
        table.add_column(no_wrap=True)  # note
        for index, project in enumerate(projects, start=1):
            active = project.key == selected
            table.add_row(
                Text(f"{index}", style="bold cyan" if active else "dim"),
                Text(project.name, style="bold reverse cyan" if active else ""),
                Text(project.visibility or project.state, style="dim"),
            )
        body = table
    return Panel(
        body,
        title=Text("Projects", style="bold"),
        subtitle=Text("1-9 to switch · P / esc to close", style="dim"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_confirm(action: Action) -> RenderableType:
    """The confirmation modal's body: exactly what is about to happen.

    Every mutating action passes through here first, naming its target, so nothing
    can fire on a single keystroke. There is no dry-run line to offer — Azure
    DevOps has no preview for any of these — so the modal says plainly that this
    changes state, which is the whole warning it can give.
    """
    body = Text()
    body.append(action.title, style="bold")
    body.append("\n\n")
    body.append("target  ", style="dim")
    body.append(action.target, style="bold cyan")
    body.append("\n\n")
    body.append("This changes state in Azure DevOps.", style="bold yellow")
    if action.kind == "queue":
        body.append(
            "\nThe run starts on the pipeline's default branch unless one is named.",
            style="dim",
        )
    return Panel(
        Group(body, Text(), Text("y / enter confirm · n / esc cancel", style="dim")),
        title=Text("Confirm", style="bold"),
        border_style="yellow",
        padding=(1, 2),
    )


# --- the menu bar ------------------------------------------------------------


@dataclass(frozen=True)
class MenuEntry:
    """One command row in a menu drop-down: the direct key, what it does, and the
    app action (`action_<name>`) that selecting it runs."""

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
    """The `R` entry's label: what pressing it moves the state filter *to*, like
    every other toggle in the bar — the menu must never point the wrong
    direction."""
    index = STATE_FILTERS.index(state_filter) if state_filter in STATE_FILTERS else 0
    following = STATE_FILTERS[(index + 1) % len(STATE_FILTERS)]
    if following is None:
        return "Show all runs and pipelines"
    if state_filter is None:
        return f"Show only {following} runs / pipelines"
    return f"State filter: {state_filter} → {following}"


def menu_categories(
    *,
    chart_shown: bool = True,
    stopped_shown: bool = False,
    state_filter: str | None = None,
) -> tuple[MenuCategory, ...]:
    """Every command, organized for the menu bar's drop-downs.

    The bar is the complete map: every command appears, grouped by what it acts on,
    so it is also where a new user discovers what exists — the footer once tried to
    advertise every key and ran out of room. Selecting a command that does not apply
    right now is the same no-op its key would be; the toggles still label themselves
    by state so the menu never points the wrong direction, and every entry names its
    direct key so the menu keeps teaching the shortcuts.
    """
    return (
        MenuCategory(
            "App",
            (
                MenuEntry("r", "Refresh now", "poll_now"),
                MenuEntry("P", "Switch project", "switch_project"),
                MenuEntry("l", "Activity log", "toggle_log"),
                MenuEntry("e", "Errors and warnings in this run", "show_issues"),
                MenuEntry("?", "Help", "help"),
                MenuEntry("q", "Quit", "quit"),
            ),
        ),
        MenuCategory(
            "Runs",
            (
                MenuEntry("enter", "Drill into the run's stages and jobs", "drill_in"),
                MenuEntry("o", "Open the selection in Azure DevOps", "open_web"),
                MenuEntry("w", "Watch / unwatch the selected run", "toggle_watch"),
                MenuEntry("W", "Clear the watched runs", "clear_watched"),
                MenuEntry("t", "Queue a run of the selected pipeline", "queue_run"),
                MenuEntry("c", "Cancel the selected run", "cancel_run"),
                MenuEntry("i", "Hand the selected run to gw for a summary", "investigate"),
            ),
        ),
        MenuCategory(
            "Steps",
            (
                MenuEntry("enter", "Open the selected step's log", "drill_in"),
                MenuEntry("E", "Filter this log to errors", "filter_errors"),
                MenuEntry("<", "Previous failed step", "prev_failure"),
                MenuEntry(">", "Next failed step", "next_failure"),
                MenuEntry("Y", "Re-run the selected failed stage", "retry_stage"),
                MenuEntry("esc", "Back out one level", "escape"),
            ),
        ),
        MenuCategory(
            "View",
            (
                MenuEntry("v", "Switch view: Runs → Pipelines → Watched", "switch_view"),
                MenuEntry("/", "Filter the list on screen", "start_filter"),
                MenuEntry("R", _state_filter_menu_label(state_filter), "cycle_state_filter"),
                MenuEntry(
                    "s",
                    "Hide paused / disabled pipelines"
                    if stopped_shown
                    else "Show paused / disabled pipelines",
                    "toggle_stopped",
                ),
                MenuEntry("d", "Move / hide the detail pane", "cycle_detail"),
                MenuEntry(
                    "g", "Hide the charts" if chart_shown else "Show the charts", "toggle_chart"
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

    This is how a user answers "did my cancel actually fire?" — which matters more
    in a tool that can change state than in a read-only one, so actions get their
    own level and glyph.
    """
    if not entries:
        body: RenderableType = Text(
            "No activity yet — background polls will appear here.", style="dim italic"
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
    ("v", "Switch view: Runs → Pipelines → Watched"),
    ("/", "Filter the list (or the log) — type to narrow, esc clears"),
    ("enter", "Drill in: run → stages, jobs and tasks → log"),
    ("escape", "Back out one level, or clear the filter"),
    ("E", "Filter the open log to Azure Pipelines error markers"),
    ("< / >", "Jump to the previous / next failed step in this run"),
    ("o", "Open the selected run or pipeline in Azure DevOps — log URLs click too"),
    ("e", "Show the errors and warnings in the drilled-into run"),
    ("P", "Switch project"),
    ("s", "Show / hide paused and disabled pipelines (hidden by default)"),
    ("R", "Cycle the state filter: running → failed → partial → queued → succeeded → all"),
    ("w", "Watch / unwatch the selected run — the Watched view shows them"),
    ("W", "Clear the watched runs"),
    ("t", "Queue a new run of the selected pipeline"),
    ("c", "Cancel the selected run"),
    ("Y", "Re-run the selected failed stage"),
    ("i", "Hand the run to gw: gather the timeline + logs, summarize, wait"),
    ("d", "Cycle the detail pane: right → below → hidden"),
    ("g", "Show / hide the charts under the detail pane"),
    ("[ / ]", "Resize the windows: shrink / grow the list"),
    ("l", "Show / hide the activity log"),
    ("r", "Refresh now"),
    ("?", "Show / hide this help"),
    ("q", "Quit"),
)

HELP_NOTE = (
    "A run's steps are listed as Azure DevOps' own tree — a stage above its jobs, "
    "a job above its tasks — and a step's errors are hoisted out of the log into "
    "the Step pane and the `e` overlay, with the log line each was printed on. "
    "Runs in flight are fetched separately from the main window, so something "
    "running is always on screen however old it is. Paused and disabled pipelines "
    "are counted in the summary bar and shown with `s`. Every action that changes "
    "Azure DevOps asks first and is recorded in the activity log; there is no dry "
    "run, because the API offers none. Any URL a log prints is clickable. The menu "
    "bar at the top opens with a click or `M` and lists every command by category; "
    "`w` marks runs to follow in the Watched view for this session. Layout and the "
    "selected project are restored on the next launch."
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
    """A single-shot snapshot of the runs (or pipelines) list for `--once` /
    scripting."""
    table = Table(expand=True, header_style="bold", border_style="dim", padding=(0, 1))
    columns = pipeline_columns() if view == "pipelines" else run_columns()
    for column in columns:
        table.add_column(column, no_wrap=True)
    if view == "pipelines":
        live = snapshot.in_flight_counts()
        for pipeline in snapshot.pipelines:
            table.add_row(
                *pipeline_row(
                    pipeline,
                    now,
                    snapshot.latest_run_for(pipeline),
                    live.get(pipeline.id, 0),
                )
            )
    else:
        for run in snapshot.runs:
            table.add_row(*run_row(run, now))
    parts: list[RenderableType] = [render_summary(snapshot, None, view=view), table]
    parts.append(
        Text(
            f"{snapshot.calls} az calls in {snapshot.elapsed:.2f}s",
            style="dim italic",
        )
    )
    return Group(*parts)
