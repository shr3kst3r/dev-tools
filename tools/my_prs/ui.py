"""Rich rendering for the my-prs dashboard.

Everything here is a pure function of the data + a clock value. The list pane
cells and the summary/footer live here; the *detail* pane reuses pr-watch's
`render_body`, so a selected PR looks exactly like `pr-watch` on that branch.
"""

from __future__ import annotations

from datetime import datetime

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tools.pr_watch.models import CheckState
from tools.pr_watch.ui import format_relative

from .models import PrItem

LIST_COLUMNS = ("!", "Repo", "PR", "Title", "CI", "💬", "Review", "Updated")

_TITLE_WIDTH = 44


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


def list_row(item: PrItem, now: datetime) -> tuple[Text, ...]:
    """The cells for one PR row, in LIST_COLUMNS order."""
    return (
        attention_cell(item),
        Text(item.repo_name, style="cyan"),
        Text(f"#{item.pr.number}", style="bold"),
        _title_cell(item),
        ci_cell(item),
        comments_cell(item),
        review_cell(item),
        Text(format_relative(item.pr.metrics.updated_at, now), style="dim"),
    )


def render_summary(items: list[PrItem] | None, error: str | None) -> Text:
    """The one-line counts bar docked at the top of the app."""
    if error is not None:
        return Text(f"✖ {error}", style="bold red")
    if items is None:
        return Text("Contacting GitHub…", style="dim italic")

    summary = Text()
    summary.append("my-prs", style="bold cyan")
    summary.append(f"  ·  {len(items)} open", style="bold")
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


def render_detail_placeholder(
    items: list[PrItem] | None,
    error: str | None,
    *,
    loading: bool = False,
) -> RenderableType:
    """What the detail pane shows when there is no selected PR to render."""
    if loading:
        message = Text("Contacting GitHub…", style="dim italic")
    elif error is not None:
        message = Text(error, style="red")
    elif not items:
        message = Text(
            "No open PRs of yours updated in this window. 🎉", style="green"
        )
    else:
        message = Text("Select a PR on the left.", style="dim")
    return Panel(Align.center(message), title="my-prs", border_style="cyan", padding=(1, 2))


def render_once(items: list[PrItem], now: datetime) -> RenderableType:
    """A single-shot snapshot of the whole list for `--once` / scripting."""
    table = Table(
        expand=True,
        header_style="bold",
        border_style="dim",
        padding=(0, 1),
    )
    for column in LIST_COLUMNS:
        table.add_column(column, no_wrap=column != "Title")
    for item in items:
        table.add_row(*list_row(item, now))
    return Group(render_summary(items, None), table)
