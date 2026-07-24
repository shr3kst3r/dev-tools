"""The my-prs TUI, built on Textual.

Two views share the dashboard — the PRs you authored ("mine") and the PRs
waiting on a review from you ("review") — and `v` switches between them.
Every poll fetches both, so switching is instant, and each view remembers its
own selection.

Windowing: a master/detail split. The left window is a DataTable of every
recent PR (cursor keys / mouse to select); the detail pane shows the selected
PR exactly as pr-watch would render it — checks, unresolved threads, metrics.
`d` cycles the detail pane through right of the list, below it, or hidden;
`[` / `]` move the divider to resize the two windows. These and the active
view are remembered in a state file (see layout.py) when the app is given a
`layout_path`, so the dashboard reopens the way you left it.
`?` floats a keybinding reference over the dashboard, and `l` floats a live
activity log of every background poll — its PR counts, and any rate-limit
backoffs or failures. A summary bar is docked at the top, the refresh status
bar at the bottom.

Polling is resilient: all views are fetched in one request, errors are shown
as concise one-liners (never a raw command dump), and a rate limit triggers an
exponential backoff instead of hammering the API on the normal cadence.

Rendering stays in ui.py (list cells, summary) and pr_watch.ui (detail pane)
as pure Rich renderables; this module only owns the polling loop, the
selection state, and the layout.
"""

from __future__ import annotations

import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, cast

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from tools.pr_watch import ui as pr_ui

from . import layout as layout_state
from . import ui
from .github import PollError
from .layout import DETAIL_MODES, SPLIT_STEP, Layout
from .models import VIEWS, LogEntry, PrItem

# What one poll of GitHub yields: ({view: items}, None) on success — every
# view's list in one poll, so switching views never waits on the network —
# or (None, PollError) on failure.
ViewData = dict[str, list[PrItem]]
PollResult = tuple[ViewData | None, PollError | None]

# When GitHub reports a rate limit, we stop polling on the normal cadence and
# back off exponentially — doubling from the base interval up to this ceiling —
# so the dashboard never turns a rate limit into a worse one. A successful poll
# resets the backoff immediately.
MAX_BACKOFF_SECONDS = 900

# How many activity-log lines to keep in memory (a rolling tail).
MAX_LOG_ENTRIES = 200


class HelpScreen(ModalScreen[None]):
    """The `?` overlay: a keybinding reference floating over the dashboard."""

    BINDINGS = [("escape,q,question_mark", "dismiss_help", "Close")]

    CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen #help {
        width: auto;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(ui.render_help(), id="help")

    def action_dismiss_help(self) -> None:
        self.dismiss()


class LogScreen(ModalScreen[None]):
    """The `l` overlay: a scrollable, live activity log of background polls."""

    BINDINGS = [("escape,q,l", "dismiss_log", "Close")]

    CSS = """
    LogScreen {
        align: center middle;
    }
    LogScreen #log-box {
        width: 80%;
        height: 80%;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="log-box"):
            yield Static(id="log-content")

    def on_mount(self) -> None:
        self.refresh_log()

    def refresh_log(self) -> None:
        """Re-render from the app's log — called on open and on every poll,
        so the log stays live while it's on screen."""
        app = cast("MyPrsApp", self.app)
        self.query_one("#log-content", Static).update(ui.render_log(app.activity_log))

    def action_dismiss_log(self) -> None:
        self.dismiss()


