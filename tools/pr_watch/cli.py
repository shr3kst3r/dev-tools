"""Entry point for `pr-watch`.

Runs in a specific directory, finds the open PR for that repo's current branch,
and refreshes a live full-screen view every N seconds (default 30).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live

from . import ui
from .github import GitHubError, fetch_pull_request, get_repo_context
from .models import PullRequest, RepoContext


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pr-watch",
        description="Live-watch the GitHub PR for a directory's current branch.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Repo directory to watch (default: current directory).",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=30,
        help="Seconds between refreshes (default: 30).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render a single snapshot and exit (no live refresh).",
    )
    return parser.parse_args(argv)


def _render(
    pr: PullRequest | None,
    error: str | None,
    ctx: RepoContext,
    seconds_left: int,
    interval: int,
):
    now = datetime.now()
    if error is not None:
        return ui.render_error(error, now, seconds_left, interval)
    if pr is None:
        return ui.render_no_pr(ctx, now, seconds_left, interval)
    return ui.render_pull_request(pr, ctx, now, seconds_left, interval)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        console.print(f"[red]Not a directory:[/red] {directory}")
        return 2

    interval = max(5, args.interval)

    try:
        ctx = get_repo_context(directory)
    except GitHubError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    def poll() -> tuple[PullRequest | None, str | None]:
        try:
            return fetch_pull_request(ctx, directory), None
        except GitHubError as exc:
            return None, str(exc)

    if args.once:
        pr, error = poll()
        console.print(_render(pr, error, ctx, interval, interval))
        return 1 if error else 0

    try:
        with Live(
            console=console,
            screen=True,
            auto_refresh=False,
            transient=True,
        ) as live:
            while True:
                pr, error = poll()
                # Count down second-by-second so the UI feels alive between polls.
                for remaining in range(interval, 0, -1):
                    live.update(
                        _render(pr, error, ctx, remaining, interval), refresh=True
                    )
                    time.sleep(1)
    except KeyboardInterrupt:
        console.print("[dim]pr-watch stopped.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
