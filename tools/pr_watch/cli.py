"""Entry point for `pr-watch`.

Runs in a specific directory, finds the open PR for that repo's current branch,
and shows a live view refreshing every N seconds (default 30). The live view is
a Textual app with a scrollable body, so long check lists and comment threads
can be scrolled instead of being cropped by the terminal height.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group

from . import ui
from .app import PrWatchApp
from .github import GitHubError, fetch_pull_request, get_repo_context
from .models import PullRequest


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pr-watch",
        description="Live-watch the GitHub PR for a directory's current branch.",
    )
    # When launched via an spg-installed `~/bin` wrapper, the wrapper cd's into
    # this repo before running the tool, so a bare "." would resolve here rather
    # than where the user actually is. spg exports $SPG_INVOCATION_DIR (the
    # caller's directory) for exactly this — fall back to it, then to ".".
    default_directory = os.environ.get("SPG_INVOCATION_DIR") or "."
    parser.add_argument(
        "directory",
        nargs="?",
        default=default_directory,
        help="Repo directory to watch (default: the directory you ran this from).",
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
        console.print(
            Group(
                ui.render_body(pr, error, ctx),
                ui.render_footer(datetime.now(), interval, interval),
            )
        )
        return 1 if error else 0

    try:
        PrWatchApp(ctx=ctx, poll=poll, interval=interval).run()
    except KeyboardInterrupt:
        pass
    console.print("[dim]pr-watch stopped.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
