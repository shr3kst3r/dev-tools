"""Rich rendering for the my-issues dashboard.

Everything here is a pure function of the data + a clock value. Unlike my-prs
there is no pr-watch renderer to borrow for the detail pane — an issue has none
of a PR's checks, threads or diff stats — so `render_body` lives here too.

Two things are deliberately absent, both recorded in
`docs/adrs/2026-08-11-issues-get-no-attention-dot.md`: there is **no `!`
attention column** in any view, and the summary bar counts **facts** (labels,
comments, unassigned) rather than verdicts. Nothing on screen is a judgment
about whether an issue wants your attention.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from rich.align import Align
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from tools.pr_watch.ui import format_relative

from .models import VIEW_LABELS, VIEWS, IssueItem, LogEntry

# The people columns follow one rule: **Author** appears wherever the filer
# might not be you, and **Assignees** appears wherever the assignee might not be
# you. In the assigned view someone else usually filed it, so "who put this on
# my plate" is the useful column; in the view of issues you filed they are all
# yours to begin with, so "who is actually holding it" is. A column that would
# read as you on every row is omitted rather than padded.
#
# No view has a "!" column — see the module docstring.
_COLUMNS = {
    "assigned": ("Repo", "#", "Author", "Title", "Labels", "💬", "Age", "Updated"),
    "created": ("Repo", "#", "Title", "Labels", "Assignees", "💬", "Age", "Updated"),
    "mentioned": (
        "Repo", "#", "Author", "Title", "Labels", "Assignees", "💬", "Updated",
    ),
    "hidden": (
        "Repo", "#", "Author", "Title", "Labels", "Assignees", "💬", "Updated", "Hidden",
    ),
}

_TITLE_WIDTH = 44

# How many labels the list cell names before collapsing the rest into "+N".
_LABELS_SHOWN = 3

# GitHub gives a label's color as six bare hex digits; anything else (including
# the empty string when the field is missing) is rendered unstyled rather than
# handed to rich, which would raise on it.
_HEX = re.compile(r"^[0-9a-fA-F]{6}$")


def list_columns(view: str) -> tuple[str, ...]:
    return _COLUMNS[view]


def label_style(color: str) -> Style:
    """A label's own GitHub color as a rich style, or no style if unusable."""
    if _HEX.match(color or ""):
        return Style(color=f"#{color}")
    return Style()


def labels_cell(item: IssueItem) -> Text:
    """The issue's labels, each in its own GitHub color.

    Rendered exactly as the repo defines them — no label name is given any
    meaning here, which is ADR-constrained: the tool spans repos that don't
    share a label vocabulary.
    """
    labels = item.issue.labels
    if not labels:
        return Text("—", style="dim")
    cell = Text()
    for i, label in enumerate(labels[:_LABELS_SHOWN]):
        if i:
            cell.append(", ", style="dim")
        cell.append(label.name, style=label_style(label.color))
    extra = len(labels) - _LABELS_SHOWN
    if extra > 0:
        cell.append(f" +{extra}", style="dim")
    return cell


def assignees_cell(item: IssueItem) -> Text:
    """Who is holding the issue — `—` when nobody is, which is a fact worth
    seeing and what the summary's unassigned count counts."""
    if not item.issue.assignees:
        return Text("—", style="dim")
    return Text(", ".join(item.issue.assignees), style="magenta")


def comments_cell(item: IssueItem) -> Text:
    """How many comments the thread has. Styled to be readable, never to imply
    that a busy thread needs you more than a quiet one."""
    n = item.issue.comment_count
    if n == 0:
        return Text("—", style="dim")
    return Text(str(n), style="cyan")


def _title_cell(item: IssueItem) -> Text:
    title = item.issue.title
    if len(title) > _TITLE_WIDTH:
        title = title[: _TITLE_WIDTH - 1] + "…"
    cell = Text(title)
    if item.reopened:
        cell.append(" ↻", style="yellow")
    return cell


def _cells(
    item: IssueItem, now: datetime, hidden_at: datetime | None
) -> dict[str, Text]:
    """Every cell an issue can contribute, keyed by column name.

    Keyed rather than positional because the four views interleave their people
    columns differently; `list_row` picks in `list_columns` order, so a row can
    never drift out of step with its header.
    """
    issue = item.issue
    return {
        "Repo": Text(item.repo_name, style="cyan"),
        "#": Text(f"#{issue.number}", style="bold"),
        "Author": Text(issue.author, style="magenta"),
        "Title": _title_cell(item),
        "Labels": labels_cell(item),
        "Assignees": assignees_cell(item),
        "💬": comments_cell(item),
        "Age": Text(format_relative(issue.created_at, now), style="dim"),
        "Updated": Text(format_relative(issue.updated_at, now), style="dim"),
        "Hidden": Text(format_relative(hidden_at, now), style="dim"),
    }


def list_row(
    item: IssueItem,
    now: datetime,
    view: str = "assigned",
    hidden_at: datetime | None = None,
) -> tuple[Text, ...]:
    """The cells for one issue row, in `list_columns(view)` order.

    `hidden_at` is when the issue was hidden, and only the hidden view shows it.
    """
    cells = _cells(item, now, hidden_at)
    return tuple(cells[column] for column in list_columns(view))


