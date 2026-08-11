"""Entry point for `my-prs`.

Shows a live dashboard of your open GitHub PRs updated in the last two weeks
(configurable), across all repos: check status, unresolved review threads,
and review state — so you know the moment any PR needs you. Three views, with
`v` cycling between them: the PRs you authored, the PRs waiting on a review
from you, and the PRs you've hidden with `h` because they're not yours to care
about. The live view is a Textual master/detail app: a PR list window on the
left, a scrollable detail window on the right.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from rich.console import Console

from . import hidden as hidden_state
from . import layout as layout_state
from . import ui
from .app import MyPrsApp, PollResult
from .github import (
    GitHubError,
    classify_github_error,
    fetch_all_views,
    require_gh,
)
from .models import VIEWS, partition_hidden, sort_items

# Refresh cadence, in seconds. One poll costs ~54 points of GitHub's 5000/hour
# GraphQL budget (see github._PR_FIELDS), so the default spends ~1080/hour and
# leaves room for my-issues and a few pr-watch instances alongside it. The floor
# is what stops `-i 1` from turning a dashboard into a rate limit: even at 30s
# this tool alone is already ~6500 points/hour.
DEFAULT_INTERVAL = 180
MIN_INTERVAL = 30


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
        default=DEFAULT_INTERVAL,
        help=f"Seconds between refreshes (default: {DEFAULT_INTERVAL}, "
        f"minimum: {MIN_INTERVAL}).",
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
        help="Which view to open with: your PRs (mine), PRs awaiting your "
        "review (review), or the PRs you've hidden (hidden). Default: the "
        "view you last had open.",
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
    interval = max(MIN_INTERVAL, args.interval)
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
        data, error = poll()
        if data is None:
            console.print(f"[red]{error.message if error else 'poll failed'}[/red]")
            return 1
        # The snapshot honors the hide list too — the same PRs are kept out,
        # and `--view hidden` is how you print what's on it.
        hidden = hidden_state.load(hidden_state.state_path())
        items = partition_hidden(data, hidden)[view]
        console.print(
            ui.render_once(items, datetime.now(timezone.utc), view, hidden)
        )
        return 0

    try:
        MyPrsApp(
            poll=poll,
            interval=interval,
            layout_path=layout_state.state_path(),
            initial_view=args.view,
            hidden_path=hidden_state.state_path(),
        ).run()
    except KeyboardInterrupt:
        pass
    console.print("[dim]my-prs stopped.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
