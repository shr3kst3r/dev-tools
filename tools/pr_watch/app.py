"""The live pr-watch TUI, built on Textual.

Why Textual instead of rich.Live: Live(screen=True) draws on the alternate
screen, which has no scrollback — a PR with many checks or comment threads
simply lost everything below the fold. Textual gives the body a real scrollable
viewport (scrollbar, mouse wheel, arrows/PgUp/PgDn) while the status bar stays
docked at the bottom.

Rendering stays in ui.py as pure Rich renderables; this module only owns the
polling loop and the viewport.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from . import ui
from .models import PullRequest, RepoContext

# What one poll of GitHub yields: (pr, None) on success — pr is None when the
# branch has no open PR — or (None, error message) on failure.
PollResult = tuple[PullRequest | None, str | None]


class PrWatchApp(App[None]):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "poll_now", "Refresh now"),
    ]

    CSS = """
    VerticalScroll {
        scrollbar-gutter: stable;
    }
    #status {
        dock: bottom;
        height: 1;
    }
    """

    def __init__(
        self,
        ctx: RepoContext,
        poll: Callable[[], PollResult],
        interval: int,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._poll = poll
        self._interval = interval
        self._seconds_left = interval
        self._pr: PullRequest | None = None
        self._error: str | None = None
        self._updated = datetime.now()
        self._loaded = False  # becomes True after the first poll completes
        self._polling = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="body")
        yield Static(id="status")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()
        self._refresh_view()
        self.action_poll_now()
        self.set_interval(1, self._tick)

    def action_poll_now(self) -> None:
        if self._polling:
            return
        self._polling = True
        self._refresh_view()
        # gh runs a subprocess + network call; keep it off the UI thread.
        self.run_worker(self._poll_in_thread, thread=True, exclusive=True)

    def _poll_in_thread(self) -> None:
        pr, error = self._poll()
        self.call_from_thread(self._apply_poll, pr, error)

    def _apply_poll(self, pr: PullRequest | None, error: str | None) -> None:
        self._pr, self._error = pr, error
        self._updated = datetime.now()
        self._seconds_left = self._interval
        self._loaded = True
        self._polling = False
        self._refresh_view()

    def _tick(self) -> None:
        if not self._polling:
            self._seconds_left -= 1
            if self._seconds_left <= 0:
                self.action_poll_now()
                return  # action_poll_now already refreshed the view
        # Re-render even between polls so pending-check timers stay live.
        self._refresh_view()

    def _refresh_view(self) -> None:
        self.query_one("#body", Static).update(
            ui.render_body(self._pr, self._error, self._ctx, loading=not self._loaded)
        )
        self.query_one("#status", Static).update(
            ui.render_footer(
                self._updated,
                max(0, self._seconds_left),
                self._interval,
                refreshing=self._polling,
                quit_hint="q to quit · r to refresh · scroll: wheel/↑↓/PgUp/PgDn",
            )
        )
