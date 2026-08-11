"""Entry point for `my-issues`.

Shows a live dashboard of your open GitHub issues updated in the last two weeks
(configurable), across all repos: labels, assignees, comment counts, and how long
ago. Four views, with `v` cycling between them: the issues assigned to you, the
ones you filed, the ones that mention you, and the ones you've hidden with `h`
because they're not yours to care about. Every list is sorted by most recently
updated — there is no attention dot, deliberately. The live view is a Textual
master/detail app: an issue list window on the left, a scrollable detail window
on the right.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from rich.console import Console

from . import hidden as hidden_state
from . import layout as layout_state
from . import ui
from .app import MyIssuesApp, PollResult
from .github import (
    GitHubError,
    classify_github_error,
    fetch_all_views,
    require_gh,
)
from .models import VIEWS, partition_hidden, sort_items


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="my-issues",
        description="Live dashboard of your recent GitHub issues across all repos.",
    )
    parser.add_argument(
        "-d",
        "--days",
        type=int,
        default=14,
        help="How far back to look for issue activity (default: 14 days).",
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
        help="Maximum issues to show per view (default: 50, GitHub caps at 100).",
    )
    # Not `--author`: this login fills assignee:, author: *and* mentions:, so
    # naming it after only one of the three would be actively misleading.
    parser.add_argument(
        "--user",
        default="@me",
        help="GitHub login to search issues for (default: you).",
    )
    parser.add_argument(
        "--view",
        choices=VIEWS,
        default=None,
        help="Which view to open with: issues assigned to you (assigned), "
        "issues you filed (created), issues mentioning you (mentioned), or the "
        "issues you've hidden (hidden). Default: the view you last had open.",
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
            views = fetch_all_views(days=days, limit=limit, user=args.user)
            data = {view: sort_items(items) for view, items in views.items()}
            return data, None
        except GitHubError as exc:
            return None, classify_github_error(exc)

    if args.once:
        view = args.view or VIEWS[0]
        data, error = poll()
        if data is None:
            console.print(f"[red]{error.message if error else 'poll failed'}[/red]")
            return 1
        # The snapshot honors the hide list too — the same issues are kept out,
        # and `--view hidden` is how you print what's on it.
        hidden = hidden_state.load(hidden_state.state_path())
        items = partition_hidden(data, hidden)[view]
        console.print(
            ui.render_once(items, datetime.now(timezone.utc), view, hidden)
        )
        return 0

    try:
        MyIssuesApp(
            poll=poll,
            interval=interval,
            layout_path=layout_state.state_path(),
            initial_view=args.view,
            hidden_path=hidden_state.state_path(),
        ).run()
    except KeyboardInterrupt:
        pass
    console.print("[dim]my-issues stopped.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
