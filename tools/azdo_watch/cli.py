"""Entry point for `azdo-watch`.

A live monitor for an Azure DevOps project's pipelines, built around the
investigation loop: see what is running, see what failed, drill into the step that
broke, read its log.

This module owns the command line, the clamping, the preflight, and the closures
that wire the app to the `az` transport — so `app.py` stays free of I/O and
`azdo.py` stays free of UI. `--once` prints a snapshot and returns before the App is
ever constructed.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from datetime import datetime, timezone

from rich.console import Console

from . import api, investigate as investigate_mod, ui
from .app import AzdoWatchApp, Poll, PollResult
from .azdo import (
    AzdoError,
    PollError,
    RunTimeline,
    classify_azdo_error,
    default_org,
    default_project,
    fetch_log,
    fetch_run_bundle,
    fetch_run_timeline,
    fetch_snapshot,
    list_projects,
    org_name,
    perform,
    require_az,
    resolve_project,
)
from .layout import state_path
from .models import (
    Action,
    Pipeline,
    PipelineList,
    PollRequest,
    Project,
    Record,
    Run,
    RunLog,
    Snapshot,
)

# Each `az devops invoke` starts a Python interpreter and loads an extension, and a
# poll is several of them against an API humans are also using; Azure DevOps rate
# limits by a per-user resource quota, so restraint is ours to impose. 20s is the
# floor regardless of what is asked for.
MIN_INTERVAL = 20
DEFAULT_INTERVAL = 60

# How many runs a poll fetches when --limit is not given. One call: paging here is
# serial (see `api`'s module docstring), so a single large `$top` is strictly cheaper
# than several small pages for the same rows.
DEFAULT_RUN_LIMIT = api.DEFAULT_LIMIT

# How long a fetched pipeline inventory stays good. The inventory is the expensive
# half of a poll — 58 pipelines with their latest builds measured ~4.9s and just
# under a megabyte, against ~3s for the runs — while it only changes when someone
# edits a pipeline. The *runs* are what change constantly, and the freshest last-run
# for each pipeline is reconciled out of the runs window on every poll
# (`Snapshot.latest_run_for`), so a cached inventory never shows a stale outcome.
PIPELINE_CACHE_SECONDS = 300

# Return codes: 0 ok, 1 user-actionable failure, 2 bad invocation (argparse's own
# exit code for a usage error).
EXIT_OK = 0
EXIT_ERROR = 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="azdo-watch",
        description=(
            "Live dashboard of an Azure DevOps project's pipelines: what is "
            "running, what failed, its stages and jobs, and their logs."
        ),
    )
    parser.add_argument(
        "-o",
        "--org",
        default=None,
        help=(
            "Azure DevOps organization — a name (`example-org`) or its URL (default: the "
            "one `az devops configure --defaults organization=…` is set to)."
        ),
    )
    parser.add_argument(
        "-p",
        "--project",
        default=None,
        help=(
            "Project name or id to watch (default: the one you last had open, else "
            "`az devops configure`'s project, else the first one visible)."
        ),
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=(
            f"Seconds between refreshes (default: {DEFAULT_INTERVAL}, minimum "
            f"{MIN_INTERVAL})."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RUN_LIMIT,
        help=(
            f"Runs to fetch per refresh (default: {DEFAULT_RUN_LIMIT}). Scrolling to "
            "the bottom of the runs list loads older runs past this. Runs currently "
            "in flight are fetched separately and are always shown, however old they "
            "are, so the list can hold more than this. Azure DevOps reports no total, "
            "so the summary bar says 'more available' rather than 'N of M'."
        ),
    )
    parser.add_argument(
        "--state",
        action="append",
        default=None,
        metavar="STATE",
        help=(
            "Only fetch runs with this azdo *status*; repeatable. One of "
            "inProgress, notStarted, completed, cancelling, postponed, none. Setting "
            "it also turns off the separate in-flight fetch, since an explicit "
            "filter is you saying which states you want. (The `R` key filters the "
            "loaded list by the folded state — succeeded, failed, running — which is "
            "a different and usually better question.)"
        ),
    )
    parser.add_argument(
        "--view",
        # The Watched view is deliberately absent: the watch list is session state
        # inside the live app, so a one-shot snapshot has nothing to show.
        choices=[view for view in ui.VIEWS if view != "watched"],
        default=ui.VIEWS[0],
        help="Which list `--once` prints: runs, or pipelines. Default: runs.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render a single snapshot and exit (no live refresh).",
    )
    return parser.parse_args(argv)


class _PipelineCache:
    """Per-project pipeline inventories with a TTL, owned by the poll closure.

    Deliberately here rather than in `azdo.py`: how often to refetch is a policy
    decision about the shape of one tool's refresh loop, not a property of the
    transport.

    A whole `PipelineList` is cached, not just the pipelines: the count and the
    truncation flag are what let the header say "N of M", and a cache that dropped
    them would make a truncated list look complete for the rest of the TTL — and
    would silently disable the server-side name filter that truncation is what makes
    necessary.
    """

    def __init__(self, ttl: float = PIPELINE_CACHE_SECONDS) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, PipelineList]] = {}

    def get(self, key: str, now: float) -> PipelineList | None:
        entry = self._entries.get(key)
        if entry is None or now - entry[0] > self._ttl:
            return None
        return entry[1]

    def put(self, key: str, pipelines: PipelineList, now: float) -> None:
        self._entries[key] = (now, pipelines)

    def drop(self, key: str) -> None:
        self._entries.pop(key, None)


def _states(args: argparse.Namespace) -> tuple[str, ...]:
    """The requested status filter. Deliberately not validated against a closed set:
    Azure DevOps may know a status this build does not."""
    return tuple(state for state in (args.state or []) if state)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()

    interval = max(MIN_INTERVAL, args.interval)
    # No upper clamp on the limit: a request for more than one page is honoured by
    # walking continuation tokens, and the summary bar says "more available" when a
    # list we could not fill was cut short.
    limit = max(1, args.limit)
    states = _states(args)
    pipeline_cache = _PipelineCache()

    try:
        require_az()
    except AzdoError as exc:
        # The preflight's own message is already user-facing and carries the install
        # command; printing it directly keeps that instruction intact.
        console.print(f"[red]{exc}[/red]")
        return EXIT_ERROR

    org = org_name(args.org or "") or default_org()
    if not org:
        console.print(
            "[red]No Azure DevOps organization known — pass --org, or run "
            "`az devops configure --defaults organization=https://dev.azure.com/"
            "<org>`.[/red]"
        )
        return EXIT_ERROR
    # `az devops configure`'s project is a fallback for the *first* launch only: once
    # the app has remembered a project, that is what the user last chose.
    wanted_project = args.project or None

    def choose(projects: list[Project], wanted: Project | None) -> Project:
        """Which of the discovered projects to read.

        A key the app already holds — the current selection, or the one restored from
        the state file — is tried first. If it has vanished (deleted project, stale
        state file) we fall back rather than refusing to start; an explicit
        `--project` that matches nothing still raises, because a typo the user just
        typed should be reported, not silently ignored.
        """
        if wanted is not None:
            try:
                return resolve_project(projects, wanted.key)
            except AzdoError:
                pass
        if args.project:
            return resolve_project(projects, args.project)
        # No explicit flag: fall back to the configured default, then to the first.
        configured = default_project() or None
        try:
            return resolve_project(projects, configured)
        except AzdoError:
            return resolve_project(projects, None)

    def poll(request: PollRequest) -> PollResult:
        """One refresh: discovery, then the fan-out over the chosen project.

        Discovery runs every poll because it is one call and it is where the project
        list comes from — the switcher depends on it staying current.

        `request.run_limit` is set once scrolling to the bottom of the runs list has
        grown the run window past `--limit` — it only ever widens the fetch, so a
        stale request can never shrink the list the user scrolled for.
        """
        try:
            projects = list_projects(org)
            chosen = choose(projects, request.project)
            now = time.monotonic()
            snapshot = fetch_snapshot(
                chosen,
                limit=max(limit, request.run_limit or 0),
                states=states,
                projects=projects,
                pipelines=pipeline_cache.get(chosen.key, now),
            )
            pipeline_cache.put(
                chosen.key,
                PipelineList(
                    pipelines=snapshot.pipelines,
                    total=snapshot.pipelines_total,
                    truncated=snapshot.pipelines_truncated,
                ),
                now,
            )
            # Discovery is one extra call on top of the fan-out; report it so the
            # activity log's call count matches what actually ran.
            return _with_discovery_cost(snapshot), None
        except (AzdoError, api.AzdoApiError) as exc:
            return None, classify_azdo_error(exc)

    def load_timeline(
        project: Project, run: Run
    ) -> tuple[RunTimeline | None, PollError | None]:
        try:
            return fetch_run_timeline(project, run), None
        except (AzdoError, api.AzdoApiError) as exc:
            return None, classify_azdo_error(exc)

    def load_log(
        project: Project, run: Run, record: Record
    ) -> tuple[RunLog | None, PollError | None]:
        try:
            return fetch_log(project, run, record), None
        except (AzdoError, api.AzdoApiError) as exc:
            return None, classify_azdo_error(exc)

    def run_action(
        project: Project, action: Action
    ) -> tuple[str | None, PollError | None]:
        try:
            line = perform(project, action)
        except (AzdoError, api.AzdoApiError) as exc:
            return None, classify_azdo_error(exc)
        # A queue creates a run and a cancel changes one, so the cached inventory's
        # `latestBuild` is now behind. The app re-polls immediately after a real
        # action; dropping the cache makes that poll tell the truth.
        pipeline_cache.drop(project.key)
        return line, None

    def prepare_investigation(
        project: Project, run: Run, pipeline: Pipeline | None
    ) -> tuple[investigate_mod.Investigation | None, PollError | None]:
        """Gather one run's timeline, issues and logs into the report `gw` reads."""
        try:
            bundle = fetch_run_bundle(project, run)
        except (AzdoError, api.AzdoApiError) as exc:
            return None, classify_azdo_error(exc)
        return (
            investigate_mod.prepare(
                project, run, pipeline, bundle, datetime.now(timezone.utc)
            ),
            None,
        )

    if args.once:
        return _once(console, poll, view=args.view)

    try:
        AzdoWatchApp(
            poll=poll,
            interval=interval,
            fetch_timeline=load_timeline,
            fetch_log=load_log,
            perform=run_action,
            investigate=prepare_investigation,
            launch=investigate_mod.run_scratch,
            layout_path=state_path(),
            project=wanted_project,
        ).run()
    except KeyboardInterrupt:
        pass
    console.print("[dim]azdo-watch stopped.[/dim]")
    return EXIT_OK


def _with_discovery_cost(snapshot: Snapshot) -> Snapshot:
    """Add the project-discovery call to a snapshot's reported call count.

    Observability, not bookkeeping: the activity log's "N calls in Ms" is only useful
    if N is the number of processes that actually ran.
    """
    return dataclasses.replace(snapshot, calls=snapshot.calls + 1)


def _once(console: Console, poll: Poll, *, view: str = "runs") -> int:
    """`--once`: one snapshot to stdout, no App and no live loop.

    Returns before the App is constructed, so this path works in a pipe and in CI
    without a TTY.
    """
    snapshot, error = poll(PollRequest())
    if error is not None or snapshot is None:
        message = error.message if error is not None else "No data returned."
        console.print(f"[red]{message}[/red]")
        return EXIT_ERROR
    console.print(ui.render_once(snapshot, datetime.now(timezone.utc), view))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
