"""The my-prs TUI, built on Textual.

Windowing: a master/detail split. The left window is a DataTable of every
recent PR (cursor keys / mouse to select); the detail pane shows the selected
PR exactly as pr-watch would render it — checks, unresolved threads, metrics.
`d` cycles the detail pane through right of the list, below it, or hidden.
`?` floats a keybinding reference over the dashboard. A summary bar is docked
at the top, the refresh status bar at the bottom.

Rendering stays in ui.py (list cells, summary) and pr_watch.ui (detail pane)
as pure Rich renderables; this module only owns the polling loop, the
selection state, and the layout.
"""

from __future__ import annotations

import webbrowser
from datetime import datetime, timezone
from typing import Callable

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from tools.pr_watch import ui as pr_ui

from . import ui
from .models import PrItem

# What one poll of GitHub yields: (items, None) on success or (None, error
# message) on failure.
PollResult = tuple[list[PrItem] | None, str | None]

# Where the detail pane lives, in the order `d` cycles through.
DETAIL_MODES = ("right", "below", "hidden")


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


class MyPrsApp(App[None]):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "poll_now", "Refresh now"),
        ("o", "open_pr", "Open in browser"),
        ("d", "cycle_detail", "Move/hide detail"),
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

    def __init__(self, poll: Callable[[], PollResult], interval: int) -> None:
        super().__init__()
        self._poll = poll
        self._interval = interval
        self._seconds_left = interval
        self._items: list[PrItem] | None = None  # None until the first poll lands
        self._error: str | None = None
        self._selected_key: str | None = None
        self._updated = datetime.now()
        self._polling = False
        self._detail_mode = DETAIL_MODES[0]

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
        for column in ui.LIST_COLUMNS:
            table.add_column(column, key=column)
        table.focus()
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
        items, error = self._poll()
        self.call_from_thread(self._apply_poll, items, error)

    def _apply_poll(self, items: list[PrItem] | None, error: str | None) -> None:
        self._error = error
        if items is not None:  # keep showing the last good list through errors
            self._items = items
        self._updated = datetime.now()
        self._seconds_left = self._interval
        self._polling = False
        self._rebuild_table()
        self._refresh_view()

    def _tick(self) -> None:
        if not self._polling:
            self._seconds_left -= 1
            if self._seconds_left <= 0:
                self.action_poll_now()
                return  # action_poll_now already refreshed the view
        # Re-render even between polls so pending-check timers stay live.
        self._refresh_view()

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
            table.add_row(*ui.list_row(item, now), key=item.key)
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

    # --- detail pane placement ----------------------------------------------

    def action_cycle_detail(self) -> None:
        index = DETAIL_MODES.index(self._detail_mode)
        self._detail_mode = DETAIL_MODES[(index + 1) % len(DETAIL_MODES)]
        body = self.query_one("#body")
        body.set_class(self._detail_mode == "below", "detail-below")
        body.set_class(self._detail_mode == "hidden", "detail-hidden")
        if self._detail_mode == "hidden":
            # A hidden pane can't keep focus; hand it back to the list.
            self.query_one(DataTable).focus()

    # --- help ----------------------------------------------------------------

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    # --- rendering ---------------------------------------------------------

    def _refresh_view(self) -> None:
        loading = self._items is None and self._error is None
        self.query_one("#summary", Static).update(
            ui.render_summary(self._items, self._error)
        )
        item = self._selected_item()
        if item is not None:
            detail = pr_ui.render_body(item.pr, None, item.ctx)
        else:
            detail = ui.render_detail_placeholder(
                self._items, self._error, loading=loading
            )
        self.query_one("#detail", Static).update(detail)
        self.query_one("#status", Static).update(
            pr_ui.render_footer(
                self._updated,
                max(0, self._seconds_left),
                self._interval,
                refreshing=self._polling,
                quit_hint="q quit · r refresh · ↑↓ select · enter/o open · ? help",
            )
        )
