"""Rich rendering for pr-watch.

Everything here is a pure function of the data + a clock value, so the layout
can be snapshotted in `--once` mode and (in principle) in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import Check, CheckState, PRMetrics, PullRequest, RepoContext

# state -> (icon, rich style)
_STATE_STYLE: dict[CheckState, tuple[str, str]] = {
    CheckState.SUCCESS: ("✔", "bold green"),
    CheckState.FAILURE: ("✖", "bold red"),
    CheckState.PENDING: ("●", "bold yellow"),
    CheckState.SKIPPED: ("○", "dim"),
    CheckState.UNKNOWN: ("?", "magenta"),
}


def _rollup_banner(pr: PullRequest) -> Text:
    icon, style = _STATE_STYLE[pr.rollup]
    words = {
        CheckState.SUCCESS: "all checks passing",
        CheckState.FAILURE: "checks failing",
        CheckState.PENDING: "checks running",
        CheckState.SKIPPED: "checks skipped",
        CheckState.UNKNOWN: "no checks reported",
    }
    return Text(f"{icon}  {words[pr.rollup]}", style=style)


def _header(pr: PullRequest, ctx: RepoContext) -> Panel:
    title = Text()
    title.append(f"{ctx.name_with_owner}", style="bold cyan")
    title.append("  ·  ", style="dim")
    title.append(f"{ctx.branch}", style="bold")

    body = Text()
    body.append(f"#{pr.number} ", style="bold white")
    body.append(pr.title, style="bold")
    if pr.is_draft:
        body.append("  [draft]", style="yellow")
    body.append(f"\nby @{pr.author}", style="dim")
    body.append(f"\n{pr.url}", style="blue underline")
    body.append("\n\n")
    body.append_text(_rollup_banner(pr))

    return Panel(body, title=title, border_style="cyan", padding=(0, 2))


def format_relative(then: datetime | None, now: datetime) -> str:
    """Compact 'time ago' string (pure — `now` is passed in for testability)."""
    if then is None:
        return "—"
    seconds = max(0.0, (now - then).total_seconds())
    minutes, hours = seconds / 60, seconds / 3600
    days, weeks = hours / 24, hours / 168
    if seconds < 60:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    if days < 7:
        return f"{int(days)}d ago"
    if days < 30:
        return f"{int(weeks)}w ago"
    return f"{int(days / 30)}mo ago"


def format_duration(seconds: float) -> str:
    """Compact 'how long' string, e.g. '45s', '3m 20s', '1h 05m' (pure)."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _check_duration(check: Check, now: datetime) -> Text:
    """How long the check has run — live while pending, total once finished."""
    start = check.started_at
    if start is None:
        return Text("—", style="dim")
    if check.state is CheckState.PENDING or check.completed_at is None:
        elapsed = (now - start).total_seconds()
        return Text(format_duration(elapsed), style="yellow")
    took = (check.completed_at - start).total_seconds()
    return Text(format_duration(took), style="dim")


def _review_text(m: PRMetrics) -> Text:
    mapping = {
        "APPROVED": ("✔ approved", "bold green"),
        "CHANGES_REQUESTED": ("✖ changes requested", "bold red"),
        "REVIEW_REQUIRED": ("○ review required", "yellow"),
    }
    label, style = mapping.get(m.review_decision or "", ("— none", "dim"))
    text = Text(label, style=style)
    if m.approvals or m.changes_requested:
        text.append("  ")
        text.append(f"✔{m.approvals}", style="green")
        text.append(" ")
        text.append(f"✖{m.changes_requested}", style="red")
    return text


def _mergeable_text(mergeable: str) -> Text:
    label, style = {
        "MERGEABLE": ("clean", "green"),
        "CONFLICTING": ("conflicts", "bold red"),
    }.get(mergeable, ("checking…", "dim"))
    return Text(label, style=style)


def _metrics_panel(m: PRMetrics, now: datetime) -> Panel:
    diff = Text()
    diff.append(f"+{m.additions}", style="green")
    diff.append(" / ", style="dim")
    diff.append(f"-{m.deletions}", style="red")

    stats: list[tuple[str, Text]] = [
        ("diff", diff),
        ("files", Text(str(m.changed_files))),
        ("commits", Text(str(m.commits))),
        ("opened", Text(format_relative(m.created_at, now))),
        ("updated", Text(format_relative(m.updated_at, now))),
        ("review", _review_text(m)),
        ("merge", _mergeable_text(m.mergeable)),
    ]

    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in stats:
        grid.add_column(justify="center")
    grid.add_row(*(Text(label.upper(), style="dim") for label, _ in stats))
    grid.add_row(*(value for _, value in stats))

    return Panel(grid, title=Text("Metrics"), border_style="magenta", padding=(0, 1))


