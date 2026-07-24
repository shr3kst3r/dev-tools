"""Entry point for `my-prs`.

Shows a live dashboard of your open GitHub PRs updated in the last two weeks
(configurable), across all repos: check status, unresolved review threads,
and review state — so you know the moment any PR needs you. Two views, with
`v` switching between them: the PRs you authored, and the PRs waiting on a
review from you. The live view is a Textual master/detail app: a PR list
window on the left, a scrollable detail window on the right.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from rich.console import Console

from . import ui
from .app import MyPrsApp, PollResult
from .layout import state_path
from .github import (
    GitHubError,
    classify_github_error,
    fetch_all_views,
    fetch_prs,
    require_gh,
)
from .models import VIEWS, sort_items


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="my-prs",
        description="Live dashboard of your recent GitHub PRs across all repos.",
    )
    parser.add_argument(
        "-d",
        "--days",
        type=int,
        default=14,
        help="How far back to look for PR activity (default: 14 days).",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=60,
        help="Seconds between refreshes (default: 60).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum PRs to show (default: 50, GitHub caps at 100).",
    )
    parser.add_argument(
        "--author",
        default="@me",
        help="GitHub login to search PRs for (default: you).",
    )
    parser.add_argument(
        "--view",
        choices=VIEWS,
        default=None,
        help="Which view to open with: your PRs (mine) or PRs awaiting your "
        "review (review). Default: the view you last had open.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render a single snapshot and exit (no live refresh).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()

    days = max(1, args.days)
    interval = max(10, args.interval)
    limit = min(100, max(1, args.limit))

    try:
        require_gh()
    except GitHubError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    def poll() -> PollResult:
        try:
            views = fetch_all_views(days=days, limit=limit, author=args.author)
            data = {view: sort_items(items) for view, items in views.items()}
            return data, None
        except GitHubError as exc:
            return None, classify_github_error(exc)

    if args.once:
        view = args.view or VIEWS[0]
        try:
            items = sort_items(
                fetch_prs(view, days=days, limit=limit, author=args.author)
            )
        except GitHubError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print(ui.render_once(items, datetime.now(timezone.utc), view))
        return 0

    try:
        MyPrsApp(
            poll=poll,
            interval=interval,
            layout_path=state_path(),
            initial_view=args.view,
        ).run()
    except KeyboardInterrupt:
        pass
    console.print("[dim]my-prs stopped.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
