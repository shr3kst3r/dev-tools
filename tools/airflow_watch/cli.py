"""Entry point for `airflow-watch`.

A live monitor for Airflow deployments on Astronomer Astro (and plain
self-hosted Airflow via `--api-url`), built around the investigation loop: see
what's failing, drill into the failed task, read its log.

This module owns the command line, the clamping, the preflight, and the closures
that wire the app to the `astro` transport — so `app.py` stays free of I/O and
`astro.py` stays free of UI. `--once` prints a snapshot and returns before the
App is ever constructed.

Airflow 2 and Airflow 3 are both spoken; anything else is refused by name rather
than attempted. See `api.py` and the airflow-2-only-behind-a-version-seam and
airflow-3-joins-the-version-seam ADRs.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from datetime import datetime, timezone

from rich.console import Console

from . import api, investigate as investigate_mod, ui
from .app import AirflowWatchApp, Poll, PollResult
from .astro import (
    AstroError,
    PollError,
    RunTasks,
    classify_astro_error,
    detect_version,
    fetch_log,
    fetch_run_bundle,
    fetch_run_tasks,
    fetch_snapshot,
    list_deployments,
    perform,
    require_astro,
    resolve_deployment,
)
from .layout import state_path
from .models import (
    KNOWN_RUN_STATES,
    Action,
    Dag,
    DagList,
    DagRun,
    Deployment,
    PollRequest,
    Snapshot,
    TaskInstance,
    TaskLog,
)

# Requests land on the deployment's own webserver pods, which humans are also
# using, and Astro does not rate-limit the Airflow API — so restraint is ours to
# impose. 15s is the floor regardless of what is asked for.
MIN_INTERVAL = 15
DEFAULT_INTERVAL = 60

# How many DAG runs a poll fetches when --limit is not given: ten server pages,
# fanned out in parallel — enough history to scroll through without asking for
# more, at roughly one extra second per refresh over a single page.
DEFAULT_RUN_LIMIT = 1000

# How long a fetched DAG list stays good. The DAG list is the expensive half of
# a poll — a 242-DAG deployment needs three pages, an 800-DAG one eight, and each
# page of DAG objects is far heavier than a page of runs — but it only changes on
# a deploy, while runs change constantly. Refetching it every poll measured 6.2s
# against ~1.0s for the runs and import errors alone, so it gets its own, much
# slower cadence and is invalidated outright whenever we change a DAG ourselves.
DAG_CACHE_SECONDS = 300

# Return codes: 0 ok, 1 user-actionable failure, 2 bad invocation (argparse's
# own exit code for a usage error).
EXIT_OK = 0
EXIT_ERROR = 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="airflow-watch",
        description=(
            "Live dashboard of recent Airflow DAG runs on an Astro deployment: "
            "what's failing, its task instances, and their logs."
        ),
    )
    parser.add_argument(
        "-d",
        "--deployment",
        default=None,
        help=(
            "Astro deployment name or id to watch (default: the one you last "
            "had open, else the first one visible)."
        ),
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=(
            f"Seconds between refreshes (default: {DEFAULT_INTERVAL}, "
            f"minimum {MIN_INTERVAL})."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RUN_LIMIT,
        help=(
            f"DAG runs to fetch per refresh (default: {DEFAULT_RUN_LIMIT}). "
            "Scrolling to the bottom of the runs list loads older runs past "
            f"this; more than {api.PAGE_LIMIT} is fetched by paging, and the "
            "header always states how many of the deployment's total are shown."
        ),
    )
    parser.add_argument(
        "--state",
        action="append",
        default=None,
        metavar="STATE",
        help=(
            "Only show runs in this state; repeatable. Common values: "
            + ", ".join(KNOWN_RUN_STATES)
            + " (not validated — Airflow's own states win)."
        ),
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "Base URL of a plain (non-Astro) Airflow to watch instead of an "
            "Astro deployment."
        ),
    )
    parser.add_argument(
        "--airflow-version",
        default=None,
        help=(
            "Airflow version of the --api-url target (default: detected by "
            "probing /version once at startup)."
        ),
    )
    parser.add_argument(
        "--view",
        # The Watched view is deliberately absent: the watch list is session
        # state inside the live app, so a one-shot snapshot has nothing to show.
        choices=[view for view in ui.VIEWS if view != "watched"],
        default=ui.VIEWS[0],
        help=(
            "Which list `--once` prints: recent DAG runs, or every DAG "
            "(including paused and stale ones). Default: runs."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Render a single snapshot and exit (no live refresh).",
    )
    return parser.parse_args(argv)


def _plain_deployment(args: argparse.Namespace) -> Deployment:
    """A `--api-url` target, which skips Astro discovery entirely.

    There is no ListDeployments to ask for a plain Airflow, so the version is
    either stated with `--airflow-version` — which is taken at its word and
    costs nothing — or detected by probing the target's `/version` once. It is
    never assumed: guessing 2.x at an Airflow 3 server would fail as an obscure
    404 on the first real request, which is what the seam exists to prevent.
    """
    version = args.airflow_version or detect_version(args.api_url)
    return Deployment(
        id="",
        name=args.api_url,
        airflow_version=version,
        status="",
        api_url=args.api_url,
    )


class _PlainTarget:
    """The `--api-url` deployment, resolved at most once per session.

    Here for the same reason `_DagCache` is: `poll` runs on a timer, and how
    often to re-establish something is a policy decision about the refresh loop.
    Resolving a plain target can cost a `/version` probe, and the ADR is explicit
    that the probe is a startup cost — a per-poll probe would spend ~0.7s every
    refresh re-learning a number that cannot change without a redeploy.

    A failed resolution is deliberately *not* remembered: an unreachable target
    at 09:00 should be retried at 09:01 rather than wedging the session.
    """

    def __init__(self) -> None:
        self._resolved: Deployment | None = None

    def get(self, args: argparse.Namespace) -> Deployment:
        if self._resolved is None:
            self._resolved = _plain_deployment(args)
        return self._resolved


class _DagCache:
    """Per-deployment DAG lists with a TTL, owned by the poll closure.

    Deliberately here rather than in `astro.py`: how often to refetch is a policy
    decision about the shape of one tool's refresh loop, not a property of the
    transport. `drop()` after a confirmed mutation is what keeps a pause action
    from leaving a stale "paused" flag on screen for the rest of the TTL.

    A whole `DagList` is cached, not just the DAGs: the server's total and the
    truncation flag are what let the header say "N of M", and a cache that
    dropped them would make a truncated list look complete for the rest of the
    TTL — and would silently disable the server-side DAG filter that truncation
    is what makes necessary.
    """

    def __init__(self, ttl: float = DAG_CACHE_SECONDS) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, DagList]] = {}

    def get(self, key: str, now: float) -> DagList | None:
        entry = self._entries.get(key)
        if entry is None or now - entry[0] > self._ttl:
            return None
        return entry[1]

    def put(self, key: str, dags: DagList, now: float) -> None:
        self._entries[key] = (now, dags)

    def drop(self, key: str) -> None:
        self._entries.pop(key, None)


class _GraphCache:
    """Task graphs per (deployment, dag_id), with no expiry inside one session.

    A DAG's *structure* changes only when someone deploys, while its runs change
    constantly — so refetching `/dags/{id}/tasks` on every drill-down would spend
    a process spawn (~0.75s) re-learning something that did not move. Bounded so a
    long session on a large deployment cannot grow without limit.
    """

    def __init__(self, capacity: int = 128) -> None:
        self._capacity = capacity
        self._entries: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {}

    def get(self, deployment_key: str, dag_id: str) -> dict[str, tuple[str, ...]] | None:
        return self._entries.get((deployment_key, dag_id))

    def put(
        self, deployment_key: str, dag_id: str, graph: dict[str, tuple[str, ...]]
    ) -> None:
        if not graph:
            return  # a failed structure fetch must not be cached as "no edges"
        if len(self._entries) >= self._capacity:
            self._entries.pop(next(iter(self._entries)))
        self._entries[(deployment_key, dag_id)] = graph


def _states(args: argparse.Namespace) -> tuple[str, ...]:
    """The requested state filter. Deliberately not validated against a closed
    set: an Airflow release may know a state this build does not."""
    return tuple(state for state in (args.state or []) if state)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    console = Console()

    interval = max(MIN_INTERVAL, args.interval)
    # No upper clamp: a request for more than one page is honoured by paging, and
    # the header states "N of M" so a list we could not fill says so.
    limit = max(1, args.limit)
    states = _states(args)
    dag_cache = _DagCache()
    graph_cache = _GraphCache()
    plain_target = _PlainTarget()

    try:
        require_astro()
    except AstroError as exc:
        # The preflight's own message is already user-facing and command-free;
        # printing it directly keeps the "install it" instruction intact.
        console.print(f"[red]{exc}[/red]")
        return EXIT_ERROR

    def choose(deployments: list[Deployment], wanted: Deployment | None) -> Deployment:
        """Which of the discovered deployments to read.

        A key the app already holds — the current selection, or the one restored
        from the state file — is tried first. If it has vanished (deleted
        deployment, stale state file) we fall back rather than refusing to start;
        an explicit `--deployment` that matches nothing still raises, because a
        typo the user just typed should be reported, not silently ignored.
        """
        if wanted is not None:
            try:
                return resolve_deployment(deployments, wanted.key)
            except AstroError:
                pass
        return resolve_deployment(deployments, args.deployment)

    def poll(request: PollRequest) -> PollResult:
        """One refresh: discovery, then the fan-out over the chosen deployment.

        Discovery runs every poll because it is one call and it is where the
        deployment list, each deployment's `airflowVersion`, and the hibernation
        flag all come from — the switcher and the version pinning both depend on
        it staying current.

        `request.dag_pattern` is only non-empty when the app decided the DAG list
        is too large to filter client-side, in which case it becomes Airflow's
        server-side `dag_id_pattern`. Because that changes what a page contains,
        it also bypasses the DAG cache.

        `request.run_limit` is set once scrolling to the bottom of the runs list
        has grown the run window past `--limit` — it only ever widens the fetch,
        so a stale request can never shrink the list the user scrolled for.
        """
        try:
            if args.api_url:
                chosen = plain_target.get(args)
                deployments = [chosen]
            else:
                deployments = list_deployments()
                chosen = choose(deployments, request.deployment)
            now = time.monotonic()
            pattern = request.dag_pattern
            snapshot = fetch_snapshot(
                chosen,
                limit=max(limit, request.run_limit or 0),
                states=states,
                deployments=deployments,
                dags=None if pattern else dag_cache.get(chosen.key, now),
                dag_pattern=pattern,
            )
            if not pattern:
                dag_cache.put(
                    chosen.key,
                    DagList(
                        dags=snapshot.dags,
                        total=snapshot.dags_total,
                        truncated=snapshot.dags_truncated,
                    ),
                    now,
                )
            # Discovery is one extra call on top of the fan-out; report it so the
            # activity log's call count matches what actually ran.
            return _with_discovery_cost(snapshot, args.api_url is None), None
        except (AstroError, api.UnsupportedAirflowVersion) as exc:
            return None, classify_astro_error(exc)

    def load_tasks(
        deployment: Deployment, run: DagRun
    ) -> tuple[RunTasks | None, PollError | None]:
        try:
            result = fetch_run_tasks(
                deployment,
                run,
                graph=graph_cache.get(deployment.key, run.dag_id),
            )
        except (AstroError, api.UnsupportedAirflowVersion) as exc:
            return None, classify_astro_error(exc)
        graph_cache.put(deployment.key, run.dag_id, result.graph)
        return result, None

    def load_log(
        deployment: Deployment, run: DagRun, task: TaskInstance, try_number: int
    ) -> tuple[TaskLog | None, PollError | None]:
        try:
            return fetch_log(deployment, run, task, try_number), None
        except (AstroError, api.UnsupportedAirflowVersion) as exc:
            return None, classify_astro_error(exc)

    def run_action(
        deployment: Deployment, action: Action
    ) -> tuple[str | None, PollError | None]:
        try:
            line = perform(deployment, action)
        except (AstroError, api.UnsupportedAirflowVersion) as exc:
            return None, classify_astro_error(exc)
        if action.mutates:
            # We just changed a DAG, so the cached list is wrong about it. The
            # app re-polls immediately after a real action; this makes that poll
            # tell the truth about `is_paused`.
            dag_cache.drop(deployment.key)
        return line, None

    def prepare_investigation(
        deployment: Deployment, run: DagRun, dag: Dag | None
    ) -> tuple[investigate_mod.Investigation | None, PollError | None]:
        """Gather one run's metadata and task logs into the report `gw` reads.

        Reuses (and refreshes) the graph cache the drill-down uses, since the
        gather starts from the same task fetch.
        """
        try:
            bundle = fetch_run_bundle(
                deployment, run, graph=graph_cache.get(deployment.key, run.dag_id)
            )
        except (AstroError, api.UnsupportedAirflowVersion) as exc:
            return None, classify_astro_error(exc)
        graph_cache.put(deployment.key, run.dag_id, bundle.graph)
        return (
            investigate_mod.prepare(
                deployment, run, dag, bundle, datetime.now(timezone.utc)
            ),
            None,
        )

    if args.once:
        return _once(console, poll, view=args.view)

    try:
        AirflowWatchApp(
            poll=poll,
            interval=interval,
            fetch_tasks=load_tasks,
            fetch_log=load_log,
            perform=run_action,
            investigate=prepare_investigation,
            launch=investigate_mod.run_scratch,
            layout_path=state_path(),
            deployment=args.deployment,
        ).run()
    except KeyboardInterrupt:
        pass
    console.print("[dim]airflow-watch stopped.[/dim]")
    return EXIT_OK


def _with_discovery_cost(snapshot: Snapshot, discovered: bool) -> Snapshot:
    """Add the discovery call to a snapshot's reported call count.

    Observability, not bookkeeping: the activity log's "N calls in Ms" is only
    useful if N is the number of processes that actually ran.
    """
    if not discovered:
        return snapshot
    return dataclasses.replace(snapshot, calls=snapshot.calls + 1)


def _once(console: Console, poll: Poll, *, view: str = "runs") -> int:
    """`--once`: one snapshot to stdout, no App and no live loop.

    Returns before the App is constructed, so this path works in a pipe and in
    CI without a TTY.
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
