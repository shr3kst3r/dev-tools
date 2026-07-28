"""The airflow-watch TUI, built on Textual.

The spine is the investigation loop, and the app is shaped around it: a list of
recent DAG runs across every DAG on the left, and a detail pane that drills
`run → task instances → log` on the right. `enter` goes in a level, `escape`
comes back out, `<`/`>` step through a task's log attempts. The runs list is
not a fixed window: moving the cursor near its bottom widens the poll's run
window by a page, so older runs stream in as you scroll back — see
`_maybe_extend_runs`. `i` hands the selected run to goblin-watcher: a worker
gathers the run's metadata and task logs into a report file, then `gw scratch`
is launched (app suspended — gw needs the real terminal) with a prompt to
summarize it, change nothing, and wait.

Windowing follows my-prs: `d` cycles the detail pane through right of the list,
below it, or hidden; `[` / `]` move the divider. Under the detail pane sits a
chart strip — an in-flight chart counting how many runs (or, drilled in, task
instances) were running at once, stacked on top of an activity chart of the
same rows bucketed over time and coloured by state — which `g` shows or
hides. All of those and the selected deployment persist in a state file (see
layout.py) when the app is given a `layout_path`. A menu bar sits above the
summary line: click a category title (App, Runs, Tasks, View) — or press `M` —
and its drop-down lists every command in that group with its direct key,
`←`/`→` sliding between categories. It is the one menu, and the complete map:
the footer stopped trying to name every key long ago. `?` floats a keybinding
reference, `l` a live activity log of every poll and every action, `e` the DAG
import errors, and `D` a deployment switcher. Stale DAGs are hidden from the DAGs view by default
(`s` shows them); the summary bar always counts them. `R` narrows both
top-level views to what is running right now — runs in state "running", DAGs
with a run in flight — and the summary bar says so while it is on. `w` marks
the selected run as watched (a yellow ★ in the runs list); the third view in
the `v` cycle shows only watched runs, and `W` clears the list. The watch list
is session state, like `s` and `R` — deliberately not persisted, and dropped
on a deployment switch because run keys only mean anything within one
deployment.

Polling is resilient in the same way my-prs is: errors show as concise
one-liners (never a raw command dump), the last good run list stays on screen
through a failure, and a rate limit triggers exponential backoff instead of
hammering a webserver humans are also using.

Every mutating action — pause, trigger, clear, mark — goes through a
confirmation modal that names its target and offers a dry run, and lands in the
activity log. Nothing here talks to Astro directly: the poll, the drill-down
fetches, and the action executor are all injected callables, which is what keeps
this module free of I/O and testable through `run_test()`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, cast

from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, OptionList, Static
from textual.widgets.option_list import Option

from tools.pr_watch import ui as pr_ui

from . import layout as layout_state
from . import ui
from .astro import PollError, RunTasks
from .investigate import Investigation
from .layout import DETAIL_MODES, SPLIT_STEP, Layout
from .models import (
    FILTER_TARGETS,
    Action,
    Dag,
    DagRun,
    Deployment,
    Drill,
    LogEntry,
    PollRequest,
    Snapshot,
    TaskInstance,
    TaskLog,
    TaskRow,
    live_import_error_files,
    matches,
)

# What one poll yields: a Snapshot on success, or a PollError on failure. The
# request names the deployment to target (None on the very first poll, when the
# app has no deployment yet and the caller picks the default) and carries the DAG
# filter, which the caller may push server-side.
PollResult = tuple[Snapshot | None, PollError | None]
Poll = Callable[[PollRequest], PollResult]

# The drill-down fetches, injected for the same reason the poll is: they are
# subprocess calls that must stay off this module and out of tests.
FetchTasks = Callable[[Deployment, DagRun], tuple[RunTasks | None, PollError | None]]
FetchLog = Callable[
    [Deployment, DagRun, TaskInstance, int], tuple[TaskLog | None, PollError | None]
]

# Runs a confirmed action, returning the line to log or an error. Separate from
# the poll so a mutation can never happen as a side effect of refreshing.
Perform = Callable[[Deployment, Action], tuple[str | None, PollError | None]]

# The `i` hand-off, in two injected halves. `PrepareInvestigation` gathers one
# run's metadata and task logs into a report file — many subprocess calls, so
# it runs in a worker exactly like the poll. `LaunchInvestigation` runs
# `gw scratch` on the prepared report, returning (message, error) one-liners
# for the activity log; the app suspends itself around it because gw needs the
# real terminal.
PrepareInvestigation = Callable[
    [Deployment, DagRun, Dag | None], tuple[Investigation | None, PollError | None]
]
LaunchInvestigation = Callable[[Investigation], tuple[str | None, str | None]]

# When Airflow reports a rate limit we back off exponentially — doubling from
# the base interval up to this ceiling — rather than turning a rate limit into a
# worse one. A successful poll resets it immediately.
MAX_BACKOFF_SECONDS = 900

# How many activity-log lines to keep in memory (a rolling tail).
MAX_LOG_ENTRIES = 200

# How the runs list grows when scrolled: nearing the bottom (within the margin)
# asks the next poll for one more step of older runs. The step matches the
# server's page size, so each extension costs about one extra `astro` call per
# poll; the margin makes the loading start a little before the wall is hit.
RUNS_EXTEND_STEP = 100
RUNS_EXTEND_MARGIN = 5

# Which states `m` can mark a task instance as. Both are terminal states a human
# sets deliberately when they know better than the scheduler does.
MARK_STATES = ("success", "failed")


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
    """The `l` overlay: a scrollable, live activity log of polls and actions."""

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
        """Re-render from the app's log — called on open and on every poll, so
        the log stays live while it is on screen."""
        app = cast("AirflowWatchApp", self.app)
        self.query_one("#log-content", Static).update(
            ui.render_activity_log(app.activity_log)
        )

    def action_dismiss_log(self) -> None:
        self.dismiss()


class ImportErrorScreen(ModalScreen[None]):
    """The `e` overlay: which DAG files are failing to parse, and why."""

    BINDINGS = [("escape,q,e", "dismiss_errors", "Close")]

    CSS = """
    ImportErrorScreen {
        align: center middle;
    }
    ImportErrorScreen #errors-box {
        width: 90%;
        height: 80%;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="errors-box"):
            yield Static(id="errors-content")

    def on_mount(self) -> None:
        app = cast("AirflowWatchApp", self.app)
        snapshot = app.snapshot
        self.query_one("#errors-content", Static).update(
            ui.render_import_errors(snapshot.import_errors if snapshot else ())
        )

    def action_dismiss_errors(self) -> None:
        self.dismiss()


