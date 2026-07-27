"""Rich rendering for the my-prs dashboard.

Everything here is a pure function of the data + a clock value. The list pane
cells and the summary/footer live here; the *detail* pane reuses pr-watch's
`render_body`, so a selected PR looks exactly like `pr-watch` on that branch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tools.pr_watch.models import CheckState
from tools.pr_watch.ui import format_relative

from .models import VIEW_LABELS, VIEWS, LogEntry, PrItem

# The review view adds an Author column — whose PR you're being asked to
# review — right where "mine" needs none (they're all yours).
_COLUMNS = {
    "mine": ("!", "Repo", "PR", "Title", "CI", "💬", "Review", "Updated"),
    "review": ("!", "Repo", "PR", "Author", "Title", "CI", "💬", "Review", "Updated"),
}

_TITLE_WIDTH = 44


def list_columns(view: str) -> tuple[str, ...]:
    return _COLUMNS[view]


def attention_cell(item: PrItem) -> Text:
    """Red dot: needs you. Green dot: ready. Blank: in between (draft,
    checks still running, merge state unknown)."""
    if item.needs_attention:
        return Text("●", style="bold red")
    if item.ready:
        return Text("●", style="bold green")
    return Text(" ")


def ci_cell(item: PrItem) -> Text:
    """Compact check status: worst state first, with a count where it helps."""
    counts = item.pr.counts()
    if item.failing:
        return Text(f"✖ {counts[CheckState.FAILURE]}", style="bold red")
    if item.pr.rollup is CheckState.PENDING:
        return Text(f"● {counts[CheckState.PENDING]}", style="bold yellow")
    if item.pr.rollup is CheckState.SUCCESS:
        return Text("✔", style="bold green")
    return Text("—", style="dim")


def comments_cell(item: PrItem) -> Text:
    n = item.open_threads
    if n == 0:
        return Text("—", style="dim")
    return Text(str(n), style="bold yellow")


def review_cell(item: PrItem) -> Text:
    if item.pr.is_draft:
        return Text("draft", style="dim")
    m = item.pr.metrics
    if m.review_decision == "APPROVED":
        return Text(f"✔ {m.approvals}", style="bold green")
    if m.review_decision == "CHANGES_REQUESTED":
        return Text("✖ changes", style="bold red")
    if m.review_decision == "REVIEW_REQUIRED":
        return Text("○ needed", style="yellow")
    # No required reviews on the repo: approvals are all we know.
    if m.approvals > 0:
        return Text(f"✔ {m.approvals}", style="green")
    return Text("○ none", style="yellow")


def _title_cell(item: PrItem) -> Text:
    title = item.pr.title
    if len(title) > _TITLE_WIDTH:
        title = title[: _TITLE_WIDTH - 1] + "…"
    style = "dim" if item.pr.is_draft else ""
    return Text(title, style=style)


def list_row(item: PrItem, now: datetime, view: str = "mine") -> tuple[Text, ...]:
    """The cells for one PR row, in `list_columns(view)` order."""
    cells = [
        attention_cell(item),
        Text(item.repo_name, style="cyan"),
        Text(f"#{item.pr.number}", style="bold"),
        _title_cell(item),
        ci_cell(item),
        comments_cell(item),
        review_cell(item),
        Text(format_relative(item.pr.metrics.updated_at, now), style="dim"),
    ]
    if view == "review":
        cells.insert(3, Text(item.pr.author, style="magenta"))
    return tuple(cells)


def view_tabs(view: str) -> Text:
    """The view switcher: every view's label, with the active one lit up."""
    tabs = Text()
    for i, name in enumerate(VIEWS):
        if i:
            tabs.append(" │ ", style="dim")
        active = name == view
        tabs.append(VIEW_LABELS[name], style="bold reverse cyan" if active else "dim")
    return tabs


def render_summary(items: list[PrItem] | None, error: str | None, view: str = "mine") -> Text:
    """The one-line counts bar docked at the top of the app."""
    summary = view_tabs(view)
    if error is not None:
        summary.append(f"   ✖ {error}", style="bold red")
        return summary
    if items is None:
        summary.append("   Contacting GitHub…", style="dim italic")
        return summary

    noun = "to review" if view == "review" else "open"
    summary.append(f"  ·  {len(items)} {noun}", style="bold")
    failing = sum(1 for i in items if i.failing)
    commented = sum(1 for i in items if i.open_threads)
    unreviewed = sum(1 for i in items if i.review_gap)
    summary.append("   ")
    summary.append(f"✖ {failing} failing", style="bold red" if failing else "dim")
    summary.append("   ")
    summary.append(f"💬 {commented} with comments", style="bold yellow" if commented else "dim")
    summary.append("   ")
    summary.append(f"○ {unreviewed} awaiting review", style="yellow" if unreviewed else "dim")
    ready = sum(1 for i in items if i.ready)
    summary.append("   ")
    summary.append(f"● {ready} ready", style="bold green" if ready else "dim")
    return summary


# One dot per recent GitHub request, keyed by its status in the app's history.
_POLL_DOT_STYLES = {
    "ok": "bold green",
    "error": "bold red",
    "running": "bold blue",
}


def render_poll_dots(history: Sequence[str]) -> Text:
    """The recent GitHub requests as a strip of dots, oldest first: green for
    success, red for failure, blue for a request still in flight."""
    dots = Text()
    for status in history:
        dots.append("●", style=_POLL_DOT_STYLES.get(status, "dim"))
    return dots


