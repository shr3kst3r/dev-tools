"""Entry point for `slack-me`.

Sends a message to your own Slack via an incoming webhook read from
``~/.slack-me.toml``. The message comes from positional arguments, or — if none
are given — from stdin, so both of these work::

    slack-me "deploy finished"
    just-a-long-job 2>&1 | slack-me
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import ConfigError, load_config
from .slack import SlackError, post_message


_EPILOG = """\
message source:
  The message is taken from the MESSAGE args, or from stdin if none are given:

    slack-me "deploy finished"
    long-job 2>&1 | slack-me

config:
  ~/.slack-me.toml holds the Slack incoming-webhook URL:

    webhook = "https://hooks.slack.com/services/XXX/YYY/ZZZ"
    # username = "slack-me"   # optional display-name override

  Overrides: $SLACK_ME_WEBHOOK (URL) and $SLACK_ME_CONFIG (config path).

slack formatting (mrkdwn):
  Slack's syntax differs from regular Markdown. What works:

    *bold*                single asterisks (NOT **double**)
    _italic_              underscores
    ~strike~             tildes
    <https://url|text>    links (NOT [text](url))
    `code`                inline code
    ```code block```      triple-backtick blocks

  What does NOT work — these render as literal text:
    # headings, **bold**, [text](url) links, Markdown images, and tables.

  slack-me sends your text in the webhook's `text` field, and Slack
  auto-formats mrkdwn there by default. For anything richer (real headers,
  dividers, fields, buttons), Slack's Block Kit is the recommended route:
  a section block with a {"type": "mrkdwn"} text object — mrkdwn is opt-in
  per text object in blocks, and the header block is plain-text only.
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="slack-me",
        description="Send yourself a Slack message via a configured webhook.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="The message to send. If omitted, read from stdin.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Send silently — no confirmation output (errors still print).",
    )
    return parser.parse_args(argv)


def _resolve_message(args: argparse.Namespace) -> str:
    """Assemble the message from positional args or stdin."""
    if args.message:
        return " ".join(args.message)
    # No positional message: pull from stdin (a pipe or heredoc). If stdin is an
    # interactive tty there's nothing to read, which we treat as "no message".
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()
    err = Console(stderr=True)

    message = _resolve_message(args)
    if not message:
        err.print("[red]slack-me:[/red] nothing to send (no message and empty stdin).")
        return 2

    try:
        config = load_config()
    except ConfigError as exc:
        err.print(f"[red]slack-me:[/red] {exc}")
        return 1

    try:
        post_message(message, config)
    except SlackError as exc:
        err.print(f"[red]slack-me:[/red] {exc}")
        return 1

    if not args.quiet:
        preview = Text(message, overflow="fold")
        console.print(
            Panel(
                preview,
                title="[green]✓ sent to Slack[/green]",
                title_align="left",
                border_style="green",
                expand=False,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