class DeploymentScreen(ModalScreen[str | None]):
    """The `D` overlay: pick a deployment by number.

    Dismisses with the chosen deployment key, or None when cancelled.
    """

    BINDINGS = [
        ("escape,q,D", "dismiss_picker", "Close"),
        *[(str(n), f"pick({n})", f"Deployment {n}") for n in range(1, 10)],
    ]

    CSS = """
    DeploymentScreen {
        align: center middle;
    }
    DeploymentScreen #deployments {
        width: auto;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        app = cast("AirflowWatchApp", self.app)
        yield Static(
            ui.render_deployments(app.deployments, app.deployment_key),
            id="deployments",
        )

    def action_pick(self, number: int) -> None:
        app = cast("AirflowWatchApp", self.app)
        deployments = app.deployments
        if 1 <= number <= len(deployments):
            self.dismiss(deployments[number - 1].key)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)


class MenuTitle(Static):
    """One clickable category title in the menu bar.

    Posts `Clicked` rather than opening anything itself: which drop-down to
    show, and where, is the app's decision — the title only knows its index.
    """

    class Clicked(Message):
        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    def __init__(self, title: str, *, index: int, id: str | None = None) -> None:
        super().__init__(title, id=id)
        self._index = index

    def on_click(self) -> None:
        self.post_message(self.Clicked(self._index))


class DropdownScreen(ModalScreen[str | None]):
    """One menu-bar drop-down: a category's commands, floated under its title.

    Dismisses with the chosen action's name (None when cancelled) and the app
    runs it — the drop-down itself changes nothing, so it can be closed and
    reopened without consequence. `←`/`→` dismiss with a navigation sentinel
    instead, so the app can slide to the neighbouring category the way menu
    bars work everywhere else. Clicking anywhere outside the list closes it,
    which is what makes the bar feel like a bar rather than a modal.
    """

    # Dismissal sentinels for `←`/`→`. Colons keep them from ever colliding
    # with an action name — Textual action names cannot contain one.
    PREV = "menu:prev"
    NEXT = "menu:next"

    # `M` is bound here as well as on the app: an open modal's focused list
    # consumes the key before app-level bindings see it, and `M` closing what
    # `M` opened is the behavior a toggle key owes.
    BINDINGS = [
        ("escape,q,M", "dismiss_dropdown", "Close"),
        ("left", "neighbour(-1)", "Previous category"),
        ("right", "neighbour(1)", "Next category"),
    ]

    CSS = """
    DropdownScreen {
        align: left top;
    }
    DropdownScreen #dropdown {
        width: auto;
        max-width: 60;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 0 1;
    }
    """

    def __init__(self, category: ui.MenuCategory, anchor_x: int) -> None:
        super().__init__()
        self._category = category
        # The x column of the category's title in the bar, so the drop-down
        # opens under the title that was clicked rather than in a corner.
        self._anchor_x = anchor_x

    def compose(self) -> ComposeResult:
        menu = OptionList(
            *(
                Option(ui.menu_option(entry), id=entry.action)
                for entry in self._category.entries
            ),
            id="dropdown",
        )
        menu.border_title = self._category.title
        menu.border_subtitle = "← → categories · esc closes"
        yield menu

    def on_mount(self) -> None:
        menu = self.query_one(OptionList)
        # Sit one row down (below the bar itself), under the clicked title.
        menu.styles.margin = (1, 0, 0, self._anchor_x)
        menu.focus()
        menu.highlighted = 0

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(event.option.id)

    def on_click(self, event: events.Click) -> None:
        if not self.query_one(OptionList).region.contains(
            event.screen_x, event.screen_y
        ):
            self.dismiss(None)

    def action_dismiss_dropdown(self) -> None:
        self.dismiss(None)

    def action_neighbour(self, delta: int) -> None:
        self.dismiss(self.PREV if delta < 0 else self.NEXT)


class ConfirmScreen(ModalScreen[bool]):
    """The gate in front of every mutation.

    Dismisses True only on an explicit confirm. The app builds the `Action`
    first — including its `dry_run` flag — so what is confirmed is exactly what
    is sent, and the modal names the target rather than describing it in the
    abstract.
    """

    BINDINGS = [
        ("y,enter", "confirm", "Confirm"),
        ("n,escape,q", "cancel", "Cancel"),
    ]

    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen #confirm {
        width: auto;
        height: auto;
    }
    """

    def __init__(self, action: Action) -> None:
        super().__init__()
        self.action_request = action

    def compose(self) -> ComposeResult:
        yield Static(ui.render_confirm(self.action_request), id="confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class AirflowWatchApp(App[None]):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "poll_now", "Refresh now"),
        ("v", "switch_view", "Switch view"),
        ("slash", "start_filter", "Filter"),
        # `enter` is not bound here on purpose: DataTable owns it and posts
        # RowSelected, which `on_data_table_row_selected` turns into a drill-in.
        ("escape", "escape", "Back / clear filter"),
        ("less_than_sign,comma", "prev_try", "Previous attempt"),
        ("greater_than_sign,full_stop", "next_try", "Next attempt"),
        ("D", "switch_deployment", "Switch deployment"),
        ("e", "show_import_errors", "Import errors"),
        ("s", "toggle_stale", "Show/hide stale DAGs"),
        ("R", "toggle_running", "Only running"),
        ("w", "toggle_watch", "Watch/unwatch run"),
        ("W", "clear_watched", "Clear watched runs"),
        ("M", "open_menu_bar", "Menu bar"),
        ("p", "toggle_pause", "Pause/unpause DAG"),
        ("t", "trigger_run", "Trigger run"),
        ("c", "clear_tasks", "Clear task"),
        ("m", "mark_tasks", "Mark task state"),
        ("i", "investigate", "Summarize run in gw"),
        ("d", "cycle_detail", "Move/hide detail"),
        ("g", "toggle_chart", "Show/hide chart"),
        ("left_square_bracket", "shrink_list", "Shrink list window"),
        ("right_square_bracket", "grow_list", "Grow list window"),
        ("l", "toggle_log", "Activity log"),
        ("question_mark", "help", "Help"),
    ]

    CSS = """
    /* The menu bar and the summary line share one docked header: two widgets
       docked to the same edge overlap rather than stack, so the container is
       what owns the dock and the rows sit inside it. */
    #header {
        dock: top;
        height: 2;
    }
    #menubar {
        height: 1;
        background: $boost;
    }
    #menubar MenuTitle {
        width: auto;
        padding: 0 2;
    }
    #menubar MenuTitle:hover {
        background: $accent 50%;
        text-style: bold;
    }
    #summary {
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
    #detail-pane {
        layout: vertical;
        width: 1fr;
        border-left: solid $foreground 30%;
    }
    #detail-scroll {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    #charts {
        height: 18;
    }
    #chart, #in-flight-chart {
        height: 9;
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
    #body.detail-below #detail-pane {
        width: 1fr;
        height: 1fr;
        border-left: none;
        border-top: solid $foreground 30%;
        /* Below the list the pane is height-starved: an 18-row chart stack
           under the detail would leave the Run pane nothing. Lay the two out
           side by side instead — detail left, charts right. */
        layout: horizontal;
    }
    #body.detail-below #detail-scroll {
        width: 1fr;
        height: 1fr;
    }
    #body.detail-below #charts {
        width: 50%;
        height: 1fr;
        max-height: 18;
    }
    #body.detail-hidden #list {
        width: 1fr;
    }
    #body.detail-hidden #detail-pane {
        display: none;
    }
    """

    def __init__(
        self,
        poll: Poll,
        interval: int,
        *,
        fetch_tasks: FetchTasks | None = None,
        fetch_log: FetchLog | None = None,
        perform: Perform | None = None,
        investigate: PrepareInvestigation | None = None,
        launch: LaunchInvestigation | None = None,
        layout_path: Path | None = None,
        deployment: str | None = None,
    ) -> None:
        super().__init__()
        self._poll = poll
        self._fetch_tasks = fetch_tasks
        self._fetch_log = fetch_log
        self._perform = perform
        self._investigate = investigate
        self._launch = launch
        self._interval = interval
        self._seconds_left = interval
        # The delay currently counting down: the base interval, or a longer
        # backoff after a rate limit. Shown in the footer and reset each poll.
        self._current_delay = interval
        self._rate_limit_streak = 0
        self._snapshot: Snapshot | None = None  # None until the first poll lands
        self._error: PollError | None = None
        self._activity_log: list[LogEntry] = []
        self._view = ui.VIEWS[0]
        # Each list keeps its own cursor, so flipping views lands where you were.
        self._selected: dict[str, str | None] = {view: None for view in ui.VIEWS}
        self._task_key: str | None = None
        self._drill = Drill()
        # Each filter target keeps its own query, so narrowing the runs list and
        # then narrowing a log do not fight each other. `_filtering` is the
        # target currently being typed into, or None.
        # Named `_queries`, not `_filters`: Textual's `App` already owns
        # `_filters` (its line-filter list) and shadowing it breaks rendering.
        self._queries: dict[str, str] = {target: "" for target in FILTER_TARGETS}
        self._filtering: str | None = None
        # Whether the DAGs view shows stale DAGs. Deliberately not persisted:
        # "hidden by default" is the promise, and `s` is one keystroke.
        self._show_stale = False
        # Whether both top-level views are narrowed to what is running right
        # now (`R`). One flag shared by both views — "show me what's in
        # flight" is a question about the deployment, not about one list.
        # Not persisted for the same reason `_show_stale` is not.
        self._only_running = False
        # The run keys `w` has marked as watched; the Watched view shows only
        # these. Session state like `_show_stale`, and dropped on a deployment
        # switch — a run key only means anything within one deployment.
        self._watched: set[str] = set()
        # How many runs the poll should fetch, once scrolling has grown it past
        # the caller's --limit. None until then, so the caller's default rules;
        # never shrunk within a deployment, so a refresh cannot cut a list the
        # user scrolled to see.
        self._wanted_runs: int | None = None
        # Whether the "run list is at its paging ceiling" line has been logged.
        # Once is information; once per keystroke at the bottom would be spam.
        self._runs_ceiling_noted = False
        # True from a scroll-triggered extension until its poll lands — the
        # bottom bar shows a loading notice for exactly that window.
        self._extending = False
        # Whether an `i` gather is currently running; one at a time, because a
        # gather is dozens of subprocess calls.
        self._investigating = False
        self._updated = datetime.now()
        self._polling = False
        # Bumped whenever the *target* of a poll changes (a deployment switch).
        # A poll carries the epoch it was started under, so a result that lands
        # after the target moved can be dropped instead of showing one
        # deployment's runs under another's name.
        self._target_epoch = 0
        # Set when a refresh was asked for while one was already running, so the
        # request is honoured when that one lands rather than dropped.
        self._poll_again = False
        # Layout persists across runs only when the caller supplies a path;
        # without one (tests, embedding) the app starts from defaults and never
        # touches the filesystem.
        self._layout_path = layout_path
        saved = layout_state.load(layout_path) if layout_path else Layout()
        self._detail_mode = saved.detail_mode
        self._split = saved.split
        self._chart_shown = saved.chart
        # An explicit --deployment wins over the remembered one.
        self._wanted_deployment = deployment or saved.deployment or None

    # --- read-only views, for the overlays ---------------------------------

    @property
    def activity_log(self) -> list[LogEntry]:
        """The activity log, read by the `l` overlay.

        Not named `log`: that's a reserved Textual `App` property (the logger).
        """
        return self._activity_log

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    @property
    def deployments(self) -> tuple[Deployment, ...]:
        return self._snapshot.deployments if self._snapshot is not None else ()

    @property
    def deployment(self) -> Deployment | None:
        return self._snapshot.deployment if self._snapshot is not None else None

    @property
    def deployment_key(self) -> str:
        deployment = self.deployment
        return deployment.key if deployment is not None else (self._wanted_deployment or "")

    @property
    def _runs(self) -> tuple[DagRun, ...]:
        return self._snapshot.runs if self._snapshot is not None else ()

    @property
    def _selected_key(self) -> str | None:
        return self._selected[self._view]

    @_selected_key.setter
    def _selected_key(self, value: str | None) -> None:
        self._selected[self._view] = value

    # --- filtering ---------------------------------------------------------
    #
    # Filters are applied client-side to rows already loaded, so a keystroke
    # costs no API call. The DAG filter is *additionally* pushed server-side via
    # `dag_id_pattern` when the DAG list was truncated, because there a
    # client-side filter would be searching an incomplete list.

    @property
    def _filter_target(self) -> str:
        """Which list `/` narrows right now: whatever is on screen."""
        if self._drill.level == "log":
            return "log"
        if self._drill.level == "tasks":
            return "tasks"
        return self._view

    def _query(self, target: str | None = None) -> str:
        return self._queries[target or self._filter_target]

    def visible_runs(self) -> tuple[DagRun, ...]:
        """The run rows the current run-shaped view shows: the `/` filter, the
        `R` narrowing, and — in the Watched view — only the runs `w` marked."""
        watched_only = self._view == "watched"
        query = self._queries["watched" if watched_only else "runs"]
        return tuple(
            run
            for run in self._runs
            if (not watched_only or run.key in self._watched)
            and (not self._only_running or run.state == "running")
            and matches(query, run.search_text)
        )

    def _watched_in_window(self) -> tuple[DagRun, ...]:
        """The watched runs the loaded run window still holds. Anything watched
        but absent here has aged out of the poll's window — the summary bar
        counts those out loud, because the Watched view cannot show them."""
        return tuple(run for run in self._runs if run.key in self._watched)

    def visible_dags(self) -> tuple[Dag, ...]:
        """The DAG rows the list shows: the `/` filter, and — unless `s` has
        shown them — stale DAGs dropped, narrowed to DAGs with a run in flight
        while `R` is on. Hiding is view-side only: the snapshot keeps every
        stale DAG, so the summary count stays real and showing them costs no
        fetch."""
        dags = self._snapshot.dags if self._snapshot is not None else ()
        query = self._queries["dags"]
        running = (
            self._snapshot.running_counts()
            if self._only_running and self._snapshot is not None
            else None
        )
        return tuple(
            dag
            for dag in dags
            if (self._show_stale or not dag.is_stale)
            and (running is None or running.get(dag.dag_id, 0) > 0)
            and matches(query, dag.search_text)
        )

    def visible_rows(self) -> tuple[TaskRow, ...]:
        query = self._queries["tasks"]
        return tuple(
            row for row in self._drill.rows if matches(query, row.task.search_text)
        )

    @property
    def _dag_pattern(self) -> str:
        """The DAG filter to push server-side, if pushing it is warranted.

        Only when the DAG list is *known* to have come back truncated: below that,
        the whole list is already loaded and filtering it client-side is both
        instant and free. With no snapshot yet we do not know, so we do not push —
        guessing would filter the first poll of a deployment by a query left over
        from the last one.
        """
        if self._snapshot is None or not self._snapshot.dags_truncated:
            return ""
        return self._queries["dags"]

    def compose(self) -> ComposeResult:
        # The menu bar: one clickable title per category, above the summary
        # line. The titles are fixed; the drop-down's entries are built at open
        # time so toggle labels reflect the state they would change.
        with Vertical(id="header"):
            with Horizontal(id="menubar"):
                for index, category in enumerate(ui.menu_categories()):
                    yield MenuTitle(
                        category.title, index=index, id=f"menu-title-{index}"
                    )
            yield Static(id="summary")
        with Container(id="body"):
            yield DataTable(id="list")
            with Container(id="detail-pane"):
                with VerticalScroll(id="detail-scroll"):
                    yield Static(id="detail")
                # The chart strip: the in-flight chart stacked on top of the
                # activity chart, so the two share one time axis and read as
                # views of the same window. Under the detail in "right" mode;
                # beside it in "below" mode (see the detail-below CSS).
                with Container(id="charts"):
                    yield Static(id="in-flight-chart")
                    yield Static(id="chart")
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
            # One poll costs several process spawns and several seconds, so a
            # second one is not started on top of it. It is remembered instead:
            # `r`, a deployment switch and a server-side DAG filter all mean the
            # poll in flight is answering the wrong question, and dropping the
            # request outright would leave that answer on screen.
            self._poll_again = True
            return
        self._poll_again = False
        self._polling = True
        self._refresh_view()
        # Each poll spawns several `astro` processes; keep them off the UI thread.
        self.run_worker(self._poll_in_thread, thread=True, exclusive=True)

    def _poll_in_thread(self) -> None:
        epoch = self._target_epoch
        request = PollRequest(
            deployment=self._target_deployment(),
            dag_pattern=self._dag_pattern,
            run_limit=self._wanted_runs,
        )
        snapshot, error = self._poll(request)
        self.call_from_thread(self._apply_poll, snapshot, error, epoch)

    def _target_deployment(self) -> Deployment | None:
        """Which deployment the next poll should read.

        The current one once we have a snapshot; otherwise the one named on the
        command line or remembered from last time, resolved by the caller's poll
        closure — the app never resolves names itself.
        """
        current = self.deployment
        if current is not None:
            return current
        wanted = self._wanted_deployment
        if wanted is None:
            return None
        return Deployment(id=wanted, name=wanted)

    def _apply_poll(
        self, snapshot: Snapshot | None, error: PollError | None, epoch: int = 0
    ) -> None:
        self._polling = False
        if epoch != self._target_epoch:
            # The deployment was switched while this poll was in flight. Its
            # result describes a target the user has moved off, and adopting it
            # would also make it the target of the *next* poll — silently
            # undoing the switch. Drop it and refresh the real one.
            self._append_log(
                "info", "Discarded a refresh of the previously selected deployment."
            )
            self._poll_again = True  # the heartbeat starts the replacement
            return
        self._error = error
        self._extending = False  # whatever poll lands, the wait is over
        if snapshot is not None:  # keep the last good list through errors
            self._snapshot = snapshot
        self._updated = datetime.now()
        self._current_delay = self._delay_after(error)
        self._seconds_left = self._current_delay
        self._record_poll(snapshot, error)
        self._rebuild_table()
        self._refresh_view()

    def _delay_after(self, error: PollError | None) -> int:
        """Seconds until the next poll.

        Normal polls use the configured interval; a rate limit backs off
        exponentially (capped) so we stop hammering an API that is already
        pushing back. An *unrecoverable* failure — no `astro` binary, a refused
        version, a deployment that does not exist — goes straight to the ceiling:
        the timer cannot fix any of those, and spawning a doomed process every
        minute only hides the message. `r` and a deployment switch still refresh
        immediately.
        """
        if error is not None and error.rate_limited:
            self._rate_limit_streak += 1
            base = error.retry_after or self._interval * 2 ** (
                self._rate_limit_streak - 1
            )
            return min(base, MAX_BACKOFF_SECONDS)
        self._rate_limit_streak = 0
        if error is not None and not error.recoverable:
            return MAX_BACKOFF_SECONDS
        return self._interval

    def _record_poll(self, snapshot: Snapshot | None, error: PollError | None) -> None:
        """Append one activity-log line summarizing this poll.

        Carries the target, the call count, and the wall clock — the three facts
        that distinguish "the deployment is slow" from "the tool is stuck".
        """
        if error is None and snapshot is not None:
            self._append_log(
                "info",
                f"{snapshot.deployment.label} — {len(snapshot.runs)} runs, "
                f"{len(snapshot.dags)} dags, {len(snapshot.import_errors)} import "
                f"errors · {snapshot.calls} calls in {snapshot.elapsed:.2f}s",
            )
        elif error is not None and error.rate_limited:
            self._append_log(
                "warn", f"{error.message} Next try in {self._current_delay}s."
            )
        elif error is not None:
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
            if self._poll_again:
                # A refresh was asked for while the last one was still running.
                # It runs from the heartbeat rather than the instant that poll
                # landed, because that instant is inside the finishing worker's
                # own callback — and starting an exclusive worker there cancels
                # the worker we would be standing in.
                self.action_poll_now()
                return
            self._seconds_left -= 1
            if self._seconds_left <= 0:
                self.action_poll_now()
                return  # action_poll_now already refreshed the view
        # Re-render even between polls so running-task timers stay live — but not
        # the log pane, whose content has no clock in it. Laying out a
        # four-thousand-line log measured ~330ms, and paying that every second
        # for a body that cannot have changed is what makes reading a big log
        # feel like the tool is struggling.
        self._refresh_view(detail=self._drill.level != "log")

    # --- the list ----------------------------------------------------------

    @property
    def _showing_tasks(self) -> bool:
        """Whether the list window currently holds task instances rather than
        runs or DAGs. Levels "tasks" and "log" share the task list, so moving the
        cursor while reading a log switches to that task's log."""
        return self._drill.level in ("tasks", "log")

    def _set_columns(self) -> None:
        """Point the list window's columns at whatever it currently lists.

        Runs, DAGs and task instances all have different columns, so a view or
        level change rebuilds from scratch rather than reusing headers.
        """
        table = self.query_one(DataTable)
        table.clear(columns=True)
        if self._showing_tasks:
            columns = ui.task_columns()
        elif self._view == "dags":
            columns = ui.dag_columns()
        else:
            columns = ui.list_columns()
        for column in columns:
            table.add_column(column, key=column)

    def _selected_run(self) -> DagRun | None:
        if self._showing_tasks and self._drill.run is not None:
            return self._drill.run
        # Each run-shaped view keeps its own cursor; outside one (the DAGs
        # view), the runs list's last selection is the run actions target.
        view = self._view if self._view in ui.RUN_VIEWS else "runs"
        for run in self._runs:
            if run.key == self._selected[view]:
                return run
        return None

    def _selected_dag(self) -> Dag | None:
        """The DAG the cursor is on in the DAGs view (not the selected run's)."""
        for dag in self.visible_dags():
            if dag.key == self._selected["dags"]:
                return dag
        return None

    def _action_dag(self) -> Dag | None:
        """The DAG a pause/trigger action would target, in either view."""
        if self._view == "dags" and not self._showing_tasks:
            return self._selected_dag()
        run = self._selected_run()
        if run is None or self._snapshot is None:
            return None
        return self._snapshot.dag(run.dag_id)

    def _action_dag_id(self) -> str | None:
        dag = self._action_dag()
        if dag is not None:
            return dag.dag_id
        run = self._selected_run()
        return run.dag_id if run is not None else None

    def _selected_task(self) -> TaskInstance | None:
        for row in self.visible_rows():
            if row.task.key == self._task_key:
                return row.task
        return None

    def _rebuild_table(self) -> None:
        """Repopulate the list window for the current view/level, keeping the
        cursor on the same row if it is still present (otherwise the top)."""
        table = self.query_one(DataTable)
        table.clear()
        now = datetime.now(timezone.utc)
        if self._showing_tasks:
            rows = self.visible_rows()
            for row in rows:
                table.add_row(*ui.task_row(row, now), key=row.task.key)
            self._task_key = self._reseat(
                table, [row.task.key for row in rows], self._task_key
            )
        elif self._view == "dags":
            dags = self.visible_dags()
            live_errors = live_import_error_files(
                self._snapshot.import_errors if self._snapshot else ()
            )
            running = (
                self._snapshot.running_counts() if self._snapshot is not None else {}
            )
            for dag in dags:
                table.add_row(
                    *ui.dag_row(
                        dag, now, live_errors, running=running.get(dag.dag_id, 0)
                    ),
                    key=dag.key,
                )
            self._selected_key = self._reseat(
                table, [dag.key for dag in dags], self._selected_key
            )
        else:
            runs = self.visible_runs()
            for run in runs:
                table.add_row(
                    *ui.list_row(run, now, watched=run.key in self._watched),
                    key=run.key,
                )
            self._selected_key = self._reseat(
                table, [run.key for run in runs], self._selected_key
            )

    def _reseat(
        self, table: DataTable, keys: list[str], wanted: str | None
    ) -> str | None:
        """Put the cursor back on `wanted`, or on the first row, or nowhere."""
        if not keys:
            return None
        row = keys.index(wanted) if wanted in keys else 0
        table.move_cursor(row=row)
        return keys[row]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        key = event.row_key.value
        if self._showing_tasks:
            if key == self._task_key:
                return
            self._task_key = key
            if self._drill.level == "log":
                # Following the cursor with the log is the point of keeping the
                # task list on screen while reading one.
                self._show_log_for_selected_task()
                return
        else:
            if key == self._selected_key:
                return
            self._selected_key = key
            # Moving off a run invalidates whatever we drilled into from it.
            self._drill = Drill()
            self._task_key = None
            self._maybe_extend_runs(event.cursor_row)
        self._refresh_view()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_drill_in()  # Enter on a row drills in

    def _maybe_extend_runs(self, cursor_row: int) -> None:
        """Load older runs when the cursor nears the bottom of the runs list.

        Scrolling *is* the request: each time the cursor comes within
        `RUNS_EXTEND_MARGIN` rows of the end, the poll's run window grows by
        `RUNS_EXTEND_STEP` and a refresh is asked for — so history streams in
        as you go back, and every later poll keeps the window scrolling earned.
        Nothing happens when everything the server has is already loaded, while
        the last extension is still undelivered, or once the fetch layer's
        paging ceiling is hit — which is logged once rather than re-attempted
        on every keystroke at the bottom.
        """
        snapshot = self._snapshot
        if self._view != "runs" or self._showing_tasks or snapshot is None:
            return
        if self._polling:
            return
        table = self.query_one(DataTable)
        # Row 0 never triggers: a table rebuild highlights it in passing, and a
        # load the user did not scroll for would start a rebuild → highlight →
        # load cycle that polls forever.
        if cursor_row < max(table.row_count - 1 - RUNS_EXTEND_MARGIN, 1):
            return
        held = len(self._runs)
        if snapshot.runs_total <= held:
            return  # the list already holds every run the server reports
        if snapshot.runs_truncated:
            if not self._runs_ceiling_noted:
                self._runs_ceiling_noted = True
                self._append_log(
                    "warn",
                    f"Run list is at its paging ceiling — showing {held} of "
                    f"{snapshot.runs_total:,} runs.",
                )
            return
        if self._wanted_runs is not None and held < self._wanted_runs:
            # The last extension has not landed (or the server could not fill
            # it); asking again before it does would stack polls, not rows.
            return
        self._wanted_runs = held + RUNS_EXTEND_STEP
        self._extending = True  # the bottom bar shows the notice until it lands
        self._append_log(
            "info",
            f"Scrolled near the bottom — loading older runs ({held} of "
            f"{snapshot.runs_total:,} held).",
        )
        self.action_poll_now()

    # --- drill-down: run -> task instances -> log ---------------------------

    def action_switch_view(self) -> None:
        """Flip the list window between DAG runs and DAGs.

        Only meaningful at the top level — inside a drill-down the list belongs to
        the run you drilled into, so `v` there would be ambiguous.
        """
        if self._showing_tasks:
            return
        index = ui.VIEWS.index(self._view)
        self._view = ui.VIEWS[(index + 1) % len(ui.VIEWS)]
        self._set_columns()
        self._rebuild_table()
        self._refresh_view()

    # --- the `/` filter -----------------------------------------------------

    def action_start_filter(self) -> None:
        self._filtering = self._filter_target
        self._refresh_view()

    def on_key(self, event: events.Key) -> None:
        """Incremental filtering: while `/` is open, keys build the query.

        Handled here rather than with a text input widget so the list keeps focus
        and every keystroke re-filters in place. Escape clears, enter keeps.
        """
        if self._filtering is None:
            return
        target, key = self._filtering, event.key
        if key == "escape":
            self._queries[target] = ""
            self._filtering = None
        elif key == "enter":
            self._filtering = None
        elif key == "backspace":
            self._queries[target] = self._queries[target][:-1]
        elif key == "space":
            self._queries[target] += " "
        elif len(event.character or "") == 1 and (event.character or "").isprintable():
            self._queries[target] += event.character or ""
        else:
            return  # not ours; let the binding run
        event.stop()
        event.prevent_default()
        self._after_filter_change(target)

    def _after_filter_change(self, target: str) -> None:
        if target != "log":
            self._rebuild_table()
        self._refresh_view()
        # A truncated DAG list cannot be filtered correctly client-side, so push
        # the pattern server-side. Only then — otherwise a keystroke would cost a
        # process spawn for no gain.
        if target == "dags" and self._snapshot is not None and self._snapshot.dags_truncated:
            self.action_poll_now()

    def action_escape(self) -> None:
        """One key, two jobs: clear an active filter, else back out a level.

        Clearing first is the right precedence — a filter is the more recent, more
        surprising piece of state to be stuck behind.
        """
        target = self._filter_target
        if self._queries[target]:
            self._queries[target] = ""
            self._filtering = None
            self._after_filter_change(target)
            return
        self._filtering = None
        self._drill_out()

    def action_drill_in(self) -> None:
        run = self._selected_run()
        if self._drill.level == "runs":
            if self._view == "dags":
                self._enter_dags_runs()
                return
            if run is not None:
                self._enter_tasks(run)
        elif self._drill.level == "tasks":
            self._show_log_for_selected_task()

    def _enter_dags_runs(self) -> None:
        """Enter on a DAG jumps to its runs, by pre-filling the runs filter.

        Cheaper and more predictable than a second server-side query: the runs are
        already loaded, and the filter is visible and clearable, so it is obvious
        why the list narrowed.
        """
        dag = self._selected_dag()
        if dag is None:
            return
        self._view = "runs"
        self._queries["runs"] = dag.dag_id
        self._selected["runs"] = None
        self._set_columns()
        self._rebuild_table()
        self._refresh_view()

    def _drill_out(self) -> None:
        """Back out one level, restoring the list window that level owns."""
        if self._drill.level == "log":
            self._drill = Drill(
                level="tasks",
                run=self._drill.run,
                tasks=self._drill.tasks,
                rows=self._drill.rows,
                tasks_total=self._drill.tasks_total,
            )
            self._refresh_view()
            return
        if self._drill.level == "tasks":
            self._drill = Drill()
            self._task_key = None
            self._set_columns()
            self._rebuild_table()
        self._refresh_view()

    def _enter_tasks(self, run: DagRun) -> None:
        deployment = self.deployment
        if deployment is None or self._fetch_tasks is None:
            return
        self._drill = Drill(level="tasks", run=run, loading=True)
        self._task_key = None
        self._set_columns()
        self._rebuild_table()
        self._refresh_view()
        self.run_worker(
            lambda: self._load_tasks(deployment, run), thread=True, group="drill"
        )

    def _load_tasks(self, deployment: Deployment, run: DagRun) -> None:
        assert self._fetch_tasks is not None
        result, error = self._fetch_tasks(deployment, run)
        self.call_from_thread(self._apply_tasks, run, result, error)

    def _apply_tasks(
        self,
        run: DagRun,
        result: RunTasks | None,
        error: PollError | None,
    ) -> None:
        if self._drill.run is None or self._drill.run.key != run.key:
            return  # the user moved on while we were fetching
        if error is not None or result is None:
            message = error.message if error is not None else "No task instances."
            self._drill = Drill(level="tasks", run=run, error=message)
            self._append_log("error", f"{run.dag_id}: {message}")
        else:
            self._drill = Drill(
                level="tasks",
                run=run,
                tasks=result.tasks,
                rows=result.rows,
                tasks_total=result.total,
            )
            visible = self.visible_rows()
            self._task_key = visible[0].task.key if visible else None
            self._rebuild_table()
            if result.truncated:
                self._append_log(
                    "warn",
                    f"{run.dag_id}: showing {len(result.tasks)} of {result.total} "
                    "task instances — list truncated.",
                )
        self._refresh_view()

    def _show_log_for_selected_task(self) -> None:
        deployment = self.deployment
        run, task = self._drill.run, self._selected_task()
        if deployment is None or run is None or task is None:
            return
        if self._fetch_log is None:
            return
        self._load_log(deployment, run, task, max(1, task.try_number))

    def _load_log(
        self, deployment: Deployment, run: DagRun, task: TaskInstance, try_number: int
    ) -> None:
        self._drill = Drill(
            level="log",
            run=run,
            task=task,
            try_number=try_number,
            tasks=self._drill.tasks,
            rows=self._drill.rows,
            tasks_total=self._drill.tasks_total,
            loading=True,
        )
        self._refresh_view()
        self.run_worker(
            lambda: self._fetch_log_in_thread(deployment, run, task, try_number),
            thread=True,
            group="drill",
        )

    def _fetch_log_in_thread(
        self, deployment: Deployment, run: DagRun, task: TaskInstance, try_number: int
    ) -> None:
        assert self._fetch_log is not None
        log, error = self._fetch_log(deployment, run, task, try_number)
        self.call_from_thread(self._apply_log, task, try_number, log, error)

    def _apply_log(
        self,
        task: TaskInstance,
        try_number: int,
        log: TaskLog | None,
        error: PollError | None,
    ) -> None:
        if self._drill.level != "log" or self._drill.task is None:
            return
        if self._drill.task.key != task.key or self._drill.try_number != try_number:
            return  # a later request superseded this one
        if error is not None:
            self._drill = Drill(
                level="log",
                run=self._drill.run,
                task=task,
                try_number=try_number,
                tasks=self._drill.tasks,
                rows=self._drill.rows,
                tasks_total=self._drill.tasks_total,
                error=error.message,
            )
            self._append_log("error", f"{task.task_id} log: {error.message}")
        else:
            self._drill = Drill(
                level="log",
                run=self._drill.run,
                task=task,
                try_number=try_number,
                tasks=self._drill.tasks,
                rows=self._drill.rows,
                tasks_total=self._drill.tasks_total,
                log=log,
            )
        self._refresh_view()

    def action_prev_try(self) -> None:
        self._step_try(-1)

    def action_next_try(self) -> None:
        self._step_try(+1)

    def _step_try(self, delta: int) -> None:
        """Move to the neighbouring log attempt, if there is one.

        Each attempt is its own fetch — the CLI transport cannot stream, so
        paging attempts means re-requesting, which is why the pane shows a
        loading state while it happens.
        """
        drill = self._drill
        deployment = self.deployment
        if drill.level != "log" or drill.task is None or drill.run is None:
            return
        if deployment is None or self._fetch_log is None:
            return
        tries = drill.task.tries
        if drill.try_number not in tries:
            return
        index = tries.index(drill.try_number) + delta
        if not 0 <= index < len(tries):
            return
        self._load_log(deployment, drill.run, drill.task, tries[index])

    # --- deployment switching ----------------------------------------------

    def action_switch_deployment(self) -> None:
        if isinstance(self.screen, DeploymentScreen):
            self.screen.dismiss(None)
            return
        self.push_screen(DeploymentScreen(), self._on_deployment_picked)

    def _on_deployment_picked(self, key: str | None) -> None:
        if key is None or key == self.deployment_key:
            return
        chosen = next((d for d in self.deployments if d.key == key), None)
        if chosen is None:
            return
        # Drop the old deployment's data outright rather than showing one
        # deployment's runs under another's name.
        self._snapshot = None
        self._error = None
        self._selected_key = None
        self._task_key = None
        self._drill = Drill()
        # Watched keys name runs of the old deployment; against the new one
        # they could only ever be stale or, worse, collide.
        self._watched.clear()
        # A run window grown by scrolling was earned against the old
        # deployment's history; the new one starts back at the default.
        self._wanted_runs = None
        self._runs_ceiling_noted = False
        self._extending = False
        self._wanted_deployment = key
        # Any poll already in flight is now about the wrong deployment.
        self._target_epoch += 1
        self._append_log("info", f"Switched to {chosen.label}.")
        self._save_layout()
        self._set_columns()
        self._rebuild_table()
        self.action_poll_now()

    # --- actions (all confirmed first) -------------------------------------

    def action_toggle_pause(self) -> None:
        dag_id = self._action_dag_id()
        if dag_id is None:
            return
        dag = self._action_dag()
        paused = dag.is_paused if dag is not None else False
        self._confirm(Action(kind="unpause" if paused else "pause", dag_id=dag_id))

    def action_trigger_run(self) -> None:
        dag_id = self._action_dag_id()
        if dag_id is None:
            return
        self._confirm(Action(kind="trigger", dag_id=dag_id))

    def action_clear_tasks(self) -> None:
        """Clear (retry) the drilled-into task instance.

        Defaults to a dry run: Airflow's clear endpoint reports what it *would*
        touch rather than touching it, which is the preview the ADR's caution
        asks for. Confirming the dry run then offers the real thing.
        """
        run, task = self._drill.run, self._selected_task()
        if run is None or task is None:
            return
        self._confirm(
            Action(
                kind="clear",
                dag_id=run.dag_id,
                run_id=run.run_id,
                task_ids=(task.task_id,),
                dry_run=True,
            )
        )

    def action_mark_tasks(self) -> None:
        """Mark the drilled-into task instance. A failed task marks success (the
        common "I fixed it downstream" case); anything else marks failed.

        The instance's `map_index` travels with the action: a mapped task is one
        of several instances sharing a task id, and marking it means naming
        which one.
        """
        run, task = self._drill.run, self._selected_task()
        if run is None or task is None:
            return
        state = MARK_STATES[0] if task.failed else MARK_STATES[1]
        self._confirm(
            Action(
                kind="mark",
                dag_id=run.dag_id,
                run_id=run.run_id,
                task_ids=(task.task_id,),
                state=state,
                dry_run=True,
                map_index=task.map_index,
            )
        )

    def _confirm(self, action: Action) -> None:
        """Put an action behind the confirmation modal. Nothing is sent unless
        the modal comes back True."""
        if self.deployment is None or self._perform is None:
            return
        self.push_screen(
            ConfirmScreen(action), lambda ok: self._on_confirmed(action, bool(ok))
        )

    def _on_confirmed(self, action: Action, confirmed: bool) -> None:
        deployment = self.deployment
        if not confirmed:
            self._append_log("info", f"Cancelled: {action.summary}")
            return
        if deployment is None or self._perform is None:
            return
        self.run_worker(
            lambda: self._perform_in_thread(deployment, action),
            thread=True,
            group="action",
        )

    def _perform_in_thread(self, deployment: Deployment, action: Action) -> None:
        assert self._perform is not None
        message, error = self._perform(deployment, action)
        self.call_from_thread(self._apply_action, action, message, error)

    def _apply_action(
        self, action: Action, message: str | None, error: PollError | None
    ) -> None:
        if error is not None:
            self._append_log("error", f"{action.summary} failed — {error.message}")
            self._refresh_view()
            return
        self._append_log("action", message or action.summary)
        if action.dry_run:
            # A dry run changed nothing, so offer the real call with the same
            # target — the preview and the commit are one decision.
            self._confirm(
                Action(
                    kind=action.kind,
                    dag_id=action.dag_id,
                    run_id=action.run_id,
                    task_ids=action.task_ids,
                    state=action.state,
                    dry_run=False,
                    map_index=action.map_index,
                )
            )
            return
        self.action_poll_now()  # a real change makes the current list stale

    # --- the gw hand-off ----------------------------------------------------

    def action_investigate(self) -> None:
        """`i`: hand the selected run to goblin-watcher for an AI summary.

        Two phases. A worker gathers the run's metadata, task instances and
        task logs into a report file — dozens of subprocess calls, so it stays
        off the UI thread like every other fetch. Then `gw scratch` launches on
        the real terminal with the app suspended, seeded with a prompt that
        says: read the report, summarize it, change nothing, wait.

        Read-only from Airflow's point of view, so no confirmation modal — the
        gate every mutation goes through guards Airflow, and this touches it
        with nothing but GETs.
        """
        if self._investigating:
            return  # one gather at a time; each is dozens of calls
        if self._view == "dags" and not self._showing_tasks:
            return  # the DAGs list has no run on screen to hand over
        deployment, run = self.deployment, self._selected_run()
        if deployment is None or run is None:
            return
        if self._investigate is None or self._launch is None:
            return
        dag = self._snapshot.dag(run.dag_id) if self._snapshot is not None else None
        self._investigating = True
        self._append_log(
            "info", f"{run.dag_id}: gathering run metadata and task logs for gw…"
        )
        self.run_worker(
            lambda: self._investigate_in_thread(deployment, run, dag),
            thread=True,
            group="investigate",
        )

    def _investigate_in_thread(
        self, deployment: Deployment, run: DagRun, dag: Dag | None
    ) -> None:
        assert self._investigate is not None
        investigation, error = self._investigate(deployment, run, dag)
        self.call_from_thread(self._apply_investigation, run, investigation, error)

    def _apply_investigation(
        self,
        run: DagRun,
        investigation: Investigation | None,
        error: PollError | None,
    ) -> None:
        self._investigating = False
        if error is not None or investigation is None:
            message = error.message if error is not None else "Nothing gathered."
            self._append_log("error", f"{run.dag_id}: {message}")
            self.notify(message, title="investigate", severity="error")
            return
        self._append_log(
            "info",
            f"{run.dag_id}: report ready — {investigation.logs} logs, "
            f"{investigation.calls} calls in {investigation.elapsed:.1f}s → "
            f"{investigation.path}",
        )
        launch = self._launch
        assert launch is not None
        message, failure = self._run_suspended(lambda: launch(investigation))
        if failure is not None:
            self._append_log("error", f"gw: {failure}")
            self.notify(failure, title="gw", severity="error")
        else:
            self._append_log(
                "action", message or f"gw: opened scratch {investigation.name}."
            )
        self._refresh_view()

    def _run_suspended(
        self, run: Callable[[], tuple[str | None, str | None]]
    ) -> tuple[str | None, str | None]:
        """Run with the terminal handed back to the shell — outside tmux, gw
        execs `tmux attach`, which needs the real tty. Headless drivers (tests)
        cannot suspend; there the callable needs no tty anyway."""
        try:
            with self.suspend():
                return run()
        except SuspendNotSupported:
            return run()

    # --- window layout: detail placement + split size ----------------------

    def action_cycle_detail(self) -> None:
        index = DETAIL_MODES.index(self._detail_mode)
        self._detail_mode = DETAIL_MODES[(index + 1) % len(DETAIL_MODES)]
        self._apply_layout()
        self._save_layout()
        if self._detail_mode == "hidden":
            # A hidden pane can't keep focus; hand it back to the list.
            self.query_one(DataTable).focus()

    def action_toggle_chart(self) -> None:
        self._chart_shown = not self._chart_shown
        self._apply_layout()
        self._save_layout()
        if self._chart_shown:
            self._refresh_view()  # the panes went un-updated while hidden

    def action_toggle_stale(self) -> None:
        """Show or hide stale DAGs in the DAGs view. Hidden by default — a
        stale DAG is one whose file left the bundle, which is history, not
        state — while the summary bar keeps counting them so hidden rows can
        never read as a smaller fleet."""
        if self._view != "dags" or self._showing_tasks:
            return
        self._show_stale = not self._show_stale
        self._append_log(
            "info",
            "Showing stale DAGs." if self._show_stale else "Hiding stale DAGs.",
        )
        self._rebuild_table()
        self._refresh_view()

    def action_toggle_running(self) -> None:
        """Narrow both top-level views to what is running right now, or widen
        them back. In the runs list that is runs in state "running"; in the
        DAGs list, DAGs with a running run in the current window (the same
        derivation the Running column shows). Client-side, like the `/`
        filter — the rows are already loaded — and marked in the summary bar
        so a narrowed list can never read as a short one."""
        if self._showing_tasks:
            return  # the list belongs to a run's tasks; nothing here to narrow
        self._only_running = not self._only_running
        self._append_log(
            "info",
            "Showing only running runs and DAGs."
            if self._only_running
            else "Showing all runs and DAGs.",
        )
        self._rebuild_table()
        self._refresh_view()

    def action_toggle_watch(self) -> None:
        """`w`: mark or unmark the selected run as watched.

        Works wherever a run is on screen — the runs list, the Watched view,
        or inside a drill-down — because "keep an eye on this one" is a thought
        you have while looking at a run, not while standing in a particular
        view. Local state only: nothing is sent to Airflow, so no confirmation.
        """
        if self._view == "dags" and not self._showing_tasks:
            return  # the DAGs list has no run on screen to mark
        run = self._selected_run()
        if run is None:
            return
        if run.key in self._watched:
            self._watched.discard(run.key)
            self._append_log("info", f"Unwatched {run.dag_id} · {run.run_id}.")
        else:
            self._watched.add(run.key)
            self._append_log("info", f"Watching {run.dag_id} · {run.run_id}.")
        self._rebuild_table()
        self._refresh_view()

    def action_clear_watched(self) -> None:
        """`W`: drop every watched run at once.

        No confirmation modal on purpose — the gate exists for Airflow
        mutations, and this is a local list that one `w` per run rebuilds. The
        activity log records the count, so an accidental `W` at least says
        what it cost.
        """
        if not self._watched:
            return
        count = len(self._watched)
        self._watched.clear()
        self._append_log(
            "info", f"Cleared {count} watched run{'s' if count != 1 else ''}."
        )
        self._rebuild_table()
        self._refresh_view()

    # --- the menu bar ---------------------------------------------------------

    def on_menu_title_clicked(self, message: MenuTitle.Clicked) -> None:
        self._open_dropdown(message.index)

    def action_open_menu_bar(self) -> None:
        """`M`: the keyboard path into the menu bar — opens the first category's
        drop-down, from which `←`/`→` reach every other one. (While one is
        open, the screen's own `M` binding closes it.)"""
        if not isinstance(self.screen, DropdownScreen):
            self._open_dropdown(0)

    def _open_dropdown(self, index: int) -> None:
        """Float the given category's drop-down under its title in the bar."""
        categories = ui.menu_categories(
            chart_shown=self._chart_shown,
            stale_shown=self._show_stale,
            running_only=self._only_running,
        )
        index %= len(categories)
        anchor = self.query_one(f"#menu-title-{index}", MenuTitle).region.x
        self.push_screen(
            DropdownScreen(categories[index], anchor),
            lambda result: self._on_dropdown_picked(index, result),
        )

    def _on_dropdown_picked(self, index: int, result: str | None) -> None:
        if result is None:
            return
        # `call_later` for the same re-entrancy reason as `_on_menu_picked`:
        # both branches push a screen from inside a dismiss callback.
        if result == DropdownScreen.PREV:
            self.call_later(self._open_dropdown, index - 1)
        elif result == DropdownScreen.NEXT:
            self.call_later(self._open_dropdown, index + 1)
        else:
            self.call_later(self.run_action, result)

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
        the list window's share of the split."""
        body = self.query_one("#body")
        body.set_class(self._detail_mode == "below", "detail-below")
        body.set_class(self._detail_mode == "hidden", "detail-hidden")
        list_ = self.query_one("#list")
        list_.styles.width = f"{self._split}%" if self._detail_mode == "right" else None
        list_.styles.height = (
            f"{self._split}%" if self._detail_mode == "below" else None
        )
        self.query_one("#charts").display = self._chart_shown

    def _save_layout(self) -> None:
        if self._layout_path is not None:
            layout_state.save(
                Layout(
                    detail_mode=self._detail_mode,
                    split=self._split,
                    deployment=self.deployment_key,
                    chart=self._chart_shown,
                ),
                self._layout_path,
            )

    # --- overlays ----------------------------------------------------------

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_toggle_log(self) -> None:
        if isinstance(self.screen, LogScreen):
            self.screen.dismiss()
        else:
            self.push_screen(LogScreen())

    def action_show_import_errors(self) -> None:
        if isinstance(self.screen, ImportErrorScreen):
            self.screen.dismiss()
        else:
            self.push_screen(ImportErrorScreen())

    # --- rendering ---------------------------------------------------------

    def _shown_count(self) -> int:
        """How many rows the list is displaying, after any filter."""
        if self._view == "dags":
            return len(self.visible_dags())
        return len(self.visible_runs())

    def _refresh_view(self, *, detail: bool = True) -> None:
        """Re-render the three docked regions from current state.

        `detail=False` leaves the detail pane alone — for callers that know its
        inputs cannot have changed. Every state change passes it, so the default
        is to redraw everything.
        """
        try:
            summary = self.query_one("#summary", Static)
        except NoMatches:
            # The 1s tick is the one caller that runs detached from a keystroke,
            # so it can fire while the app is tearing down — after compose's
            # widgets are gone. Nothing to render into, so nothing to do.
            return
        error_message = self._error.message if self._error is not None else None
        summary.update(
            ui.render_summary(
                self._snapshot,
                error_message,
                view=self._view,
                shown=self._shown_count(),
                stale_hidden=not self._show_stale,
                running_only=self._only_running,
                watched_runs=self._watched_in_window(),
                watched_total=len(self._watched),
            )
        )
        now = datetime.now(timezone.utc)
        if self._chart_shown:
            # The charts track the same selection the detail pane does; when
            # hidden they are skipped entirely and repainted on toggle instead.
            self.query_one("#chart", Static).update(
                ui.render_chart(self._drill, self.visible_runs(), now)
            )
            self.query_one("#in-flight-chart", Static).update(
                ui.render_in_flight_chart(self._drill, self.visible_runs(), now)
            )
        if detail:
            self.query_one("#detail", Static).update(
                ui.render_detail(
                    self._drill,
                    self._snapshot,
                    self._selected_run(),
                    self._selected_task(),
                    now,
                    error=error_message,
                    view=self._view,
                    dag=self._selected_dag(),
                    query=self._queries[self._filter_target],
                    shown=self._shown_count(),
                )
            )
        # While `/` is open the status line is the filter prompt; the refresh
        # countdown gives up its row rather than competing for attention.
        if self._filtering is not None:
            self.query_one("#status", Static).update(
                ui.render_filter_prompt(self._filtering, self._queries[self._filtering])
            )
            return
        # While a scroll-triggered extension is fetching, the bottom bar says
        # so — otherwise the seconds it takes read as nothing happening.
        if self._extending:
            self.query_one("#status", Static).update(
                ui.render_loading_older(
                    len(self._runs),
                    self._snapshot.runs_total if self._snapshot is not None else 0,
                )
            )
            return
        self.query_one("#status", Static).update(
            pr_ui.render_footer(
                self._updated,
                max(0, self._seconds_left),
                self._current_delay,
                refreshing=self._polling,
                quit_hint=self._hint(),
            )
        )

    def _hint(self) -> str:
        """The footer's key hint. It once tried to advertise every key for the
        current level and ran out of room; the menu bar now carries that list,
        so the footer keeps only what is stateful — an active filter, which
        would otherwise make a narrowed list indistinguishable from a short
        one — plus the three constants."""
        active = self._queries[self._filter_target]
        prefix = f"/{active} ({self._filter_count()}) esc clears · " if active else ""
        return prefix + "M menu · ? help · q quit"

    def _filter_count(self) -> str:
        """"12/240" — matches over the rows the filter searched."""
        target = self._filter_target
        if target == "log":
            log = self._drill.log
            content = log.content if log is not None else ""
            hits, total = ui.filter_log(content, self._queries["log"])
            return f"{len(hits)}/{total}"
        if target == "tasks":
            return f"{len(self.visible_rows())}/{len(self._drill.rows)}"
        if target == "dags":
            dags = self._snapshot.dags if self._snapshot is not None else ()
            return f"{len(self.visible_dags())}/{len(dags)}"
        if target == "watched":
            return f"{len(self.visible_runs())}/{len(self._watched_in_window())}"
        return f"{len(self.visible_runs())}/{len(self._runs)}"