def render_status_bar(
    updated: datetime,
    seconds_to_refresh: int,
    interval: int,
    history: Sequence[str],
    *,
    refreshing: bool = False,
) -> Text:
    """The one-line bar docked at the bottom: refresh timing, the recent
    GitHub-request dots, and a pointer at the `?` popup. The keybindings
    themselves live in the popup (render_help), not here."""
    bar = Text(justify="center")
    bar.append("updated ", style="dim")
    bar.append(updated.strftime("%H:%M:%S"), style="bold")
    bar.append("   ·   ", style="dim")
    if refreshing:
        bar.append("refreshing…", style="bold cyan")
    else:
        bar.append(f"refresh in {seconds_to_refresh:>2}s", style="bold cyan")
    bar.append(f" (every {interval}s)", style="dim")
    if history:
        bar.append("   ·   ", style="dim")
        bar.append_text(render_poll_dots(history))
    bar.append("   ·   ", style="dim")
    bar.append("? help", style="dim")
    return bar


def render_detail_placeholder(
    items: list[PrItem] | None,
    error: str | None,
    *,
    loading: bool = False,
    view: str = "mine",
) -> RenderableType:
    """What the detail pane shows when there is no selected PR to render."""
    if loading:
        message = Text("Contacting GitHub…", style="dim italic")
    elif error is not None:
        message = Text(error, style="red")
    elif not items:
        if view == "review":
            message = Text("No PRs waiting on your review. 🎉", style="green")
        else:
            message = Text(
                "No open PRs of yours updated in this window. 🎉", style="green"
            )
    else:
        message = Text("Select a PR on the left.", style="dim")
    return Panel(Align.center(message), title="my-prs", border_style="cyan", padding=(1, 2))


# The activity log's per-level glyph and style, keyed by LogEntry.level.
_LOG_LEVELS = {
    "info": ("·", "dim"),
    "warn": ("▲", "yellow"),
    "error": ("✖", "bold red"),
}


def render_log(entries: list[LogEntry]) -> RenderableType:
    """The `l` overlay: every background poll's outcome, newest first.

    This is the window into what the dashboard has been doing on its own —
    each refresh's PR counts, and any rate-limit backoffs or failures — so a
    quiet-looking dashboard is never a mystery.
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
                Text(entry.message, style=style if entry.level != "info" else ""),
            )
        body = table
    return Panel(
        body,
        title=Text("Activity log", style="bold"),
        subtitle=Text("l / esc to close", style="dim"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_gw_exists(task_id: str, project: str) -> RenderableType:
    """The confirm dialog shown when gw already has a task for the PR's
    branch: recreate it with --rm, or leave it alone."""
    body = Text()
    body.append("gw already has task ")
    body.append(task_id, style="bold cyan")
    body.append(" in project ")
    body.append(project, style="bold cyan")
    body.append(".\n\n")
    body.append(
        "--rm removes that task's worktree and record, then recreates the "
        "task from the PR. The branch itself is kept, and gw refuses if the "
        "worktree has uncommitted changes.\n\n",
        style="dim",
    )
    body.append("y", style="bold cyan")
    body.append("  remove it and recreate  ")
    body.append("(gw new --pr --rm)", style="dim")
    body.append("\n")
    body.append("n / esc", style="bold cyan")
    body.append("  keep it")
    return Panel(
        body,
        title=Text("Task already exists", style="bold"),
        border_style="yellow",
        padding=(1, 2),
    )


HELP_KEYS: tuple[tuple[str, str], ...] = (
    ("↑ / ↓", "Select a PR"),
    ("v", "Switch view: your PRs ↔ PRs needing your review"),
    ("enter / o", "Open the selected PR in your browser"),
    ("g", "Open the PR's branch as a gw task (asks before --rm if it exists)"),
    ("tab", "Move focus between the list and the detail pane"),
    ("d", "Cycle the detail pane: right → below → hidden"),
    ("[ / ]", "Resize the windows: shrink / grow the list"),
    ("l", "Show / hide the activity log"),
    ("r", "Refresh now"),
    ("?", "Show / hide this help"),
    ("q", "Quit"),
)

HELP_NOTES = (
    "Layout, sizing, and the active view are saved and restored on the next launch.",
    "The status-bar dots are the last 10 GitHub requests: "
    "green ok, red failed, blue in flight.",
)


def render_help() -> RenderableType:
    """The keybinding reference shown by the `?` overlay."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(justify="right", style="bold cyan", no_wrap=True)
    table.add_column()
    for key, description in HELP_KEYS:
        table.add_row(key, description)
    notes = Group(*(Text(note, style="dim italic") for note in HELP_NOTES))
    return Panel(
        Group(table, Text(), notes),
        title=Text("Help", style="bold"),
        subtitle=Text("esc to close", style="dim"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_once(items: list[PrItem], now: datetime, view: str = "mine") -> RenderableType:
    """A single-shot snapshot of the whole list for `--once` / scripting."""
    table = Table(
        expand=True,
        header_style="bold",
        border_style="dim",
        padding=(0, 1),
    )
    for column in list_columns(view):
        table.add_column(column, no_wrap=column != "Title")
    for item in items:
        table.add_row(*list_row(item, now, view))
    return Group(render_summary(items, None, view), table)