def view_tabs(view: str) -> Text:
    """The view switcher: every view's label, with the active one lit up."""
    tabs = Text()
    for i, name in enumerate(VIEWS):
        if i:
            tabs.append(" │ ", style="dim")
        active = name == view
        tabs.append(VIEW_LABELS[name], style="bold reverse cyan" if active else "dim")
    return tabs


_SUMMARY_NOUNS = {
    "assigned": "assigned to you",
    "created": "you filed",
    "mentioned": "mentioning you",
    "hidden": "hidden",
}


def render_summary(
    items: list[IssueItem] | None,
    error: str | None,
    view: str = "assigned",
    hidden_total: int = 0,
) -> Text:
    """The one-line counts bar docked at the top of the app.

    Every number here is something GitHub told us: how many are labeled, how
    many have comments, how many nobody is holding. No "needs you" count —
    see the module docstring.

    `hidden_total` is the size of the whole hide list. On the visible views it's
    a dim reminder that some issues are being kept out of the list; on the
    hidden view it's what the shown rows are measured against, since a hidden
    issue the poll no longer returns has no row to appear as.
    """
    summary = view_tabs(view)
    if error is not None:
        summary.append(f"   ✖ {error}", style="bold red")
        return summary
    if items is None:
        summary.append("   Contacting GitHub…", style="dim italic")
        return summary

    summary.append(f"  ·  {len(items)} {_SUMMARY_NOUNS[view]}", style="bold")
    if view == "hidden":
        offscreen = max(0, hidden_total - len(items))
        if offscreen:
            summary.append(f"   ⊘ {offscreen} not in this window", style="dim")
        return summary
    labeled = sum(1 for i in items if i.issue.labels)
    commented = sum(1 for i in items if i.issue.comment_count)
    summary.append("   ")
    summary.append(f"🏷 {labeled} labeled", style="cyan" if labeled else "dim")
    summary.append("   ")
    summary.append(f"💬 {commented} with comments", style="cyan" if commented else "dim")
    if view != "assigned":
        # Vacuous on the assigned view: every row there has you on it.
        unassigned = sum(1 for i in items if i.unassigned)
        summary.append("   ")
        summary.append(
            f"◯ {unassigned} unassigned", style="yellow" if unassigned else "dim"
        )
    if hidden_total:
        summary.append(f"   ⊘ {hidden_total} hidden", style="dim")
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


_EMPTY_STATES = {
    "assigned": (
        "No open issues assigned to you in this window. 🎉",
        "green",
    ),
    "created": (
        "No open issues you filed were updated in this window.",
        "dim",
    ),
    "mentioned": (
        "Nothing mentioning you in this window.",
        "dim",
    ),
    "hidden": (
        "Nothing hidden — press h on an issue to keep it out of the lists.",
        "dim",
    ),
}


def render_detail_placeholder(
    items: list[IssueItem] | None,
    error: str | None,
    *,
    loading: bool = False,
    view: str = "assigned",
) -> RenderableType:
    """What the detail pane shows when there is no selected issue to render."""
    if loading:
        message = Text("Contacting GitHub…", style="dim italic")
    elif error is not None:
        message = Text(error, style="red")
    elif not items:
        text, style = _EMPTY_STATES.get(view, _EMPTY_STATES["assigned"])
        message = Text(text, style=style)
    else:
        message = Text("Select an issue on the left.", style="dim")
    return Panel(
        Align.center(message), title="my-issues", border_style="cyan", padding=(1, 2)
    )


# --- the detail pane ---------------------------------------------------------


def _detail_header(item: IssueItem) -> Panel:
    issue = item.issue
    title = Text()
    title.append(item.repo, style="bold cyan")
    title.append("  ·  ", style="dim")
    title.append(f"#{issue.number}", style="bold")

    body = Text()
    body.append(issue.title or "(no title)", style="bold")
    body.append(f"\nby @{issue.author}", style="dim")
    body.append("   ")
    body.append(issue.state.lower(), style="green" if issue.state == "OPEN" else "dim")
    if item.reopened:
        body.append("  ↻ reopened", style="yellow")
    body.append(f"\n{issue.url}", style="blue underline")
    return Panel(body, title=title, border_style="cyan", padding=(0, 2))


def _detail_meta(item: IssueItem, now: datetime) -> Panel:
    issue = item.issue
    rows: list[tuple[str, Text]] = [
        ("labels", labels_cell(item)),
        ("assignees", assignees_cell(item)),
        ("milestone", Text(issue.milestone or "—", style="" if issue.milestone else "dim")),
        ("opened", Text(format_relative(issue.created_at, now))),
        ("updated", Text(format_relative(issue.updated_at, now))),
        ("comments", Text(str(issue.comment_count))),
        ("reactions", Text(str(issue.reactions))),
    ]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="dim", no_wrap=True)
    grid.add_column(ratio=1)
    for label, value in rows:
        grid.add_row(label.upper(), value)
    return Panel(grid, title=Text("Details"), border_style="magenta", padding=(0, 1))


