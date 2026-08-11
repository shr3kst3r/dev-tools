"""The my-issues TUI, built on Textual.

Four views share the dashboard — the issues assigned to you ("assigned"), the
ones you filed ("created"), the ones that mention you ("mentioned"), and the ones
you've hidden ("hidden") — and `v` cycles between them. Every poll fetches the
first three in one request, so switching is instant, and each view remembers its
own selection.

Every list is sorted by most recently updated and nothing else. There is no
attention dot and no "needs you" column anywhere in this app — GitHub exposes no
fact about an issue that reliably means that, and a dot that is only sometimes
right is worse than none. See
`docs/adrs/2026-08-11-issues-get-no-attention-dot.md` before adding one.

`h` hides the selected issue: it leaves whichever list it was in and turns up in
the hidden view, where `h` puts it back. The hide list is persisted (see
hidden.py) when the app is given a `hidden_path`, so an issue you're not
interested in stays out of the way across restarts. Hiding is a local mute and
nothing else — nothing is changed on GitHub — and because the hidden view is
derived from the same poll, both directions take effect without a refresh.

Windowing: a master/detail split. The left window is a DataTable of every recent
issue (cursor keys / mouse to select); the detail pane shows the selected issue's
header, labels/assignees/milestone, its markdown body, and the tail of its
comment thread. `d` cycles the detail pane through right of the list, below it,
or hidden; `[` / `]` move the divider to resize the two windows. These and the
active view are remembered in a state file (see layout.py) when the app is given
a `layout_path`, so the dashboard reopens the way you left it. `?` floats a
keybinding reference over the dashboard — the full key list lives there, not in
the bottom bar — and `l` floats a live activity log of every background poll —
its issue counts, and any rate-limit backoffs or failures. A summary bar is
docked at the top; the status bar at the bottom shows the refresh timing plus the
last 10 GitHub requests as a strip of dots (green success, red failure, blue
still in flight).

`g` hands the selected issue to goblin-watcher: the app suspends itself and runs
`gw new --issue <url>` on the real terminal (gw needs it to attach tmux when run
outside a session). If gw reports that a task already exists, a confirm dialog
offers to re-run with --rm; any other failure lands in the activity log and a
toast.

Polling is resilient: all searched views are fetched in one request, errors are
shown as concise one-liners (never a raw command dump), and a rate limit triggers
an exponential backoff instead of hammering the API on the normal cadence.

Rendering stays in ui.py as pure Rich renderables; this module only owns the
polling loop, the selection state, and the layout.
"""

from __future__ import annotations

import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, cast

from textual.app import App, ComposeResult, SuspendNotSupported
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from . import gw
from . import hidden as hidden_state
from . import layout as layout_state
from . import ui
from .github import PollError
from .layout import DETAIL_MODES, SPLIT_STEP, Layout
from .models import SOURCE_VIEWS, VIEWS, IssueItem, LogEntry, partition_hidden

# What one poll of GitHub yields: ({view: items}, None) on success — every
# searched view's list in one poll, so switching views never waits on the
# network — or (None, PollError) on failure.
ViewData = dict[str, list[IssueItem]]
PollResult = tuple[ViewData | None, PollError | None]

# When GitHub reports a rate limit, we stop polling on the normal cadence and
# back off exponentially — doubling from the base interval up to this ceiling —
# so the dashboard never turns a rate limit into a worse one. A successful poll
# resets the backoff immediately.
MAX_BACKOFF_SECONDS = 900

# How many activity-log lines to keep in memory (a rolling tail).
MAX_LOG_ENTRIES = 200

# How many recent GitHub requests the status bar shows as dots.
POLL_HISTORY_LIMIT = 10


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
        app = cast("MyIssuesApp", self.app)
        self.query_one("#log-content", Static).update(ui.render_log(app.activity_log))

    def action_dismiss_log(self) -> None:
        self.dismiss()