class MyPrsApp(App[None]):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("v", "switch_view", "Switch view"),
        ("r", "poll_now", "Refresh now"),
        ("o", "open_pr", "Open in browser"),
        ("d", "cycle_detail", "Move/hide detail"),
        ("left_square_bracket", "shrink_list", "Shrink list window"),
        ("right_square_bracket", "grow_list", "Grow list window"),
        ("l", "toggle_log", "Activity log"),
        ("question_mark", "help", "Help"),
    ]

    CSS = """
    #summary {
        dock: top;
        height: 1;
        padding: 0 1;
    }
    #status {
        dock: bottom;
        height: 1;
    }
    #body {
        layout: horizontal;
    }
    #list {
        width: 50%;
        min-width: 40;
    }
    #detail-scroll {
        width: 1fr;
        scrollbar-gutter: stable;
        border-left: solid $foreground 30%;
    }
    #body.detail-below {
        layout: vertical;
    }
    #body.detail-below #list {
        width: 1fr;
        min-width: 0;
        height: 50%;
        min-height: 8;
    }
    #body.detail-below #detail-scroll {
        width: 1fr;
        height: 1fr;
        border-left: none;
        border-top: solid $foreground 30%;
    }
    #body.detail-hidden #list {
        width: 1fr;
    }
    #body.detail-hidden #detail-scroll {
        display: none;
    }
    """

    def __init__(
        self,
        poll: Callable[[], PollResult],
        interval: int,
        layout_path: Path | None = None,
        initial_view: str | None = None,
    ) -> None:
        super().__init__()
        self._poll = poll
        self._interval = interval
        self._seconds_left = interval
        # The delay currently counting down: the base interval, or a longer
        # backoff after a rate limit. Shown in the footer and reset each poll.
        self._current_delay = interval
        self._rate_limit_streak = 0
        self._data: ViewData | None = None  # None until the first poll lands
        self._error: PollError | None = None
        # A rolling log of what each background poll did, for the `l` overlay.
        self._activity_log: list[LogEntry] = []
        # Each view keeps its own cursor, so flipping back lands where you were.
        self._selected: dict[str, str | None] = {view: None for view in VIEWS}
        self._updated = datetime.now()
        self._polling = False
        # Layout persists across runs only when the caller supplies a path;
        # without one (tests, embedding) the app starts from defaults and
        # never touches the filesystem.
        self._layout_path = layout_path
        saved = layout_state.load(layout_path) if layout_path else Layout()
        self._detail_mode = saved.detail_mode
        self._split = saved.split
        self._view = initial_view if initial_view in VIEWS else saved.view

    @property
    def activity_log(self) -> list[LogEntry]:
        """The activity log, read by the `l` overlay (LogScreen.refresh_log).

        Not named `log`: that's a reserved Textual `App` property (the logger).
        """
        return self._activity_log

    @property
    def _items(self) -> list[PrItem] | None:
        return None if self._data is None else self._data.get(self._view)

    @property
    def _selected_key(self) -> str | None:
        return self._selected[self._view]

    @_selected_key.setter
    def _selected_key(self, value: str | None) -> None:
        self._selected[self._view] = value

    def compose(self) -> ComposeResult:
        yield Static(id="summary")
        with Container(id="body"):
            yield DataTable(id="list")
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail")
        yield Static(id="status")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._set_columns()
        table.focus()
        self._apply_layout()
        self._refresh_view()
        self.action_poll_now()
        self.set_interval(1, self._tick)

    # --- polling -----------------------------------------------------------

    def action_poll_now(self) -> None:
        if self._polling:
            return
        self._polling = True
        self._refresh_view()
        # gh runs a subprocess + network call; keep it off the UI thread.
        self.run_worker(self._poll_in_thread, thread=True, exclusive=True)

    def _poll_in_thread(self) -> None:
        data, error = self._poll()
        self.call_from_thread(self._apply_poll, data, error)

    def _apply_poll(self, data: ViewData | None, error: PollError | None) -> None:
        self._error = error
        if data is not None:  # keep showing the last good lists through errors
            self._data = data
        self._updated = datetime.now()
        self._current_delay = self._delay_after(error)
        self._seconds_left = self._current_delay
        self._polling = False
        self._record_poll(data, error)
        self._rebuild_table()
        self._refresh_view()

    def _delay_after(self, error: PollError | None) -> int:
        """Seconds until the next poll. Normal polls use the configured
        interval; a rate limit backs off exponentially (capped) so we stop
        hammering an API that's already pushing back. Anything else resets."""
        if error is not None and error.rate_limited:
            self._rate_limit_streak += 1
            base = error.retry_after or self._interval * 2 ** (
                self._rate_limit_streak - 1
            )
            return min(base, MAX_BACKOFF_SECONDS)
        self._rate_limit_streak = 0
        return self._interval

    def _record_poll(self, data: ViewData | None, error: PollError | None) -> None:
        """Append one activity-log line summarizing this poll's outcome."""
        if error is None:
            counts = " · ".join(
                f"{len((data or {}).get(view, []))} {view}" for view in VIEWS
            )
            self._append_log("info", f"Refreshed — {counts}")
        elif error.rate_limited:
            self._append_log(
                "warn", f"{error.message} Next try in {self._current_delay}s."
            )
        else:
            self._append_log("error", error.message)

    def _append_log(self, level: str, message: str) -> None:
        self._activity_log.append(
            LogEntry(time=datetime.now(), level=level, message=message)
        )
        if len(self._activity_log) > MAX_LOG_ENTRIES:
            del self._activity_log[: -MAX_LOG_ENTRIES]
        if isinstance(self.screen, LogScreen):
            self.screen.refresh_log()  # keep an open log overlay live

    def _tick(self) -> None:
        if not self._polling:
            self._seconds_left -= 1
            if self._seconds_left <= 0:
                self.action_poll_now()
                return  # action_poll_now already refreshed the view
        # Re-render even between polls so pending-check timers stay live.
        self._refresh_view()

    # --- views ---------------------------------------------------------------

    def action_switch_view(self) -> None:
        index = VIEWS.index(self._view)
        self._view = VIEWS[(index + 1) % len(VIEWS)]
        self._set_columns()  # the views' columns differ, so rebuild from scratch
        self._rebuild_table()
        self._refresh_view()
        self._save_layout()

    def _set_columns(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        for column in ui.list_columns(self._view):
            table.add_column(column, key=column)

    # --- selection ---------------------------------------------------------

    def _selected_item(self) -> PrItem | None:
        for item in self._items or []:
            if item.key == self._selected_key:
                return item
        return None

    def _rebuild_table(self) -> None:
        """Repopulate the list, keeping the cursor on the same PR if it's
        still present (otherwise fall back to the top row)."""
        table = self.query_one(DataTable)
        table.clear()
        items = self._items or []
        now = datetime.now(timezone.utc)
        for item in items:
            table.add_row(*ui.list_row(item, now, self._view), key=item.key)
        if items:
            keys = [item.key for item in items]
            row = keys.index(self._selected_key) if self._selected_key in keys else 0
            self._selected_key = keys[row]
            table.move_cursor(row=row)
        else:
            self._selected_key = None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._selected_key = event.row_key.value
            self._refresh_view()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_pr()  # Enter on a row opens it in the browser

    def action_open_pr(self) -> None:
        item = self._selected_item()
        if item is not None and item.pr.url:
            webbrowser.open(item.pr.url)

    # --- window layout: detail placement + split size ------------------------

    def action_cycle_detail(self) -> None:
        index = DETAIL_MODES.index(self._detail_mode)
        self._detail_mode = DETAIL_MODES[(index + 1) % len(DETAIL_MODES)]
        self._apply_layout()
        self._save_layout()
        if self._detail_mode == "hidden":
            # A hidden pane can't keep focus; hand it back to the list.
            self.query_one(DataTable).focus()

    def action_grow_list(self) -> None:
        self._resize_split(+SPLIT_STEP)

    def action_shrink_list(self) -> None:
        self._resize_split(-SPLIT_STEP)

    def _resize_split(self, delta: int) -> None:
        if self._detail_mode == "hidden":
            return  # one window fills the screen; nothing to divide
        self._split = layout_state.clamp_split(self._split + delta)
        self._apply_layout()
        self._save_layout()

    def _apply_layout(self) -> None:
        """Make the widgets match the layout state: the body's mode class and
        the list window's share of the split. The split overrides the CSS
        default (50%) inline, on whichever axis the current mode divides."""
        body = self.query_one("#body")
        body.set_class(self._detail_mode == "below", "detail-below")
        body.set_class(self._detail_mode == "hidden", "detail-hidden")
        list_ = self.query_one("#list")
        list_.styles.width = f"{self._split}%" if self._detail_mode == "right" else None
        list_.styles.height = f"{self._split}%" if self._detail_mode == "below" else None

    def _save_layout(self) -> None:
        if self._layout_path is not None:
            layout_state.save(
                Layout(
                    detail_mode=self._detail_mode,
                    split=self._split,
                    view=self._view,
                ),
                self._layout_path,
            )

    # --- help ----------------------------------------------------------------

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_toggle_log(self) -> None:
        if isinstance(self.screen, LogScreen):
            self.screen.dismiss()
        else:
            self.push_screen(LogScreen())

    # --- rendering ---------------------------------------------------------

    def _refresh_view(self) -> None:
        loading = self._items is None and self._error is None
        error_message = self._error.message if self._error is not None else None
        self.query_one("#summary", Static).update(
            ui.render_summary(self._items, error_message, self._view)
        )
        item = self._selected_item()
        if item is not None:
            detail = pr_ui.render_body(item.pr, None, item.ctx)
        else:
            detail = ui.render_detail_placeholder(
                self._items, error_message, loading=loading, view=self._view
            )
        self.query_one("#detail", Static).update(detail)
        self.query_one("#status", Static).update(
            pr_ui.render_footer(
                self._updated,
                max(0, self._seconds_left),
                self._current_delay,
                refreshing=self._polling,
                quit_hint="q quit · v view · r refresh · o open · l log · [ ] resize · ? help",
            )
        )