def _checks_table(checks: list[Check], now: datetime) -> RenderableType:
    if not checks:
        return Panel(
            Align.center(Text("No checks reported yet.", style="dim")),
            title="Checks",
            border_style="dim",
        )

    table = Table(
        expand=True,
        header_style="bold",
        border_style="dim",
        padding=(0, 1),
    )
    table.add_column("", width=3, no_wrap=True)
    table.add_column("Check", ratio=3)
    table.add_column("Elapsed", justify="right", no_wrap=True)
    table.add_column("Result", ratio=1, no_wrap=True)

    # Failures first, then pending, then the rest — most actionable on top.
    order = {
        CheckState.FAILURE: 0,
        CheckState.PENDING: 1,
        CheckState.UNKNOWN: 2,
        CheckState.SUCCESS: 3,
        CheckState.SKIPPED: 4,
    }
    for check in sorted(checks, key=lambda c: (order[c.state], c.name.lower())):
        icon, style = _STATE_STYLE[check.state]
        table.add_row(
            Text(icon, style=style),
            Text(check.name),
            _check_duration(check, now),
            Text((check.detail or check.state.value).lower(), style=style),
        )

    counts = {s: sum(1 for c in checks if c.state == s) for s in CheckState}
    summary = Text()
    for state in (CheckState.SUCCESS, CheckState.FAILURE, CheckState.PENDING):
        if counts[state]:
            icon, style = _STATE_STYLE[state]
            summary.append(f"{icon} {counts[state]}  ", style=style)

    return Panel(
        table,
        title=Text("Checks"),
        subtitle=summary or None,
        border_style="blue",
    )


def _threads_panel(pr: PullRequest) -> Panel:
    if not pr.threads:
        return Panel(
            Align.center(Text("No unresolved review threads 🎉", style="green")),
            title="Open comments",
            border_style="green",
        )

    table = Table(
        expand=True,
        header_style="bold",
        border_style="dim",
        padding=(0, 1),
        show_lines=True,
    )
    table.add_column("Who", style="bold magenta", no_wrap=True)
    table.add_column("Where", style="cyan", no_wrap=True)
    table.add_column("Comment", ratio=1)

    for thread in pr.threads:
        snippet = " ".join(thread.body.split())
        if len(snippet) > 200:
            snippet = snippet[:197] + "…"
        where = Text(thread.location)
        if thread.is_outdated:
            where.append("\n(outdated)", style="dim yellow")
        comment = Text(snippet)
        if thread.comment_count > 1:
            comment.append(f"\n+{thread.comment_count - 1} more", style="dim")
        table.add_row(Text(f"@{thread.author}"), where, comment)

    return Panel(
        table,
        title=Text(f"Open comments ({len(pr.threads)} unresolved)"),
        border_style="yellow",
    )


def _footer(updated: datetime, seconds_to_refresh: int, interval: int) -> Text:
    footer = Text(justify="center")
    footer.append("updated ", style="dim")
    footer.append(updated.strftime("%H:%M:%S"), style="bold")
    footer.append("   ·   ", style="dim")
    footer.append(f"refresh in {seconds_to_refresh:>2}s", style="bold cyan")
    footer.append(f" (every {interval}s)", style="dim")
    footer.append("   ·   ", style="dim")
    footer.append("Ctrl-C to quit", style="dim")
    return footer


def render_pull_request(
    pr: PullRequest,
    ctx: RepoContext,
    updated: datetime,
    seconds_to_refresh: int,
    interval: int,
) -> RenderableType:
    now = datetime.now(timezone.utc)
    return Group(
        _header(pr, ctx),
        _metrics_panel(pr.metrics, now),
        _checks_table(pr.checks, now),
        _threads_panel(pr),
        _footer(updated, seconds_to_refresh, interval),
    )


def render_no_pr(
    ctx: RepoContext,
    updated: datetime,
    seconds_to_refresh: int,
    interval: int,
) -> RenderableType:
    body = Text(justify="center")
    body.append(f"No open PR for branch ", style="dim")
    body.append(ctx.branch, style="bold")
    body.append(f" in {ctx.name_with_owner}.\n\n", style="dim")
    body.append("Waiting — this will update automatically when a PR is opened.", style="dim italic")
    return Group(
        Panel(body, title="pr-watch", border_style="cyan", padding=(1, 2)),
        _footer(updated, seconds_to_refresh, interval),
    )


def render_error(message: str, updated: datetime, seconds_to_refresh: int, interval: int) -> RenderableType:
    return Group(
        Panel(
            Text(message, style="red"),
            title="Error (retrying)",
            border_style="red",
            padding=(1, 2),
        ),
        _footer(updated, seconds_to_refresh, interval),
    )