class GwRmScreen(ModalScreen[bool]):
    """The confirm dialog when a gw task for the issue already exists:
    dismisses True to re-run `gw new` with --rm, False to leave it alone."""

    BINDINGS = [
        ("y", "confirm", "Remove & recreate"),
        ("n,escape", "cancel", "Keep"),
    ]

    CSS = """
    GwRmScreen {
        align: center middle;
    }
    GwRmScreen #gw-exists {
        width: auto;
        max-width: 80;
        height: auto;
    }
    """

    def __init__(self, existing: gw.ExistingTask) -> None:
        super().__init__()
        self._existing = existing

    def compose(self) -> ComposeResult:
        yield Static(
            ui.render_gw_exists(self._existing.task_id, self._existing.project),
            id="gw-exists",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class MyIssuesApp(App[None]):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("v", "switch_view", "Switch view"),
        ("h", "toggle_hidden", "Hide / unhide"),
        ("r", "poll_now", "Refresh now"),
        ("o", "open_issue", "Open in browser"),
        ("g", "open_in_gw", "Open in gw"),
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
        hidden_path: Path | None = None,
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
        # The lists the UI actually shows: `_data` with the hidden issues moved
        # out into their own view. Re-derived on every poll and on every `h`.
        self._views: ViewData | None = None
        self._error: PollError | None = None
        # A rolling log of what each background poll did, for the `l` overlay.
        self._activity_log: list[LogEntry] = []
        # The last few GitHub requests, oldest first, for the status bar's
        # dots: "running" while in flight, then settled to "ok" or "error".
        self._poll_history: list[str] = []
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
        # The hide list, on the same terms as the layout: persisted only when
        # the caller supplies a path, in memory otherwise.
        self._hidden_path = hidden_path
        self._hidden = hidden_state.load(hidden_path) if hidden_path else {}

    @property
    def activity_log(self) -> list[LogEntry]:
        """The activity log, read by the `l` overlay (LogScreen.refresh_log).

        Not named `log`: that's a reserved Textual `App` property (the logger).
        """
        return self._activity_log

    @property
    def _items(self) -> list[IssueItem] | None:
        return None if self._views is None else self._views.get(self._view)

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
        # The request goes on the status bar's dot strip as in-flight;
        # _apply_poll settles it to ok/error when the worker lands.
        self._poll_history.append("running")
        del self._poll_history[:-POLL_HISTORY_LIMIT]
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
            self._derive_views()
        self._updated = datetime.now()
        self._current_delay = self._delay_after(error)
        self._seconds_left = self._current_delay
        self._polling = False
        if self._poll_history and self._poll_history[-1] == "running":
            self._poll_history[-1] = "ok" if error is None else "error"
        self._record_poll(error)
        self._rebuild_table()
        self._refresh_view()

    def _derive_views(self) -> None:
        """Re-split the last poll into the lists the UI shows. Cheap and pure,
        so pressing `h` gets the same result a fresh poll would."""
        if self._data is not None:
            self._views = partition_hidden(self._data, self._hidden)

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

    def _record_poll(self, error: PollError | None) -> None:
        """Append one activity-log line summarizing this poll's outcome.

        The counts are the *shown* lists, so a poll that returned nothing new
        to look at because you hid it reads that way.
        """
        if error is None:
            counts = " · ".join(
                f"{len((self._views or {}).get(view, []))} {view}" for view in VIEWS
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
        # Re-render even between polls so the relative timestamps stay live.
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

    def _selected_item(self) -> IssueItem | None:
        for item in self._items or []:
            if item.key == self._selected_key:
                return item
        return None

    def _rebuild_table(self) -> None:
        """Repopulate the list, keeping the cursor on the same issue if it's
        still present (otherwise fall back to the top row)."""
        table = self.query_one(DataTable)
        table.clear()
        items = self._items or []
        now = datetime.now(timezone.utc)
        for item in items:
            row = ui.list_row(item, now, self._view, self._hidden.get(item.key))
            table.add_row(*row, key=item.key)
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
        self.action_open_issue()  # Enter on a row opens it in the browser

    def action_open_issue(self) -> None:
        item = self._selected_item()
        if item is not None and item.issue.url:
            webbrowser.open(item.issue.url)

    # --- hiding --------------------------------------------------------------

    def action_toggle_hidden(self) -> None:
        """`h`: hide the selected issue, or unhide it if it's already hidden.

        The issue leaves the current list either way, so the cursor is handed to
        its neighbour, and the views it lands in are pre-selected on it — flip
        over with `v` and it's the row under the cursor.
        """
        item = self._selected_item()
        if item is None:
            return
        unhiding = item.key in self._hidden
        if unhiding:
            del self._hidden[item.key]
        else:
            self._hidden[item.key] = datetime.now(timezone.utc)
        if self._hidden_path is not None:
            hidden_state.save(self._hidden, self._hidden_path)

        self._selected_key = self._neighbour_key(item.key)
        self._derive_views()
        for view in self._destination_views(item.key, unhiding=unhiding):
            self._selected[view] = item.key
        self._rebuild_table()
        self._refresh_view()

        verb = "Unhid" if unhiding else "Hid"
        where = "" if unhiding else " — it's in the Hidden view (v)"
        self._append_log("info", f"{verb} {item.key}: {item.issue.title}{where}")

    def _neighbour_key(self, key: str) -> str | None:
        """The row to select once `key` leaves the current list: the one after
        it, or the one before it when it was last."""
        keys = [item.key for item in self._items or []]
        if key not in keys:
            return self._selected_key
        index = keys.index(key)
        rest = keys[index + 1 :] or keys[:index][-1:]
        return rest[0] if rest else None

    def _destination_views(self, key: str, *, unhiding: bool) -> list[str]:
        """Where a just-(un)hidden issue went, so those views can point at it."""
        if not unhiding:
            return ["hidden"]
        return [
            view
            for view in SOURCE_VIEWS
            if any(item.key == key for item in (self._views or {}).get(view, []))
        ]

    # --- goblin-watcher hand-off ---------------------------------------------

    def action_open_in_gw(self) -> None:
        item = self._selected_item()
        if item is not None and item.issue.url:
            self._launch_gw(item, rm=False)

    def _launch_gw(self, item: IssueItem, *, rm: bool) -> None:
        """Hand the issue to `gw new --issue`, then classify the outcome. The app
        is suspended for the duration: gw prints its progress to the terminal,
        and outside tmux it execs `tmux attach`, which needs the real tty."""
        result = self._run_suspended(lambda: gw.run_new(item.issue.url, rm=rm))
        if result.ok:
            flags = " --rm" if rm else ""
            self._append_log(
                "info", f"gw: created task for {item.key} (gw new --issue{flags})"
            )
            return
        if result.exists is not None and not rm:
            existing = result.exists

            def answer(remove: bool | None) -> None:
                if remove:
                    self._launch_gw(item, rm=True)
                else:
                    self._append_log(
                        "info", f"gw: kept existing task {existing.task_id!r}"
                    )

            self.push_screen(GwRmScreen(existing), answer)
            return
        self._append_log("error", f"gw: {result.error}")
        self.notify(result.error or "gw failed", title="gw", severity="error")

    def _run_suspended(self, run: Callable[[], gw.GwLaunch]) -> gw.GwLaunch:
        """Run with the terminal handed back to the shell. Headless drivers
        (tests) can't suspend; there the subprocess needs no tty anyway."""
        try:
            with self.suspend():
                return run()
        except SuspendNotSupported:
            return run()

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
            ui.render_summary(
                self._items, error_message, self._view, len(self._hidden)
            )
        )
        item = self._selected_item()
        if item is not None:
            detail = ui.render_body(item, datetime.now(timezone.utc))
        else:
            detail = ui.render_detail_placeholder(
                self._items, error_message, loading=loading, view=self._view
            )
        self.query_one("#detail", Static).update(detail)
        self.query_one("#status", Static).update(
            ui.render_status_bar(
                self._updated,
                max(0, self._seconds_left),
                self._current_delay,
                self._poll_history,
                refreshing=self._polling,
            )
        )