def _detail_body(item: IssueItem) -> Panel:
    """The issue body. Issue bodies are markdown, so render them as markdown."""
    text = item.issue.body.strip()
    body: RenderableType = (
        Markdown(text) if text else Text("No description.", style="dim italic")
    )
    return Panel(body, title=Text("Description"), border_style="blue", padding=(0, 1))


def _detail_comments(item: IssueItem, now: datetime) -> Panel:
    """The tail of the thread the poll fetched, oldest of the tail first.

    Says how many earlier comments it isn't showing, so the pane never implies
    it is the whole conversation.
    """
    issue = item.issue
    if not issue.comments:
        return Panel(
            Align.center(Text("No comments yet.", style="dim")),
            title=Text("Comments"),
            border_style="dim",
            padding=(0, 1),
        )
    parts: list[RenderableType] = []
    earlier = issue.comment_count - len(issue.comments)
    if earlier > 0:
        parts.append(Text(f"+{earlier} earlier", style="dim italic"))
    for comment in issue.comments:
        head = Text()
        head.append(f"@{comment.author}", style="bold magenta")
        head.append(f"  ·  {format_relative(comment.created_at, now)}", style="dim")
        parts.append(head)
        snippet = " ".join(comment.body.split())
        if len(snippet) > 500:
            snippet = snippet[:497] + "…"
        parts.append(Text(snippet or "(empty)", style="" if snippet else "dim"))
        parts.append(Text())
    return Panel(
        Group(*parts),
        title=Text(f"Comments ({issue.comment_count})"),
        border_style="yellow",
        padding=(0, 1),
    )


def render_body(item: IssueItem, now: datetime) -> RenderableType:
    """The detail pane for one issue. Height-unconstrained — the caller puts
    this in a scrollable viewport (live app) or plain stdout."""
    return Group(
        _detail_header(item),
        _detail_meta(item, now),
        _detail_body(item),
        _detail_comments(item, now),
    )


# --- overlays ----------------------------------------------------------------

# The activity log's per-level glyph and style, keyed by LogEntry.level.
_LOG_LEVELS = {
    "info": ("·", "dim"),
    "warn": ("▲", "yellow"),
    "error": ("✖", "bold red"),
}


def render_log(entries: list[LogEntry]) -> RenderableType:
    """The `l` overlay: every background poll's outcome, newest first.

    This is the window into what the dashboard has been doing on its own —
    each refresh's issue counts, and any rate-limit backoffs or failures — so a
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
    """The confirm dialog shown when gw already has a task for the issue:
    recreate it with --rm, or leave it alone."""
    body = Text()
    body.append("gw already has task ")
    body.append(task_id, style="bold cyan")
    body.append(" in project ")
    body.append(project, style="bold cyan")
    body.append(".\n\n")
    body.append(
        "--rm removes that task's worktree and record, then recreates the "
        "task from the issue. The branch itself is kept, and gw refuses if the "
        "worktree has uncommitted changes.\n\n",
        style="dim",
    )
    body.append("y", style="bold cyan")
    body.append("  remove it and recreate  ")
    body.append("(gw new --issue --rm)", style="dim")
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
    ("↑ / ↓", "Select an issue"),
    ("v", "Cycle view: assigned to you → you filed → mentions you → hidden"),
    ("h", "Hide the selected issue (in the Hidden view, unhide it)"),
    ("enter / o", "Open the selected issue in your browser"),
    ("g", "Open the issue as a gw task (asks before --rm if it exists)"),
    ("tab", "Move focus between the list and the detail pane"),
    ("d", "Cycle the detail pane: right → below → hidden"),
    ("[ / ]", "Resize the windows: shrink / grow the list"),
    ("l", "Show / hide the activity log"),
    ("r", "Refresh now"),
    ("?", "Show / hide this help"),
    ("q", "Quit"),
)

HELP_NOTES = (
    "Every list is sorted by most recently updated, and nothing else — there is "
    "no attention dot, because GitHub tells us nothing about an issue that "
    "reliably means 'this needs you'.",
    "The columns report facts: the labels the repo defined, who is assigned "
    "(— when nobody is), how many comments, and how long ago.",
    "Hiding is local and permanent until you unhide: hidden issues stay out of "
    "the other views across restarts, and the Hidden view is where they wait.",
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


def render_once(
    items: list[IssueItem],
    now: datetime,
    view: str = "assigned",
    hidden: Mapping[str, datetime] | None = None,
) -> RenderableType:
    """A single-shot snapshot of the whole list for `--once` / scripting."""
    hidden = hidden or {}
    table = Table(
        expand=True,
        header_style="bold",
        border_style="dim",
        padding=(0, 1),
    )
    for column in list_columns(view):
        table.add_column(column, no_wrap=column not in ("Title", "Labels"))
    for item in items:
        table.add_row(*list_row(item, now, view, hidden.get(item.key)))
    return Group(render_summary(items, None, view, len(hidden)), table)
