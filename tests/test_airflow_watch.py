"""Tests for airflow-watch: the pure layers (version seam, parsers, models,
error classification, redaction), the single subprocess seam, and the Textual
app including its drill-down and its confirmation gate.

Fixtures below are trimmed and anonymised captures of real responses from live
Astro deployments — Airflow 2.11.0 for the `*_PAYLOAD` fixtures and Airflow
3.3.0 for the `*_V2_PAYLOAD` ones. Deployment ids, hostnames and DAG names are
renamed, and no fixture carries a credential.

The mutating actions are exercised **only** against fakes. Nothing here talks to
a real Airflow, and nothing here may: the transport is faked at one seam
(`astro._run`), and the app is driven with injected callables.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import pathlib
import re
import threading
from datetime import datetime, timedelta, timezone

from rich.console import Console
from textual.widgets import DataTable, Static

from tools.airflow_watch import api, astro, layout, ui
from tools.airflow_watch.app import (
    AirflowWatchApp,
    ConfirmScreen,
    DeploymentScreen,
    HelpScreen,
    ImportErrorScreen,
    LogScreen,
    MenuScreen,
)
from tools.airflow_watch.astro import AstroError, PollError
from tools.airflow_watch.cli import _parse_args, _plain_deployment, _states
from tools.airflow_watch.models import (
    ACTION_KINDS,
    KNOWN_RUN_STATES,
    KNOWN_TASK_STATES,
    Action,
    Dag,
    DagList,
    DagRun,
    Deployment,
    Drill,
    ImportErrorEntry,
    LogEntry,
    PollRequest,
    Snapshot,
    TaskInstance,
    TaskLog,
    TaskRow,
    filter_log,
    live_import_error_files,
    matches,
    order_task_instances,
    sort_runs,
    sort_task_instances,
)

NOW = datetime(2026, 7, 24, 21, 0, 0, tzinfo=timezone.utc)

# A JWT-shaped string of exactly the form an Astro session token takes. Used to
# prove redaction works; it is not a real credential.
FAKE_JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJmYWtlIiwiZXhwIjoxNzAwMDAwMDAwfQ"
    ".c2lnbmF0dXJlLXRoYXQtaXMtbm90LXJlYWw"
)


# --- factories -------------------------------------------------------------


def _deployment(
    name: str = "Production",
    *,
    id_: str = "dep-prod-1",
    version: str = "2.11.0",
    status: str = "HEALTHY",
    workspace: str = "Customers",
    hibernating: bool = False,
    api_url: str = "",
) -> Deployment:
    return Deployment(
        id=id_,
        name=name,
        workspace_name=workspace,
        airflow_version=version,
        status=status,
        api_url=api_url or f"https://{id_}.example.invalid/d/api/v1",
        hibernating=hibernating,
    )


def _run_(
    dag_id: str = "sync_alpha",
    *,
    run_id: str = "scheduled__2026-07-24T20:40:00+00:00",
    state: str = "success",
    run_type: str = "scheduled",
    start: datetime | None = None,
    end: datetime | None = None,
    note: str | None = None,
) -> DagRun:
    started = NOW - timedelta(minutes=20) if start is None else start
    return DagRun(
        dag_id=dag_id,
        run_id=run_id,
        state=state,
        run_type=run_type,
        logical_date=started,
        start_date=started,
        end_date=end,
        note=note,
    )


def _task(
    task_id: str = "sensor",
    *,
    state: str = "success",
    try_number: int = 1,
    max_tries: int = 1,
    operator: str = "PythonOperator",
    map_index: int = -1,
) -> TaskInstance:
    return TaskInstance(
        task_id=task_id,
        state=state,
        try_number=try_number,
        max_tries=max_tries,
        operator=operator,
        start_date=NOW - timedelta(minutes=10),
        end_date=NOW - timedelta(minutes=5),
        pool="default_pool",
        map_index=map_index,
    )


def _dag(dag_id: str = "sync_alpha", *, paused: bool = False) -> Dag:
    return Dag(
        dag_id=dag_id,
        is_paused=paused,
        owners=("data",),
        tags=("databricks",),
        next_dagrun=NOW + timedelta(hours=1),
    )


def _snapshot(
    *,
    deployment: Deployment | None = None,
    runs: tuple[DagRun, ...] | None = None,
    dags: tuple[Dag, ...] | None = None,
    import_errors: tuple[ImportErrorEntry, ...] = (),
    deployments: tuple[Deployment, ...] | None = None,
) -> Snapshot:
    chosen = deployment or _deployment()
    default_runs = (
        _run_("sync_beta", run_id="r-broken", state="failed"),
        _run_("sync_alpha", run_id="r-ok", state="success", end=NOW),
    )
    return Snapshot(
        deployment=chosen,
        deployments=deployments or (chosen, _deployment("Staging", id_="dep-stg-2")),
        runs=tuple(sort_runs(list(runs if runs is not None else default_runs))),
        dags=dags if dags is not None else (_dag("sync_alpha"), _dag("sync_beta")),
        import_errors=import_errors,
        calls=4,
        elapsed=1.23,
    )


# --- captured fixtures (anonymised) ---------------------------------------


DEPLOYMENTS_PAYLOAD = {
    "deployments": [
        {
            "id": "dep-prod-1",
            "name": "Production",
            "workspaceName": "Customers",
            "airflowVersion": "2.11.0",
            "astroRuntimeVersion": "13.4.0",
            "status": "HEALTHY",
            "type": "HYBRID",
            "apiUrl": "https://dep-prod-1.example.invalid/dprod/api/v1",
            "scalingStatus": {
                "hibernationStatus": {
                    "isHibernating": False,
                    "reason": "Hibernation not enabled for this deployment",
                }
            },
        },
        {
            "id": "dep-dev-2",
            "name": "Dev",
            "workspaceName": "Engineering",
            "airflowVersion": "2.11.0",
            "status": "HIBERNATING",
            "apiUrl": "https://dep-dev-2.example.invalid/ddev/api/v1",
            "scalingStatus": {"hibernationStatus": {"isHibernating": True}},
        },
        {
            "id": "dep-next-3",
            "name": "Next",
            "workspaceName": "Engineering",
            "airflowVersion": "3.0.2",
            "status": "HEALTHY",
            "apiUrl": "https://dep-next-3.example.invalid/dnext/api/v2",
        },
        # A major this build does not speak. Invented rather than captured —
        # there is no Airflow 4 — and it exists so every refusal path keeps a
        # target now that 3.x is supported.
        {
            "id": "dep-future-4",
            "name": "Future",
            "workspaceName": "Engineering",
            "airflowVersion": "4.0.0",
            "status": "HEALTHY",
            "apiUrl": "https://dep-future-4.example.invalid/dfut/api/v4",
        },
    ],
    "limit": 20,
    "offset": 0,
    "totalCount": 4,
}

# The version this build refuses, named once so the refusal tests agree.
UNSUPPORTED_VERSION = "4.0.0"

DAG_RUN_ROWS: list[dict] = [
    {
        "conf": {},
        "dag_id": "sync_alpha",
        "dag_run_id": "scheduled__2026-07-24T20:40:00+00:00",
        "data_interval_end": "2026-07-24T20:40:00+00:00",
        "data_interval_start": "2026-07-24T20:35:00+00:00",
        "end_date": "2026-07-24T21:10:13.602608+00:00",
        "execution_date": "2026-07-24T20:40:00+00:00",
        "external_trigger": False,
        "logical_date": "2026-07-24T20:40:00+00:00",
        "note": None,
        "run_type": "scheduled",
        "start_date": "2026-07-24T20:40:00.000001+00:00",
        "state": "failed",
    },
    {
        "conf": {"paths": "['inbox/report.csv']"},
        "dag_id": "cw_beta",
        "dag_run_id": "manual__2026-07-24T21:18:00+00:00",
        "end_date": None,
        "execution_date": "2026-07-24T21:18:00+00:00",
        "external_trigger": True,
        "logical_date": "2026-07-24T21:18:00+00:00",
        "note": "kicked off by hand",
        "run_type": "manual",
        "start_date": "2026-07-24T21:18:30+00:00",
        "state": "running",
    },
    {
        "dag_id": "monitoring",
        "dag_run_id": "scheduled__2026-07-24T20:55:00+00:00",
        "end_date": "2026-07-24T20:55:01+00:00",
        "logical_date": "2026-07-24T20:55:00+00:00",
        "run_type": "scheduled",
        "start_date": "2026-07-24T20:55:00+00:00",
        "state": "success",
    },
]

DAG_RUNS_PAYLOAD = {"dag_runs": DAG_RUN_ROWS, "total_entries": 533618}

TASK_INSTANCES_PAYLOAD = {
    "task_instances": [
        {
            "dag_id": "sync_alpha",
            "dag_run_id": "scheduled__2026-07-24T20:40:00+00:00",
            "duration": 1812.0,
            "end_date": "2026-07-24T21:10:13.710848+00:00",
            "execution_date": "2026-07-24T20:40:00+00:00",
            "map_index": -1,
            "max_tries": 2,
            "operator": "S3KeySensorAsync",
            "pool": "default_pool",
            "pool_slots": 1,
            "start_date": "2026-07-24T20:40:01.710848+00:00",
            "state": "failed",
            "task_id": "sensor",
            "try_number": 1,
        },
        {
            "dag_id": "sync_alpha",
            "dag_run_id": "scheduled__2026-07-24T20:40:00+00:00",
            "duration": None,
            "end_date": None,
            "map_index": 3,
            "max_tries": 0,
            "operator": "DatabricksSubmitRunOperator",
            "pool": "default_pool",
            "start_date": None,
            "state": "upstream_failed",
            "task_id": "databricks_sync",
            "try_number": 0,
        },
    ],
    "total_entries": 2,
}

DAGS_PAYLOAD = {
    "dags": [
        {
            "dag_id": "sync_alpha",
            "dag_display_name": "sync_alpha",
            "description": "Sync alpha",
            "fileloc": "/usr/local/airflow/dags/jobs.py",
            "has_import_errors": False,
            "is_active": True,
            "is_paused": True,
            "next_dagrun": "2026-07-24T22:40:00+00:00",
            "owners": ["data"],
            "schedule_interval": {"__type": "CronExpression", "value": "40 * * * *"},
            "tags": [{"name": "databricks"}, {"name": "data"}],
        },
        {
            "dag_id": "cw_beta",
            # Matches IMPORT_ERRORS_PAYLOAD's filename, so this DAG's flag is a
            # *live* error — the case the marker must show in red.
            "fileloc": "/usr/local/airflow/dags/broken.py",
            "has_import_errors": True,
            "is_paused": False,
            "next_dagrun": None,
            "owners": ["cs", "data"],
            "tags": [],
        },
    ],
    "total_entries": 2,
}

IMPORT_ERRORS_PAYLOAD = {
    "import_errors": [
        {
            "import_error_id": 4,
            "filename": "/usr/local/airflow/dags/broken.py",
            "stack_trace": "Traceback (most recent call last):\n  ImportError: boom",
            "timestamp": "2026-07-24T20:00:00+00:00",
        }
    ],
    "total_entries": 1,
}

# v1 answers a log request with the *repr* of a list of (host, text) tuples, not
# with plain text. Captured verbatim (paths shortened).
LOG_PAYLOAD = {
    "content": (
        "[('', \" INFO - ::group::Log message source details\\n"
        "*** Found logs in s3:\\n"
        " INFO - ::endgroup::\\n"
        "[2026-07-24T20:49:32.561+0000] {taskinstance.py:441} INFO - start\\n\")]"
    ),
    "continuation_token": "eyJlbmRfb2ZfbG9nIjp0cnVlfQ.Zm9vYmFy",
}


# --- Airflow 3.3.0 captures ------------------------------------------------
#
# The three responses whose *shape* differs from v1, taken from the live 3.3.0
# deployment (read-only GETs) rather than from the spec. Everything else the
# tool reads — runs, task instances, the task graph, import errors — comes back
# with the same field names in both majors and is covered by the v1 fixtures.

DAGS_V2_PAYLOAD = {
    "dags": [
        {
            "dag_id": "sync_alpha",
            "dag_display_name": "sync_alpha",
            "description": "Sync alpha",
            "fileloc": "/usr/local/airflow/dags/jobs.py",
            "has_import_errors": False,
            # `is_stale`, not `is_active` — and the sense is inverted.
            "is_stale": False,
            "is_paused": True,
            # `next_dagrun` is gone; the run-after form is what the UI wants.
            "next_dagrun_logical_date": "2026-07-24T21:40:00+00:00",
            "next_dagrun_run_after": "2026-07-24T22:40:00+00:00",
            "owners": ["data"],
            # A plain cron string where v1 had a typed `schedule_interval` object.
            "timetable_summary": "40 * * * *",
            "timetable_description": "At minute 40",
            "tags": [{"name": "databricks"}, {"name": "data"}],
        },
        {
            "dag_id": "cw_beta",
            "fileloc": "/usr/local/airflow/dags/broken.py",
            "has_import_errors": True,
            "is_stale": True,
            "is_paused": False,
            "next_dagrun_run_after": None,
            "owners": ["cs", "data"],
            "timetable_summary": None,
            "timetable_description": "Never, external triggers only",
            "tags": [],
        },
    ],
    "total_entries": 2,
}

# v2 answers a log request with a JSON array of structured events. The trailing
# bare string is real: the source-details preamble arrives unstructured.
LOG_V2_PAYLOAD = {
    "content": [
        {
            "event": "::group::Log message source details",
            "level": "info",
            "logger": "airflow.task",
            "timestamp": "2026-07-27T09:12:01.004+00:00",
            "sources": ["s3://logs/dag_id=sync_alpha/attempt=1.log"],
        },
        {
            "event": "start",
            "level": "info",
            "timestamp": "2026-07-27T09:12:02.561+00:00",
            "chan": "stdout",
        },
        {
            "event": "Task failed with exception",
            "level": "error",
            "timestamp": "2026-07-27T09:41:13.710+00:00",
        },
        "*** Found logs in s3:",
    ],
    "continuation_token": "eyJlbmRfb2ZfbG9nIjp0cnVlfQ.Zm9vYmFy",
}

# FastAPI reports a validation failure with `detail` as a *list*, which the
# string-shaped RFC-7807 reader walks straight past.
VALIDATION_ERROR_V2_PAYLOAD = {
    "detail": [
        {
            "type": "missing",
            "loc": ["body", "logical_date"],
            "msg": "Field required",
            "input": {"conf": {}},
        },
        {"type": "missing", "loc": ["body", "conf"], "msg": "Field required"},
    ]
}


# --- the version seam ------------------------------------------------------


def test_supports_airflow_2_and_3_and_nothing_else() -> None:
    assert api.supports("2.11.0")
    assert api.supports("2.10")
    assert api.supports("v2.11.0+astro.1")
    assert api.supports("3.0.0")
    assert api.supports("3.3.0")
    assert api.supports("v3.3.0+astro.2")
    # The refusal machinery stays in place; it just fires for other majors now.
    assert not api.supports("1.10.15")
    assert not api.supports(UNSUPPORTED_VERSION)
    assert not api.supports("")
    assert not api.supports("banana")


def test_major_version_is_lenient() -> None:
    assert api.major_version("2.11.0") == 2
    assert api.major_version(" v2.11 ") == 2
    assert api.major_version("3.0.2") == 3
    assert api.major_version("3.3.0+astro.2") == 3
    assert api.major_version("nonsense") is None


def test_base_path_is_per_major_and_refuses_the_rest_by_name() -> None:
    assert api.base_path("2.11.0") == "/api/v1"
    assert api.base_path("3.3.0") == "/api/v2"
    try:
        api.base_path(UNSUPPORTED_VERSION)
    except api.UnsupportedAirflowVersion as exc:
        assert exc.version == UNSUPPORTED_VERSION
        assert UNSUPPORTED_VERSION in str(exc)  # the refusal names what it saw
        assert "Airflow 2.x" in str(exc) and "Airflow 3.x" in str(exc)
    else:  # pragma: no cover - the raise is the behaviour under test
        raise AssertionError(f"{UNSUPPORTED_VERSION} must be refused")


def test_unsupported_message_names_version_even_when_blank() -> None:
    assert "unknown" in api.unsupported_message("   ")


def test_api_url_for_appends_base_path_only_when_missing() -> None:
    assert (
        api.api_url_for("https://airflow.example.invalid", "2.11.0")
        == "https://airflow.example.invalid/api/v1"
    )
    assert (
        api.api_url_for("https://airflow.example.invalid/api/v1/", "2.11.0")
        == "https://airflow.example.invalid/api/v1"
    )
    assert (
        api.api_url_for("https://airflow.example.invalid", "3.3.0")
        == "https://airflow.example.invalid/api/v2"
    )
    assert (
        api.api_url_for("https://airflow.example.invalid/api/v2", "3.3.0")
        == "https://airflow.example.invalid/api/v2"
    )


def test_api_url_for_replaces_the_other_majors_suffix() -> None:
    """A URL copied from a 3.x deployment but pinned to 2.x (or the reverse)
    must not become `…/api/v2/api/v1` — every request would 404."""
    assert (
        api.api_url_for("https://airflow.example.invalid/api/v2", "2.11.0")
        == "https://airflow.example.invalid/api/v1"
    )
    assert (
        api.api_url_for("https://airflow.example.invalid/api/v1/", "3.3.0")
        == "https://airflow.example.invalid/api/v2"
    )


def test_probe_pins_are_concrete_and_ordered_by_the_urls_hint() -> None:
    """Each pin is handed straight to `astro --airflow-version`, which uses it to
    pick a bundled OpenAPI spec, so "2.x" would not do."""
    pins = api.probe_pins("https://airflow.example.invalid")
    assert len(pins) == 2
    for pin in pins:
        assert api.supports(pin)
        assert re.fullmatch(r"\d+\.\d+\.\d+", pin)
    # One pin per major, so a probe cannot miss a supported line.
    assert {api.major_version(pin) for pin in pins} == {2, 3}
    # No hint: the 2.x line first, which is what the old assume-2.11 behaviour
    # did — an existing plain-2.x user still succeeds on call one.
    assert api.major_version(pins[0]) == 2
    # A URL that already names a base path is asked in that dialect first.
    v2_first = api.probe_pins("https://airflow.example.invalid/api/v2")
    assert api.major_version(v2_first[0]) == 3
    assert set(v2_first) == set(pins)
    assert api.major_version(api.probe_pins("https://x/api/v1/")[0]) == 2


def test_version_path_and_parse_version() -> None:
    assert api.version_path() == "/version"
    assert api.parse_version({"version": "2.11.0"}) == "2.11.0"
    assert api.parse_version({"version": " 3.1.2 "}) == "3.1.2"
    assert api.parse_version({"git_version": "abc"}) == ""
    assert api.parse_version("2.11.0") == ""
    assert api.parse_version(None) == ""


def test_parse_version_keeps_the_release_and_drops_the_build_suffix() -> None:
    """What comes back is handed to `astro --airflow-version`, which loads the
    OpenAPI spec *named by it* — so it has to be a version the CLI can resolve.
    An Astro Runtime image reports `3.3.0+astro.2`, which is not a released
    Airflow and 404s the spec fetch, turning a successful probe into a session
    where every later call fails. The release is kept exactly; only the build
    metadata goes."""
    for reported, pinned in (
        ("3.3.0+astro.2", "3.3.0"),
        ("2.11.0+astro.7", "2.11.0"),
        ("2.10.3", "2.10.3"),
    ):
        detected = api.parse_version({"version": reported})
        assert detected == pinned
        # Shaped like the probe pins, which is the shape the CLI resolves.
        assert re.fullmatch(r"\d+\.\d+\.\d+", detected)
        assert api.supports(detected)
    # An unsupported major still survives intact, so the refusal can name it.
    assert api.parse_version({"version": "4.0.0+astro.1"}) == UNSUPPORTED_VERSION


# --- ADR constraint: nothing version-specific outside api.py ---------------

_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "tools" / "airflow_watch"

# Version-specific wire names, **both dialects in one list**: the constraint is
# that no version's spelling appears outside api.py, whichever version it
# belongs to. Each appears as a *string literal* only inside api.py; the same
# words exist as normalized model attributes (`Dag.is_stale`, `Action.map_index`),
# which is fine — it is the wire spelling that is version-specific.
_VERSIONED_WIRE_NAMES = (
    # v1
    "execution_date",
    "only_active",
    "is_active",
    "schedule_interval",
    "next_dagrun",
    # v2
    "exclude_stale",
    "is_stale",
    "run_after",
    "next_dagrun_run_after",
    "timetable_summary",
    "timetable_description",
    # The ordering *values*. `order_by` is spelled the same in both majors, but
    # `-execution_date` does not exist in v2 and an unknown one is ignored
    # rather than rejected, so the value is the version-specific part. Listed in
    # their own right because the leading `-` means the bare names above would
    # not match a leaked `"-run_after"`.
    "-execution_date",
    "-run_after",
    # shared spellings that are still wire names, not model names
    "dag_run_id",
    # The two v1 endpoints that disagree with each other: `clearTaskInstances`
    # takes `task_ids`, `updateTaskInstancesState` takes `task_id`. Which is
    # which is exactly the kind of knowledge that must not leak out of the seam.
    "task_id",
    "task_ids",
    "map_index",
    "dag_runs",
    "task_instances",
    "import_errors",
    "stack_trace",
    "full_content",
    "continuation_token",
    "update_mask",
    "logical_date",
    "dry_run",
    "new_state",
    "is_paused",
    "has_import_errors",
)

# The seam's own version-dispatch machinery. A reference to any of these outside
# api.py would be a version conditional living in the wrong module. (Public
# helpers the transport legitimately calls — `base_path`, `probe_pins`,
# `supports` — are not listed: the constraint is on *dispatching*, not on
# asking.)
_SEAM_INTERNALS = (
    "_SUPPORTED_MAJORS",
    "_V1_BASE_PATH",
    "_V2_BASE_PATH",
    "_PROBE_PINS",
    "_serves_v2",
    "major_version",
)

# Endpoints only one major has. Checked as *bare* substrings, not quoted ones:
# a path is built by interpolation (`f"/dags/{dag_id}/…"`), so the quoted-literal
# form used for field names would never see one. This is what holds the
# airflow-3-joins-the-version-seam decision to move the whole (method, path,
# body) construction into `api.mutation_request` — the transport used to build
# these itself, and nothing else stops it doing so again.
_VERSIONED_ENDPOINTS = (
    "updateTaskInstancesState",  # v1's set-state collection endpoint
    "/dry_run",  # v2's separate side-effect-free mark endpoint
)


def _modules_outside_api() -> list[pathlib.Path]:
    return [p for p in sorted(_PACKAGE.glob("*.py")) if p.name != "api.py"]


def test_no_api_version_literal_outside_api_module() -> None:
    """The ADR says a reviewer should be able to grep for this. This is the grep."""
    assert _modules_outside_api(), "expected other modules to check"
    for path in _modules_outside_api():
        assert "/api/v" not in path.read_text(), f"{path.name} contains an /api/v literal"
    # Both dialects' base paths live in the seam, and only there.
    seam = (_PACKAGE / "api.py").read_text()
    assert "/api/v1" in seam
    assert "/api/v2" in seam


def test_no_version_conditional_outside_api_module() -> None:
    for path in _modules_outside_api():
        text = path.read_text()
        for name in _SEAM_INTERNALS:
            assert name not in text, f"{path.name} reaches into the version seam ({name})"


def test_no_versioned_field_name_outside_api_module() -> None:
    for path in _modules_outside_api():
        text = path.read_text()
        for name in _VERSIONED_WIRE_NAMES:
            assert f'"{name}"' not in text, f"{path.name} spells the wire field {name}"
            assert f"'{name}'" not in text, f"{path.name} spells the wire field {name}"


def test_no_version_specific_endpoint_outside_api_module() -> None:
    """A mutation's *shape* is version knowledge too, so the path belongs to the
    seam as much as the field names do."""
    seam = (_PACKAGE / "api.py").read_text()
    for name in _VERSIONED_ENDPOINTS:
        assert name in seam, f"{name} should be built in api.py"
        for path in _modules_outside_api():
            assert name not in path.read_text(), (
                f"{path.name} builds the version-specific endpoint {name}"
            )


# --- ADR constraint: query params in the path, never -f/-F ------------------


V1 = "2.11.0"
V2 = "3.3.0"


def test_built_paths_embed_query_parameters() -> None:
    # -f/-F would silently flip the request to POST and return 405 on a GET
    # endpoint, so every filter has to ride in the path string.
    for version in (V1, V2):
        runs = api.dag_runs_path(version=version, limit=25, states=("failed", "running"))
        assert runs.startswith("/dags/~/dagRuns?"), version
        assert "limit=25" in runs
        assert runs.count("state=") == 2  # repeated key, not a comma list
        assert "order_by=" in runs

        dags = api.dags_path(version=version, limit=10)
        assert dags.startswith("/dags?"), version
        assert "limit=10" in dags

    assert api.import_errors_path(limit=5) == "/importErrors?limit=5"
    assert "?update_mask=is_paused" in api.pause_dag_path("d")
    assert "full_content=true" in api.log_path("d", "r", "t", 1)


def test_dags_path_turns_off_the_hide_stale_filter_in_both_dialects() -> None:
    """The filter defaults to hiding in both majors, and v2 *ignores* the v1
    spelling silently — so sending the wrong one would quietly drop rows rather
    than fail. The name is the whole test."""
    v1 = api.dags_path(version=V1, dag_id_pattern="sync")
    assert "only_active=false" in v1
    assert "exclude_stale" not in v1
    v2 = api.dags_path(version=V2, dag_id_pattern="sync")
    assert "exclude_stale=false" in v2
    assert "only_active" not in v2
    # The rest of the query is shared.
    for path in (v1, v2):
        assert "dag_id_pattern=sync" in path
        assert f"limit={api.DEFAULT_LIMIT}" in path


def test_dag_runs_path_orders_newest_first_in_each_dialect() -> None:
    """Pinned to the exact values: `-execution_date` does not exist in Airflow 3,
    and an unknown `order_by` there is ignored rather than rejected — the runs
    list would come back in an arbitrary order and look plausible."""
    assert "order_by=-execution_date" in api.dag_runs_path(version=V1)
    assert "order_by=-run_after" in api.dag_runs_path(version=V2)
    # Same wildcard, same paging, either way.
    for version in (V1, V2):
        path = api.dag_runs_path(version=version, limit=7, offset=100)
        assert path.startswith("/dags/~/dagRuns?")
        assert "limit=7" in path and "offset=100" in path


def test_built_paths_have_no_trailing_slash() -> None:
    paths = [
        api.dags_path(version=V1),
        api.dags_path(version=V2),
        api.dag_path("d"),
        api.dag_runs_path(version=V1),
        api.dag_runs_path(version=V2),
        api.task_instances_path("d", "r"),
        api.log_path("d", "r", "t", 2),
        api.import_errors_path(),
        api.pause_dag_path("d"),
        api.trigger_run_path("d"),
        api.clear_task_instances_path("d"),
        api.mark_task_state_path("d"),
        api.mark_task_instance_path("d", "r", "t"),
        api.mark_task_instance_path("d", "r", "t", map_index=3, dry_run=True),
    ]
    for path in paths:
        assert path.startswith("/")
        assert not path.rstrip("?").endswith("/"), path


def test_path_segments_are_percent_encoded() -> None:
    # Run ids carry ':' and '+', which must not be read as URL syntax.
    path = api.task_instances_path("my dag", "manual__2026-05-13T01:04:15+00:00")
    assert "%3A" in path and "%2B" in path
    assert "my%20dag" in path
    assert " " not in path


def test_log_path_never_asks_for_attempt_zero() -> None:
    # try_number 0 means "never ran"; v1 still serves attempt 1's log body.
    assert api.log_path("d", "r", "t", 0).startswith("/dags/d/dagRuns/r/taskInstances/t/logs/1")


def test_log_path_addresses_a_mapped_task_instance() -> None:
    """v1 looks a task instance up by (dag, run, task, map_index) and defaults
    the last to -1, so a mapped instance's log is a 404 without it."""
    mapped = api.log_path("d", "r", "t", 1, map_index=3)
    assert "map_index=3" in mapped
    # -1 is v1's own default and our "not mapped" sentinel, so it stays off.
    assert "map_index" not in api.log_path("d", "r", "t", 1)
    assert "map_index" not in api.log_path("d", "r", "t", 1, map_index=-1)


def test_astro_never_passes_field_flags() -> None:
    """The whole package must not contain -f/-F, per the measured 405."""
    for path in sorted(_PACKAGE.glob("*.py")):
        text = path.read_text()
        assert '"-f"' not in text, path.name
        assert '"-F"' not in text, path.name
        assert "--raw-field" not in text, path.name


# --- request bodies --------------------------------------------------------


def test_trigger_body_uses_logical_date_not_execution_date() -> None:
    for version in (V1, V2):
        body = api.trigger_body(version, NOW)
        assert body["logical_date"] == NOW.isoformat()
        assert "execution_date" not in body  # gone from the Airflow 3 payload
        assert api.trigger_body(version)["conf"] == {}


def test_trigger_body_states_a_null_logical_date_only_for_v2() -> None:
    """v2 makes the field required-but-nullable: omitting it is a validation
    error, and an explicit null is how you ask the server to stamp the date.
    v1 has no such rule and omitting it is the documented way."""
    v2 = api.trigger_body(V2)
    assert "logical_date" in v2
    assert v2["logical_date"] is None
    assert "logical_date" not in api.trigger_body(V1)


def test_clear_and_mark_bodies_always_state_dry_run() -> None:
    # v1 defaults dry_run to *true*: a body that omits it returns 200 and does
    # nothing at all, so the flag is never left implicit.
    for dry in (True, False):
        clear = api.clear_body("r1", ("t1",), dry_run=dry)
        assert clear["dry_run"] is dry
        assert clear["dag_run_id"] == "r1"
        assert clear["task_ids"] == ["t1"]
        mark = api.mark_body("r1", "t1", "success", dry_run=dry)
        assert mark["dry_run"] is dry
        assert mark["new_state"] == "success"


def test_mark_body_names_one_task_id_not_a_list() -> None:
    """The two endpoints disagree, and v1 wins: `clearTaskInstances` takes a
    `task_ids` array, `updateTaskInstancesState` takes a single `task_id`. A body
    carrying the wrong one is rejected (unknown field + missing required field),
    so every mark action would have failed."""
    mark = api.mark_body("r1", "t1", "failed", dry_run=True)
    assert mark["task_id"] == "t1"
    assert "task_ids" not in mark
    # All four include_* flags are required by that endpoint's schema.
    for flag in ("include_upstream", "include_downstream", "include_future", "include_past"):
        assert mark[flag] is False


def test_clear_body_omits_task_ids_when_clearing_a_whole_run() -> None:
    assert "task_ids" not in api.clear_body("r1", (), dry_run=True)


def test_pause_body() -> None:
    assert api.pause_body(True) == {"is_paused": True}
    assert api.pause_body(False) == {"is_paused": False}


# --- mutations: the whole request, per major -------------------------------


def _mark(
    *,
    task_ids: tuple[str, ...] = ("sensor",),
    state: str = "success",
    dry_run: bool = False,
    map_index: int = -1,
) -> Action:
    return Action(
        kind="mark",
        dag_id="sync_alpha",
        run_id="manual__2026-07-27T09:00:00+00:00",
        task_ids=task_ids,
        state=state,
        dry_run=dry_run,
        map_index=map_index,
    )


def test_mutation_request_shares_pause_trigger_and_clear_across_majors() -> None:
    """Three of the five actions were verified identical in 3.3.0 — including
    `clearTaskInstances`, which the original seam ADR recorded as removed."""
    for kind in ("pause", "unpause"):
        v1 = api.mutation_request(V1, Action(kind=kind, dag_id="sync_alpha"))
        assert v1 == api.mutation_request(V2, Action(kind=kind, dag_id="sync_alpha"))
        assert v1[0] == "PATCH"
        assert "update_mask=is_paused" in v1[1]

    trigger = Action(kind="trigger", dag_id="sync_alpha")
    assert api.mutation_request(V1, trigger)[:2] == api.mutation_request(V2, trigger)[:2]
    assert api.mutation_request(V1, trigger)[0] == "POST"

    clear = Action(kind="clear", dag_id="d", run_id="r", task_ids=("t",), dry_run=True)
    assert api.mutation_request(V1, clear) == api.mutation_request(V2, clear)
    method, path, body = api.mutation_request(V2, clear)
    assert (method, path) == ("POST", "/dags/d/clearTaskInstances")
    assert body["dry_run"] is True


def test_mutation_request_marks_a_task_with_v1s_collection_endpoint() -> None:
    method, path, body = api.mutation_request(V1, _mark())
    assert method == "POST"
    assert path == "/dags/sync_alpha/updateTaskInstancesState"
    assert body["task_id"] == "sensor"  # not a `task_ids` list
    assert body["new_state"] == "success"
    assert body["dry_run"] is False
    # v1 says "dry run" in the body, so the path never moves.
    assert api.mutation_request(V1, _mark(dry_run=True))[1] == path
    assert api.mutation_request(V1, _mark(dry_run=True))[2]["dry_run"] is True
    # ...and a mapped instance is expanded by the endpoint, not addressed.
    assert api.mutation_request(V1, _mark(map_index=3))[1] == path


def test_mutation_request_marks_a_task_by_patching_the_instance_on_v2() -> None:
    """Airflow 3 removed `updateTaskInstancesState`. Its replacement addresses
    the instance by path, so both of the things v1 carried in the body — which
    task, and whether this is a dry run — become path segments."""
    method, path, body = api.mutation_request(V2, _mark())
    assert method == "PATCH"
    assert path == (
        "/dags/sync_alpha/dagRuns/manual__2026-07-27T09%3A00%3A00%2B00%3A00"
        "/taskInstances/sensor"
    )
    assert body == {"new_state": "success"}  # exactly this, nothing else

    # The dry run is a *different endpoint*, side-effect-free by construction,
    # not a flag — and the body stays identical.
    dry_method, dry_path, dry_body = api.mutation_request(V2, _mark(dry_run=True))
    assert dry_method == "PATCH"
    assert dry_path == path + "/dry_run"
    assert dry_body == body
    assert "dry_run" not in dry_body

    # A mapped instance names its index in the path; -1 means "not mapped" and
    # stays off, exactly as it does for a log request.
    assert api.mutation_request(V2, _mark(map_index=3))[1] == path + "/3"
    assert api.mutation_request(V2, _mark(map_index=-1))[1] == path
    assert (
        api.mutation_request(V2, _mark(map_index=3, dry_run=True))[1]
        == path + "/3/dry_run"
    )


def test_mutation_request_marks_exactly_one_task_in_both_majors() -> None:
    """v1's endpoint names one `task_id` in its body and v2's addresses one
    instance by path: different reasons, same contract. Marking one of several
    silently is the failure being refused."""
    for version in (V1, V2):
        for task_ids in ((), ("a", "b")):
            try:
                api.mutation_request(version, _mark(task_ids=task_ids))
            except ValueError as exc:
                assert "exactly one task instance" in str(exc)
                assert str(len(task_ids)) in str(exc)
            else:  # pragma: no cover
                raise AssertionError(f"{version} {task_ids!r} must be refused")


def test_mutation_request_refuses_an_unscoped_clear_or_mark_in_both_majors() -> None:
    for version in (V1, V2):
        for kind in ("clear", "mark"):
            try:
                api.mutation_request(
                    version,
                    Action(kind=kind, dag_id="d", task_ids=("t",), state="success"),
                )
            except ValueError as exc:
                assert "every run" in str(exc), (version, kind)
            else:  # pragma: no cover
                raise AssertionError(f"an unscoped {kind} must be refused")


def test_mutation_request_refuses_an_unknown_kind() -> None:
    for version in (V1, V2):
        try:
            api.mutation_request(version, Action(kind="delete_everything", dag_id="d"))
        except ValueError as exc:
            assert "delete_everything" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("an unknown action must not be built")


def test_every_action_kind_has_a_request_in_both_majors() -> None:
    """The modal offers exactly `ACTION_KINDS`; a kind with no request would be
    a dead key on one major and a working one on the other."""
    for version in (V1, V2):
        for kind in ACTION_KINDS:
            method, path, _ = api.mutation_request(
                version,
                Action(
                    kind=kind,
                    dag_id="d",
                    run_id="r",
                    task_ids=("t",),
                    state="success",
                ),
            )
            assert method in ("POST", "PATCH"), (version, kind)
            assert path.startswith("/dags/d"), (version, kind)


# --- parsers ---------------------------------------------------------------


def test_parse_deployments_reads_version_status_and_hibernation() -> None:
    deployments = api.parse_deployments(DEPLOYMENTS_PAYLOAD)
    assert [d.name for d in deployments] == ["Production", "Dev", "Next", "Future"]
    prod, dev, nxt, future = deployments
    assert prod.airflow_version == "2.11.0"
    assert prod.label == "Customers / Production"
    assert prod.key == "dep-prod-1"
    assert prod.is_astro
    assert not prod.is_hibernating
    assert dev.is_hibernating  # from scalingStatus.hibernationStatus
    # A 3.x deployment is a first-class target now, not a refusal.
    assert api.supports(nxt.airflow_version)
    # An unsupported one is kept, not hidden — the switcher explains it.
    assert not api.supports(future.airflow_version)


def test_parse_deployments_tolerates_junk() -> None:
    assert api.parse_deployments(None) == []
    assert api.parse_deployments({"deployments": "nope"}) == []
    assert api.parse_deployments({"deployments": [{}]})[0].name == "(unnamed)"


def test_parse_dag_runs() -> None:
    runs = api.parse_dag_runs(DAG_RUNS_PAYLOAD)
    assert [r.dag_id for r in runs] == ["sync_alpha", "cw_beta", "monitoring"]
    failed, running, ok = runs
    assert failed.state == "failed"
    assert failed.run_id == "scheduled__2026-07-24T20:40:00+00:00"
    assert failed.duration is not None and 1800 < failed.duration < 1815
    assert running.state == "running"
    assert running.duration is None  # started, not ended: no total yet
    assert running.note == "kicked off by hand"
    assert ok.run_type == "scheduled"


def test_a_null_logical_date_run_still_says_when_it_belongs() -> None:
    """A manually triggered Airflow 3 run can have no logical date at all —
    v2's `logical_date` is nullable and our own trigger body sends null. Its
    `run_after` (v2-only, absent on v1) is what keeps the run sorting to the
    top of the list while it is still queued, instead of to the bottom."""
    queued = api.parse_dag_run(
        {
            "dag_id": "d",
            "dag_run_id": "manual__2026-07-27T15:00:00+00:00",
            "state": "queued",
            "run_type": "manual",
            "logical_date": None,
            "run_after": "2026-07-27T15:00:00Z",
            "start_date": None,
            "end_date": None,
        }
    )
    assert queued is not None
    assert queued.logical_date is None
    assert queued.run_after is not None
    assert queued.sort_date == queued.run_after
    # v1 rows have no `run_after`; the parse stays lenient and the sort falls
    # back exactly as before.
    v1 = api.parse_dag_runs(DAG_RUNS_PAYLOAD)
    assert all(r.run_after is None for r in v1)


def test_parse_dag_run_reads_the_bare_trigger_response() -> None:
    single = dict(DAG_RUN_ROWS[0])
    parsed = api.parse_dag_run(single)
    assert parsed is not None and parsed.run_id == single["dag_run_id"]
    assert api.parse_dag_run({}) is None
    assert api.parse_dag_run(None) is None


def test_parse_task_instances() -> None:
    tasks = api.parse_task_instances(TASK_INSTANCES_PAYLOAD)
    sensor, mapped = tasks
    assert sensor.task_id == "sensor"
    assert sensor.state == "failed"
    assert sensor.failed
    assert sensor.try_number == 1 and sensor.max_tries == 2
    assert sensor.tries == (1,)
    assert sensor.display_id == "sensor"
    assert mapped.map_index == 3
    assert mapped.display_id == "databricks_sync[3]"
    assert mapped.state == "upstream_failed"
    assert mapped.failed  # upstream_failed is still "the thing that broke"
    assert mapped.duration is None
    assert mapped.tries == (1,)  # never ran, but attempt 1's log endpoint exists


def test_parse_dags() -> None:
    dags = api.parse_dags(DAGS_PAYLOAD, V1)
    alpha, beta = dags
    assert alpha.dag_id == "sync_alpha"
    assert alpha.is_paused
    assert alpha.owners == ("data",)
    assert alpha.tags == ("databricks", "data")
    assert alpha.next_dagrun is not None
    assert alpha.schedule == "40 * * * *"  # out of v1's typed schedule_interval
    assert alpha.is_active and not alpha.is_stale
    assert not beta.is_paused
    assert beta.has_import_errors
    assert beta.tags == ()
    assert beta.next_dagrun is None


def test_parse_dags_v2_reads_the_renamed_fields() -> None:
    """Three fields moved in Airflow 3 and one of them inverted. Reading the v1
    names off a v2 row would report every DAG as active with no schedule."""
    alpha, beta = api.parse_dags(DAGS_V2_PAYLOAD, V2)
    # `is_stale` is the inverse of v1's `is_active`.
    assert alpha.is_active and not alpha.is_stale
    assert beta.is_stale and not beta.is_active
    # `next_dagrun_run_after`, not `next_dagrun`.
    assert alpha.next_dagrun is not None
    assert alpha.next_dagrun.hour == 22
    assert beta.next_dagrun is None
    # `timetable_summary` (a plain cron string), falling back to the prose form.
    assert alpha.schedule == "40 * * * *"
    assert beta.schedule == "Never, external triggers only"
    # Everything else is spelled the same as v1 and still lands.
    assert alpha.is_paused and not beta.is_paused
    assert alpha.tags == ("databricks", "data")
    assert alpha.owners == ("data",)
    assert beta.has_import_errors
    assert beta.fileloc.endswith("broken.py")


def test_parse_dags_v2_treats_an_absent_is_stale_as_present() -> None:
    """Lenient in the same direction v1 is: the exceptional case has to be
    stated, so a row missing the flag is a live DAG rather than a stale one."""
    dags = api.parse_dags({"dags": [{"dag_id": "d"}]}, V2)
    assert dags[0].is_active
    assert not dags[0].is_stale
    assert dags[0].schedule == ""


def test_parse_dags_reads_each_dialect_only_in_its_own_version() -> None:
    """The dispatch is the point: v2's names mean nothing to the v1 reader and
    vice versa, which is why the version has to be passed rather than sniffed."""
    v2_row_as_v1 = api.parse_dags(DAGS_V2_PAYLOAD, V1)[1]
    assert v2_row_as_v1.is_active  # `is_stale: true` is invisible to the v1 reader
    v1_row_as_v2 = api.parse_dags(DAGS_PAYLOAD, V2)[0]
    assert v1_row_as_v2.next_dagrun is None  # v1's `next_dagrun` likewise


# --- a stale DAG's leftover import-error flag ---------------------------------
#
# Measured on a real 889-DAG deployment: 51 DAGs carried `has_import_errors`
# while `/importErrors` held zero entries, and every one of the 51 was stale.
# Airflow sets the flag when a file fails to parse and never clears it once the
# file is gone, so the flag alone is not evidence of a live problem.


def _stale_flagged(fileloc: str = "/dags/gone.py") -> Dag:
    return Dag(
        dag_id="vendor_sync", has_import_errors=True, is_active=False, fileloc=fileloc
    )


def test_stale_dag_with_a_leftover_flag_is_not_a_live_import_error() -> None:
    dag = _stale_flagged()
    assert dag.has_import_errors  # Airflow still says so
    assert not dag.import_error_is_live(frozenset())  # but nothing is failing now


def test_stale_dag_is_a_live_import_error_when_its_file_still_fails() -> None:
    dag = _stale_flagged("/dags/gone.py")
    assert dag.import_error_is_live(frozenset({"/dags/gone.py"}))


def test_active_dag_keeps_its_flag_when_the_error_list_says_nothing() -> None:
    """`/importErrors` is not paged, so absence is not proof for a live DAG."""
    dag = Dag(dag_id="sync_beta", has_import_errors=True, fileloc="/dags/beta.py")
    assert dag.import_error_is_live(frozenset())


def test_stale_dag_shows_the_stale_marker_not_the_import_error_marker() -> None:
    """The precedence bug: a red import-error marker hid the true signal."""
    marker = ui.dag_marker(_stale_flagged())
    assert marker.plain == "✂"
    assert "red" not in str(marker.style)
    row = ui.dag_row(_stale_flagged(), NOW)
    assert "stale" in row[1].plain
    assert "import error" not in row[1].plain


def test_live_import_error_outranks_stale_in_the_marker() -> None:
    live = frozenset({"/dags/gone.py"})
    assert ui.dag_marker(_stale_flagged(), live).plain == "⚠"
    assert "import error" in ui.dag_row(_stale_flagged(), NOW, live)[1].plain


def test_live_import_error_files_reads_the_filenames() -> None:
    errors = tuple(api.parse_import_errors(IMPORT_ERRORS_PAYLOAD))
    files = live_import_error_files(errors)
    assert any(name.endswith("broken.py") for name in files)
    assert live_import_error_files(()) == frozenset()


def test_parse_dags_reads_fileloc() -> None:
    _, beta = api.parse_dags(DAGS_PAYLOAD, V1)
    assert beta.fileloc  # needed to match against the import-error list


def test_parse_import_errors() -> None:
    errors = api.parse_import_errors(IMPORT_ERRORS_PAYLOAD)
    assert len(errors) == 1
    entry = errors[0]
    assert entry.filename.endswith("broken.py")
    assert entry.short_filename == "broken.py"
    assert "ImportError: boom" in entry.stacktrace
    assert entry.timestamp is not None


def test_parse_log_unwraps_v1s_tuple_repr() -> None:
    log = api.parse_log(LOG_PAYLOAD, 1, V1)
    assert log.try_number == 1
    assert log.continuation_token == LOG_PAYLOAD["continuation_token"]
    # The `[('', "...")]` wrapper is gone and the text is readable lines.
    assert not log.content.startswith("[(")
    assert "Found logs in s3" in log.content
    assert log.lines[0].strip().startswith("INFO - ::group::")
    assert len(log.lines) == 4


def test_parse_log_falls_back_to_the_raw_string() -> None:
    plain = api.parse_log({"content": "just some text"}, 2, V1)
    assert plain.content == "just some text"
    assert plain.try_number == 2
    assert plain.continuation_token is None
    # A repr we cannot evaluate is shown rather than swallowed.
    broken = api.parse_log({"content": "[('', unterminated"}, 1, V1)
    assert broken.content == "[('', unterminated"
    assert api.parse_log(None, 1, V1).content == ""


def test_parse_log_accepts_a_real_list_too() -> None:
    log = api.parse_log(
        {"content": [["host-a", "line one\n"], ["host-b", "line two"]]}, 1, V1
    )
    assert log.content == "line one\nline two"


def test_parse_log_v2_flattens_structured_events_to_lines() -> None:
    """Airflow 3 returns log *events*, not text. The pane renders lines and the
    `/` filter matches lines, so each event becomes one — timestamp, level and
    the event itself, with the bookkeeping keys dropped."""
    log = api.parse_log(LOG_V2_PAYLOAD, 1, V2)
    assert log.try_number == 1
    assert log.continuation_token == LOG_V2_PAYLOAD["continuation_token"]
    lines = log.lines
    assert len(lines) == 4  # three events plus the bare string
    assert lines[0] == (
        "2026-07-27T09:12:01.004+00:00 info ::group::Log message source details"
    )
    assert lines[2] == "2026-07-27T09:41:13.710+00:00 error Task failed with exception"
    # The keys we do not render are gone rather than dumped into the line.
    assert "logger" not in log.content
    assert "sources" not in log.content
    assert "chan" not in log.content
    # A bare string in the array passes straight through.
    assert lines[3] == "*** Found logs in s3:"


def test_parse_log_v2_shows_something_for_a_shape_it_cannot_read() -> None:
    """Never nothing: an unreadable log is a monitoring failure, and the whole
    point of the pane is that the text arrives."""
    # An event dict with no `event` key still shows its contents.
    odd = api.parse_log({"content": [{"msg": "no event key"}]}, 1, V2)
    assert "no event key" in odd.content
    # A partial event drops the missing pieces rather than printing blanks.
    assert api.parse_log({"content": [{"event": "bare"}]}, 1, V2).content == "bare"
    # A non-string event is stringified rather than skipped.
    assert "42" in api.parse_log({"content": [{"event": 42}]}, 1, V2).content
    # Whole-body fallbacks: a plain string body, junk, and nothing at all.
    assert api.parse_log({"content": "plain text body"}, 1, V2).content == "plain text body"
    assert api.parse_log({"content": {"unexpected": "dict"}}, 1, V2).content
    assert api.parse_log({"content": None}, 1, V2).content == ""
    assert api.parse_log(None, 1, V2).content == ""


def test_parse_log_v2_output_is_lines_the_pane_can_work_with() -> None:
    """The flattened form is what everything downstream operates on — the line
    counter, the `/` filter, the truncation cut — so it has to behave like the
    v1 text did."""
    events = [{"event": f"line {n:04d}", "level": "info"} for n in range(200)]
    log = api.parse_log({"content": events}, 1, V2)
    assert len(log.lines) == 200
    hits, total = filter_log(log.content, "line 0007")
    assert total == 200 and len(hits) == 1
    assert hits[0][0] == 8  # 1-based line number in the unfiltered log


def test_parse_error_detail_prefers_detail() -> None:
    body = {"detail": "The DAG with dag_id: nope was not found", "status": 404, "title": "DAG not found"}
    assert api.parse_error_detail(body) == "The DAG with dag_id: nope was not found"
    assert api.parse_error_detail({"title": "Bad"}) == "Bad"
    assert api.parse_error_detail({}) is None
    assert api.parse_error_detail("nope") is None


def test_parse_error_detail_reads_a_v2_validation_list() -> None:
    """FastAPI answers a bad body with `detail` as a *list*, which the RFC-7807
    string lookup walks straight past — leaving the user a raw JSON tail."""
    detail = api.parse_error_detail(VALIDATION_ERROR_V2_PAYLOAD)
    assert detail is not None
    assert "Field required" in detail
    assert "body.logical_date" in detail  # says *which* field
    assert "+1 more" in detail  # and that it was not the only one
    # A single error needs no count, and an unreadable entry still says
    # something rather than falling back to None.
    one = api.parse_error_detail({"detail": [{"msg": "Bad value", "loc": ["query", "limit"]}]})
    assert one == "query.limit: Bad value"
    assert api.parse_error_detail({"detail": ["just a string"]}) == "just a string"
    assert api.parse_error_detail({"detail": []}) is None


# --- ADR constraint: no state enum is closed -------------------------------


def test_unknown_states_render_in_the_fallback_bucket_and_never_raise() -> None:
    """A 2.x patch release inventing a state must not break the tool."""
    # `awaiting_input` is a real Airflow 3 state this build has never seen.
    for unknown in ("awaiting_input", "brand_new_state", "  ", "SUCCESS!"):
        glyph, style = ui.state_style(unknown)
        assert (glyph, style) == ui.FALLBACK_STATE_STYLE, unknown
        cell = ui.state_cell(unknown)
        assert cell.plain  # renders something
        assert cell.style == ui.FALLBACK_STATE_STYLE[1]


def test_absent_state_normalizes_to_none_rather_than_the_unknown_bucket() -> None:
    # A *missing* state is not an unrecognized one — it means "not set yet", and
    # Airflow spells that "none", so it gets the known neutral style.
    assert ui.state_style("") == ui.state_style("none")
    assert ui.state_cell("").plain == "— none"


def test_every_known_state_has_its_own_style() -> None:
    for state in KNOWN_RUN_STATES + KNOWN_TASK_STATES:
        assert ui.state_style(state) != ui.FALLBACK_STATE_STYLE, state


def test_parsers_accept_an_unknown_state_verbatim() -> None:
    runs = api.parse_dag_runs({"dag_runs": [{"dag_id": "d", "dag_run_id": "r", "state": "awaiting_input"}]})
    assert runs[0].state == "awaiting_input"
    tasks = api.parse_task_instances({"task_instances": [{"task_id": "t", "state": "hypothetical"}]})
    assert tasks[0].state == "hypothetical"
    # A missing state becomes "none" rather than None, so nothing downstream
    # has to guard against it.
    assert api.parse_dag_runs({"dag_runs": [{"dag_id": "d", "dag_run_id": "r"}]})[0].state == "none"


def test_unknown_state_survives_a_full_render() -> None:
    run = _run_(state="awaiting_input", end=NOW)
    row = ui.list_row(run, NOW)
    assert "awaiting_input" in row[4].plain
    console = Console(width=140)
    with console.capture() as cap:
        console.print(ui.render_once(_snapshot(runs=(run,)), NOW))
    assert "awaiting_input" in cap.get()


# --- models ---------------------------------------------------------------


def test_run_duration_and_attention() -> None:
    finished = _run_(start=NOW - timedelta(seconds=90), end=NOW)
    assert finished.duration == 90
    assert not finished.needs_attention  # success
    assert _run_(state="failed").needs_attention
    assert _run_(state="running").needs_attention
    assert _run_(state="queued").needs_attention
    assert _run_(state="skipped", end=NOW).duration is not None
    # No start date at all: no duration, and the sort key still works.
    orphan = DagRun(dag_id="d", run_id="r", state="failed")
    assert orphan.duration is None
    assert orphan.sort_date.year == 1


def test_task_tries_and_duration() -> None:
    assert _task(try_number=3).tries == (1, 2, 3)
    assert _task(try_number=0).tries == (1,)
    task = TaskInstance(task_id="t", state="failed", start_date=NOW, end_date=None)
    assert task.duration is None


def test_deployment_hibernation_from_status_string() -> None:
    assert _deployment(status="HIBERNATING").is_hibernating
    assert _deployment(hibernating=True).is_hibernating
    assert not _deployment().is_hibernating


def test_plain_airflow_deployment_is_addressed_by_url() -> None:
    plain = Deployment(id="", name="local", api_url="http://localhost:8080")
    assert not plain.is_astro
    assert plain.key == "http://localhost:8080"
    assert plain.label == "local"


def test_sort_runs_attention_first_then_newest() -> None:
    calm_new = _run_("a", run_id="1", state="success", start=NOW, end=NOW)
    calm_old = _run_("b", run_id="2", state="success", start=NOW - timedelta(days=1), end=NOW)
    hot_old = _run_("c", run_id="3", state="failed", start=NOW - timedelta(days=3))
    assert [r.dag_id for r in sort_runs([calm_old, calm_new, hot_old])] == ["c", "a", "b"]


def test_sort_task_instances_failed_first() -> None:
    ok = _task("z_ok")
    bad = _task("a_bad", state="failed")
    upstream = _task("m_up", state="upstream_failed")
    ordered = sort_task_instances([ok, upstream, bad])
    assert [t.task_id for t in ordered[:2]] == ["a_bad", "m_up"]
    assert ordered[-1].task_id == "z_ok"


# --- dependency ordering ---------------------------------------------------


def _tasks(*task_ids: str) -> list[TaskInstance]:
    return [_task(task_id) for task_id in task_ids]


# Annotated explicitly: a bare literal infers as `tuple[str] | tuple[()]`,
# which is not the `tuple[str, ...]` the graph is declared with.
Graph = dict[str, tuple[str, ...]]


def _labels(rows: list[TaskRow]) -> list[str]:
    return [row.label for row in rows]


def test_order_task_instances_renders_a_chain_as_a_tree() -> None:
    graph: Graph = {
        "extract_prices": ("validate_prices",),
        "validate_prices": ("load_to_delta",),
        "load_to_delta": ("notify_slack",),
        "notify_slack": (),
        "reconcile_positions": (),
    }
    rows = order_task_instances(
        _tasks(
            "notify_slack",
            "reconcile_positions",
            "load_to_delta",
            "extract_prices",
            "validate_prices",
        ),
        graph,
    )
    assert _labels(rows) == [
        "extract_prices",
        "└─ validate_prices",
        "   └─ load_to_delta",
        "      └─ notify_slack",
        "reconcile_positions",
    ]
    assert [row.position for row in rows] == [1, 2, 3, 4, 5]
    assert [row.depth for row in rows] == [0, 1, 2, 3, 0]
    assert not any(row.unplaced for row in rows)


def test_order_task_instances_marks_siblings_with_a_tee() -> None:
    graph: Graph = {"root": ("beta", "alpha"), "alpha": (), "beta": ()}
    rows = order_task_instances(_tasks("root", "alpha", "beta"), graph)
    # Siblings are visited in task-id order regardless of the edge order, so the
    # rows do not reshuffle between refreshes.
    assert _labels(rows) == ["root", "├─ alpha", "└─ beta"]


def test_order_task_instances_places_a_diamond_after_both_upstreams() -> None:
    """The join must come after *both* branches, not after whichever the walk
    reached first — otherwise an upstream would sit below what it feeds."""
    graph: Graph = {"a": ("b", "c"), "b": ("d",), "c": ("d",), "d": ()}
    rows = order_task_instances(_tasks("a", "b", "c", "d"), graph)
    order = [row.task.task_id for row in rows]
    assert order.index("d") > order.index("b")
    assert order.index("d") > order.index("c")
    assert order[0] == "a"
    assert len(rows) == 4


def test_order_task_instances_handles_disconnected_components() -> None:
    graph: Graph = {"a": ("b",), "b": (), "x": ("y",), "y": ()}
    rows = order_task_instances(_tasks("a", "b", "x", "y"), graph)
    assert _labels(rows) == ["a", "└─ b", "x", "└─ y"]


def test_order_task_instances_is_cycle_safe_and_never_drops_a_task() -> None:
    """A cycle cannot be topologically ordered, so its members go to a marked
    trailing group. Losing a task from a monitoring view is a correctness bug."""
    graph: Graph = {"a": ("b",), "b": ("c",), "c": ("a",), "standalone": ()}
    rows = order_task_instances(_tasks("a", "b", "c", "standalone"), graph)
    assert len(rows) == 4  # total, whatever the graph says
    assert [row.task.task_id for row in rows if not row.unplaced] == ["standalone"]
    assert [row.task.task_id for row in rows if row.unplaced] == ["a", "b", "c"]
    assert [row.position for row in rows] == [1, 2, 3, 4]


def test_order_task_instances_keeps_a_task_with_no_graph_entry() -> None:
    """A task instance the DAG's structure does not mention — a task removed
    since the run, say — is still a row."""
    graph: Graph = {"a": ("b",), "b": ()}
    rows = order_task_instances(_tasks("a", "b", "ghost"), graph)
    assert len(rows) == 3
    assert "ghost" in [row.task.task_id for row in rows]
    # It has no upstream, so it is placeable as a root rather than unlinked.
    ghost = next(row for row in rows if row.task.task_id == "ghost")
    assert not ghost.unplaced


def test_order_task_instances_drops_edges_through_a_missing_task() -> None:
    """`b` is defined but not in this run (a skipped branch); `c` must still be
    placed rather than stranded behind an upstream that will never arrive."""
    graph: Graph = {"a": ("b",), "b": ("c",), "c": ()}
    rows = order_task_instances(_tasks("a", "c"), graph)
    assert len(rows) == 2
    assert not any(row.unplaced for row in rows)
    assert _labels(rows) == ["a", "c"]


def test_order_task_instances_groups_mapped_instances_together() -> None:
    graph: Graph = {"fan": ("collect",), "collect": ()}
    mapped = [_task("fan", map_index=index) for index in (2, 0, 1)]
    rows = order_task_instances([*mapped, _task("collect")], graph)
    # All of a task's instances sit at that task's position, by map index.
    assert [row.task.display_id for row in rows] == [
        "fan[0]",
        "fan[1]",
        "fan[2]",
        "collect",
    ]
    assert _labels(rows)[-1] == "└─ collect"
    assert [row.depth for row in rows] == [0, 0, 0, 1]


def test_order_task_instances_without_a_graph_falls_back_to_start_order() -> None:
    tasks = [_task("z_ok"), _task("a_bad", state="failed")]
    for graph in (None, {}):
        rows = order_task_instances(tasks, graph)
        assert [row.task.task_id for row in rows] == ["a_bad", "z_ok"]
        assert all(row.depth == 0 and row.prefix == "" for row in rows)
    assert order_task_instances([], {"a": ()}) == []


def test_order_task_instances_is_deterministic_across_input_orders() -> None:
    graph: Graph = {"a": ("b", "c"), "b": ("d",), "c": ("d",), "d": (), "e": ()}
    names = ["a", "b", "c", "d", "e"]
    first = _labels(order_task_instances(_tasks(*names), graph))
    for shuffled in ([*reversed(names)], ["d", "a", "e", "c", "b"]):
        assert _labels(order_task_instances(_tasks(*shuffled), graph)) == first


def test_parse_task_graph() -> None:
    payload = {
        "tasks": [
            {"task_id": "extract", "downstream_task_ids": ["load"], "operator_name": "Py"},
            {"task_id": "load", "downstream_task_ids": []},
            {"downstream_task_ids": ["orphan"]},  # no task_id: skipped
        ],
        "total_entries": 3,
    }
    assert api.parse_task_graph(payload) == {"extract": ("load",), "load": ()}
    assert api.parse_task_graph(None) == {}
    assert api.parse_task_graph({"tasks": "nope"}) == {}


def test_action_titles_and_targets() -> None:
    pause = Action(kind="pause", dag_id="sync_alpha")
    assert pause.title == "Pause DAG"
    assert pause.target == "sync_alpha"
    assert pause.mutates
    dry = Action(kind="clear", dag_id="d", run_id="r", task_ids=("t",), dry_run=True)
    assert dry.title.endswith("dry run")
    assert not dry.mutates  # a dry run changes nothing, by definition
    assert dry.target == "d · r · t"
    mark = Action(kind="mark", dag_id="d", state="success")
    assert "success" in mark.title
    assert set(ACTION_KINDS) == {"pause", "unpause", "trigger", "clear", "mark"}


def test_snapshot_helpers() -> None:
    snapshot = _snapshot(dags=(_dag("sync_alpha", paused=True), _dag("sync_beta")))
    assert snapshot.paused_count == 1
    assert snapshot.dag("sync_alpha") is not None
    assert snapshot.dag("nope") is None


# --- filtering -------------------------------------------------------------


def test_matches_is_case_insensitive_and_all_terms() -> None:
    assert matches("", "anything")
    assert matches("   ", "anything")
    assert matches("SYNC", "sync_alpha failed")
    assert matches("failed sync", "sync_alpha failed")  # order does not matter
    assert not matches("failed sync", "sync_alpha success")
    assert not matches("nope", "sync_alpha")


def test_search_text_covers_the_fields_a_human_would_type() -> None:
    run = _run_("sync_alpha", state="failed", run_type="scheduled")
    assert matches("sync failed", run.search_text)
    assert matches("scheduled", run.search_text)
    dag = Dag(dag_id="sync_alpha", owners=("data",), tags=("databricks",), description="Nightly")
    assert matches("databricks", dag.search_text)
    assert matches("data nightly", dag.search_text)
    task = _task("sensor", state="failed", operator="S3KeySensor")
    assert matches("s3key", task.search_text)


def test_filter_log_keeps_original_line_numbers() -> None:
    content = "alpha\nbeta ERROR\ngamma\ndelta error\n"
    hits, total = filter_log(content, "error")
    assert total == 4
    assert hits == [(2, "beta ERROR"), (4, "delta error")]
    # An empty query is every line, still numbered from one.
    everything, total = filter_log(content, "")
    assert total == 4
    assert everything[0] == (1, "alpha")
    assert filter_log("", "anything") == ([], 0)


def test_highlight_marks_every_term_occurrence() -> None:
    marked = ui.highlight("error: a real error", "error")
    assert marked.plain == "error: a real error"
    spans = [(span.start, span.end) for span in marked.spans]
    assert (0, 5) in spans
    assert (14, 19) in spans
    assert ui.highlight("plain", "").spans == []


def test_render_log_filters_and_reports_the_match_count() -> None:
    task = _task("sensor", state="failed", try_number=1, max_tries=1)
    log = TaskLog(content="alpha\nbeta ERROR\ngamma\n", try_number=1)
    out = _plain_renderable(ui.render_log(task, log, query="error"))
    assert "beta ERROR" in out
    assert "alpha" not in out  # non-matching lines are filtered out
    assert "1 of 3 lines match" in out
    assert "2" in out  # the original line number survives the filter
    empty = _plain_renderable(ui.render_log(task, log, query="zzz"))
    assert "No log line matches" in empty


def test_render_log_bounds_what_it_renders(monkeypatch) -> None:
    """The CLI transport cannot stream, so a log arrives whole; the pane must
    still not try to render an unbounded one — and must say when it stopped."""
    monkeypatch.setattr(ui, "MAX_LOG_LINES", 5)
    task = _task("sensor")
    log = TaskLog(content="\n".join(f"line {n}" for n in range(50)), try_number=1)
    out = _plain_renderable(ui.render_log(task, log))
    assert "line 0" in out
    assert "line 49" not in out
    assert "45 more lines not shown" in out
    assert "50 in this attempt" in out


def test_render_filter_prompt_shows_what_is_being_typed() -> None:
    out = _plain_renderable(ui.render_filter_prompt("runs", "syn"))
    assert "/syn" in out
    assert "filtering runs" in out


# --- ADR constraint: credential redaction ---------------------------------


def test_redact_removes_jwt_shaped_tokens() -> None:
    text = f"request used token {FAKE_JWT} against /dags"
    scrubbed = astro._redact(text)
    assert FAKE_JWT not in scrubbed
    assert astro.REDACTED in scrubbed
    assert "/dags" in scrubbed  # only the credential goes


def test_redact_removes_authorization_headers_in_any_shape() -> None:
    for text in (
        f"Authorization: Bearer {FAKE_JWT}",
        f"authorization={FAKE_JWT}",
        f"-H 'Authorization: {FAKE_JWT}'",
        f"bearer {FAKE_JWT}",
    ):
        scrubbed = astro._redact(text)
        assert FAKE_JWT not in scrubbed, text
        assert astro.REDACTED in scrubbed


def test_poll_error_messages_are_redacted() -> None:
    exc = AstroError(
        f"`astro api airflow -d dep-prod-1 /dags` failed: 401 "
        f'{{"detail": "token {FAKE_JWT} rejected"}}'
    )
    error = astro.classify_astro_error(exc)
    assert FAKE_JWT not in error.message
    # ...and no raw command dump reaches the UI either.
    assert "astro api airflow" not in error.message


def test_activity_log_entries_are_redacted(monkeypatch) -> None:
    """The log is a displayed surface, so it goes through the same scrub."""
    def boom(args, **kwargs):
        raise AstroError(f"`astro api cloud ListDeployments` failed: Bearer {FAKE_JWT}")

    monkeypatch.setattr(astro, "_run", boom)
    try:
        astro.list_deployments()
    except AstroError as exc:
        error = astro.classify_astro_error(exc)
    assert FAKE_JWT not in error.message
    entry = LogEntry(time=NOW, level="error", message=error.message)
    console = Console(width=100)
    with console.capture() as cap:
        console.print(ui.render_activity_log([entry]))
    assert FAKE_JWT not in cap.get()


def test_display_command_truncates_and_redacts() -> None:
    shown = astro._display_command(["astro", "api", "airflow", "-H", f"Authorization: Bearer {FAKE_JWT}"])
    assert FAKE_JWT not in shown
    assert len(shown) < 200


def _code_string_literals(path: pathlib.Path) -> list[str]:
    """Every string literal in a module that is *not* a docstring.

    Prose is allowed to name a forbidden command in order to explain why it is
    forbidden; only executable code must not.
    """
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_credential_emitting_astro_subcommand_is_reachable() -> None:
    """`astro auth token` and `astro api --generate` both print live credentials
    on stdout, so the ADR forbids them in any displayed code path. Assert no
    executable string literal names either — docstrings may, and do, explain why.
    """
    forbidden = ("auth token", "--generate", "--verbose", "--include")
    for path in sorted(_PACKAGE.glob("*.py")):
        for literal in _code_string_literals(path):
            for banned in forbidden:
                assert banned not in literal, f"{path.name} builds {literal!r}"


# --- error classification (one case per row of the plan's table) -----------


def test_classify_missing_cli() -> None:
    error = astro.classify_astro_error(AstroError("`astro` is not installed or not on PATH."))
    assert error.kind == "missing_cli"
    assert "astro login" in error.message
    assert not error.recoverable  # retrying on a timer cannot help


def test_classify_not_authenticated() -> None:
    # The real 1.43.1 message, captured against an empty credential store.
    raw = AstroError(
        "`astro api cloud ListDeployments` failed: Error: getting current context: "
        "no context set, have you authenticated to Astro or Astro Private Cloud? "
        "Run astro login and try again"
    )
    error = astro.classify_astro_error(raw)
    assert error.kind == "auth"
    assert error.message == "Astro authentication failed — run `astro login`."


def test_classify_hibernating() -> None:
    error = astro.classify_astro_error(
        AstroError("`astro api airflow` failed: deployment is hibernating")
    )
    assert error.kind == "hibernating"
    assert "hibernating" in error.message
    assert not error.rate_limited  # its own state, not a connection error


def test_classify_unsupported_version() -> None:
    error = astro.classify_astro_error(api.UnsupportedAirflowVersion(UNSUPPORTED_VERSION))
    assert error.kind == "unsupported_version"
    assert UNSUPPORTED_VERSION in error.message
    assert not error.recoverable


def test_classify_rate_limited_reads_retry_after() -> None:
    error = astro.classify_astro_error(
        AstroError("`astro api airflow` failed: 429 Too Many Requests. Retry-After: 45")
    )
    assert error.kind == "rate_limited"
    assert error.rate_limited
    assert error.retry_after == 45


def test_classify_rate_limited_without_a_hint() -> None:
    error = astro.classify_astro_error(AstroError("`astro api airflow` failed: 429 too many requests"))
    assert error.rate_limited and error.retry_after is None


def test_classify_forbidden_is_not_an_auth_error() -> None:
    error = astro.classify_astro_error(
        AstroError("`astro api airflow` failed: API request failed with status 403")
    )
    assert error.kind == "forbidden"
    assert "workspace role" in error.message


def test_classify_not_found_quotes_airflows_own_detail() -> None:
    # The real shape: status on stderr, RFC-7807 body on stdout, both folded in.
    raw = AstroError(
        '`astro api airflow -d dep /dags/nope` failed: Error: API request failed '
        'with status 404 {"detail": "The DAG with dag_id: nope was not found", '
        '"status": 404, "title": "DAG not found"}'
    )
    error = astro.classify_astro_error(raw)
    assert error.kind == "not_found"
    assert error.message == "The DAG with dag_id: nope was not found"


def test_classify_a_deployment_that_does_not_exist() -> None:
    """The ADR names "deployment not found" as its own actionable mode; it also
    must not retry on the minute, since a name that cannot resolve never will."""
    deployments = api.parse_deployments(DEPLOYMENTS_PAYLOAD)
    try:
        astro.resolve_deployment(deployments, "nope")
    except AstroError as exc:
        error = astro.classify_astro_error(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown deployment must be reported")
    assert error.kind == "not_found"
    assert "Production" in error.message  # it still says what was available
    assert not error.recoverable


def test_classify_unknown_truncates_and_drops_the_command() -> None:
    error = astro.classify_astro_error(
        AstroError("`astro api airflow -d dep /dags` failed: " + "x" * 900)
    )
    assert error.kind == "unknown"
    assert "astro api airflow" not in error.message
    assert len(error.message) <= 240
    assert error.message.endswith("…")


def test_classify_timeout() -> None:
    error = astro.classify_astro_error(AstroError("`astro api airflow /dags` timed out after 30s."))
    assert error.kind == "unknown"
    assert "timed out" in error.message


def test_every_error_kind_is_reachable() -> None:
    """The taxonomy is only useful if the classifier can actually produce it."""
    produced = {
        astro.classify_astro_error(exc).kind
        for exc in (
            AstroError("`astro` is not installed or not on PATH."),
            AstroError("`x` failed: no context set"),
            AstroError("`x` failed: hibernating"),
            api.UnsupportedAirflowVersion(UNSUPPORTED_VERSION),
            AstroError("`x` failed: 429"),
            AstroError("`x` failed: status 403"),
            AstroError("`x` failed: status 404"),
            AstroError("`x` failed: something else entirely"),
        )
    }
    assert produced == set(astro.KINDS)


# --- the transport seam (astro._run faked) --------------------------------


def _fake_run(captured: list[list[str]], responses: list[str]):
    """A stand-in for `astro._run` that records argv and replays canned JSON."""

    def run(args: list[str], *, timeout: float = 30.0, input_text: str | None = None) -> str:
        captured.append(args)
        if input_text is not None:
            captured[-1] = args + ["<stdin>", input_text]
        return responses.pop(0) if responses else "{}"

    return run


# The snapshot fan-out runs its calls concurrently, so a positional response
# queue would be a flaky test. Route on the requested path instead.
_ROUTES = {
    "dagRuns?": json.dumps(DAG_RUNS_PAYLOAD),
    "/dags?": json.dumps(DAGS_PAYLOAD),
    "/importErrors": json.dumps(IMPORT_ERRORS_PAYLOAD),
    "/taskInstances?": json.dumps(TASK_INSTANCES_PAYLOAD),
    "/logs/": json.dumps(LOG_PAYLOAD),
}


def _fake_routed_run(captured: list[list[str]], extra: dict[str, str] | None = None):
    """A stand-in for `astro._run` that answers by path, order-independently."""
    routes = {**_ROUTES, **(extra or {})}

    def run(args: list[str], *, timeout: float = 30.0, input_text: str | None = None) -> str:
        captured.append(args if input_text is None else args + ["<stdin>", input_text])
        path = args[-1]
        for marker, payload in routes.items():
            if marker in path:
                return payload
        return "{}"

    return run


def test_list_deployments_calls_the_cloud_api(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(captured, [json.dumps(DEPLOYMENTS_PAYLOAD)]))
    deployments = astro.list_deployments()
    assert [d.name for d in deployments] == ["Production", "Dev", "Next", "Future"]
    assert captured[0] == ["astro", "api", "cloud", "ListDeployments"]


def test_every_airflow_invocation_pins_the_airflow_version(monkeypatch) -> None:
    """ADR constraint: no call may pay for version auto-detection. Checked on
    both majors, since the version now decides paths as well as pinning."""
    for version in (V1, V2):
        captured: list[list[str]] = []
        monkeypatch.setattr(astro, "_run", _fake_routed_run(captured))
        deployment = _deployment(version=version)
        run = _run_()
        task = _task()
        astro.fetch_snapshot(deployment, limit=5)
        astro.fetch_run_tasks(deployment, run)
        astro.fetch_log(deployment, run, task, 1)
        astro.perform(
            deployment,
            Action(kind="clear", dag_id="d", run_id="r", task_ids=("t",), dry_run=True),
        )

        # 3 for the snapshot fan-out, 2 for the drill (task instances + DAG
        # structure), 1 log, 1 mutation.
        assert len(captured) == 7, version
        for args in captured:
            assert args[:3] == ["astro", "api", "airflow"]
            assert "--airflow-version" in args
            assert args[args.index("--airflow-version") + 1] == version
            assert args[args.index("-d") + 1] == "dep-prod-1"
            # And never the flags that would flip the method to POST.
            assert "-f" not in args and "-F" not in args


def test_fetch_snapshot_issues_one_call_per_pane(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_routed_run(captured))
    snapshot = astro.fetch_snapshot(_deployment(), limit=5, states=("failed",))
    assert snapshot.calls == 3
    assert snapshot.elapsed >= 0
    # Newest-attention-first, straight out of sort_runs.
    assert [r.dag_id for r in snapshot.runs] == ["cw_beta", "sync_alpha", "monitoring"]
    assert snapshot.paused_count == 1
    assert len(snapshot.import_errors) == 1
    # The state filter rides in the path of the runs call only.
    runs_call = next(a for a in captured if "dagRuns" in a[-1])
    assert "state=failed" in runs_call[-1]


def test_fetch_snapshot_pages_the_whole_dag_list(monkeypatch) -> None:
    """v1 truncates a page at 100 without complaining, and the CLI's own
    --paginate does not work on this API, so we page by offset ourselves.
    Trusting one page would mis-describe every DAG past the hundredth —
    including whether it is paused, which the pause action depends on."""
    captured: list[list[str]] = []
    total = 242

    def routed(args: list[str], *, timeout: float = 30.0, input_text: str | None = None) -> str:
        captured.append(args)
        path = args[-1]
        if path.startswith("/dags?"):
            found = re.search(r"offset=(\d+)", path)
            offset = int(found.group(1)) if found else 0
            page = [
                {"dag_id": f"dag_{n:03d}", "is_paused": n % 2 == 0}
                for n in range(offset, min(offset + api.PAGE_LIMIT, total))
            ]
            return json.dumps({"dags": page, "total_entries": total})
        if "dagRuns?" in path:
            return json.dumps(DAG_RUNS_PAYLOAD)
        return json.dumps({"import_errors": [], "total_entries": 0})

    monkeypatch.setattr(astro, "_run", routed)
    snapshot = astro.fetch_snapshot(_deployment())
    assert len(snapshot.dags) == total
    assert snapshot.paused_count == 121
    assert snapshot.dag("dag_241") is not None  # past the first page
    # runs + errors + three DAG pages, all reported.
    assert snapshot.calls == 5
    dag_calls = [a[-1] for a in captured if a[-1].startswith("/dags?")]
    assert len(dag_calls) == 3
    assert all(f"limit={api.PAGE_LIMIT}" in path for path in dag_calls)
    assert sorted("offset=" in path for path in dag_calls) == [False, True, True]


def test_dag_pagination_is_capped(monkeypatch) -> None:
    """A very large deployment truncates the DAG list rather than making a
    refresh take forever."""
    def routed(args: list[str], *, timeout: float = 30.0, input_text: str | None = None) -> str:
        if args[-1].startswith("/dags?"):
            return json.dumps({"dags": [{"dag_id": "d"}], "total_entries": 10_000})
        return "{}"

    monkeypatch.setattr(astro, "_run", routed)
    snapshot = astro.fetch_snapshot(_deployment())
    # runs page 1 + import errors + MAX_DAG_PAGES worth of DAG pages.
    assert snapshot.calls == 2 + astro.MAX_DAG_PAGES
    assert snapshot.dags_total == 10_000
    # And the truncation is *visible*, which is the whole point of the cap.
    assert snapshot.dags_truncated
    assert "10,000 dags" in ui.render_summary(snapshot, None, view="dags").plain
    assert "truncated" in ui.render_summary(snapshot, None, view="dags").plain


async def test_paged_runs_never_repeat_a_row(monkeypatch) -> None:
    """Offset paging can hand back the same run twice — the sort key is a date
    thousands of runs share, and rows are being inserted while we walk the
    offsets. A repeat would double-count the run *and* crash the next render,
    because the list window keys its rows by `run.key`."""
    overlap = DAG_RUN_ROWS[0]

    def routed(args: list[str], *, timeout: float = 30.0, input_text: str | None = None) -> str:
        path = args[-1]
        if "dagRuns?" in path:
            # Both pages contain `overlap`, as a shifting offset window would.
            page = (
                {"dag_runs": DAG_RUN_ROWS, "total_entries": 150}
                if "offset=" not in path
                else {"dag_runs": [overlap], "total_entries": 150}
            )
            return json.dumps(page)
        return json.dumps({"total_entries": 0})

    monkeypatch.setattr(astro, "_run", routed)
    snapshot = astro.fetch_snapshot(_deployment(), limit=150)
    keys = [run.key for run in snapshot.runs]
    assert len(keys) == len(set(keys)) == 3

    # ...and the table accepts every row, which is the failure being prevented.
    app = _app([(snapshot, None)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 3


def test_fetch_snapshot_reuses_a_supplied_dag_list(monkeypatch) -> None:
    """The DAG list is the expensive half of a poll and only changes on deploy,
    so a caller that already has it skips fetching it entirely."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_routed_run(captured))
    known = DagList(dags=(_dag("sync_alpha", paused=True),), total=1)
    snapshot = astro.fetch_snapshot(_deployment(), dags=known)
    assert snapshot.dags is known.dags
    assert snapshot.paused_count == 1
    assert snapshot.calls == 2  # runs + import errors only
    assert not any(a[-1].startswith("/dags?") for a in captured)


def test_a_reused_dag_list_still_says_it_was_truncated(monkeypatch) -> None:
    """The truncation flag and the server's total travel *with* the DAGs. A cache
    that dropped them would make an incomplete list look complete for the rest of
    its TTL — and would turn off the server-side DAG filter that a truncated list
    is exactly what makes necessary."""
    monkeypatch.setattr(astro, "_run", _fake_routed_run([]))
    known = DagList(dags=(_dag("sync_alpha"),), total=10_000, truncated=True)
    snapshot = astro.fetch_snapshot(_deployment(), dags=known)
    assert snapshot.dags_truncated
    assert snapshot.dags_total == 10_000
    assert "1 of 10,000 dags" in ui.render_summary(snapshot, None, view="dags").plain


def test_dag_cache_expires_and_can_be_dropped() -> None:
    from tools.airflow_watch.cli import _DagCache

    cache = _DagCache(ttl=100)
    dags = DagList(dags=(_dag("sync_alpha"),), total=1)
    cache.put("dep-prod-1", dags, now=1000.0)
    assert cache.get("dep-prod-1", now=1050.0) is dags
    assert cache.get("dep-prod-1", now=1200.0) is None  # past the TTL
    assert cache.get("other", now=1000.0) is None
    # A mutation we made ourselves invalidates it outright, so the immediate
    # re-poll tells the truth about is_paused.
    cache.put("dep-prod-1", dags, now=2000.0)
    cache.drop("dep-prod-1")
    assert cache.get("dep-prod-1", now=2000.0) is None
    cache.drop("never-there")  # dropping an absent key is not an error


def test_total_entries_is_read_leniently() -> None:
    assert api.total_entries({"total_entries": 242}) == 242
    assert api.total_entries({}) == 0
    assert api.total_entries({"total_entries": "many"}) == 0
    assert api.total_entries(None) == 0


def test_fetch_snapshot_refuses_an_unsupported_major(monkeypatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(called, []))
    for version in (UNSUPPORTED_VERSION, "1.10.15", ""):
        try:
            astro.fetch_snapshot(_deployment(version=version))
        except api.UnsupportedAirflowVersion as exc:
            assert (version or "unknown") in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"Airflow {version!r} must be refused")
    assert called == []  # refused at the boundary, before any request


def test_fetch_snapshot_accepts_an_airflow_3_deployment(monkeypatch) -> None:
    """The other half of the widened seam: 3.x is no longer a refusal, and the
    calls it makes carry the v2 spellings."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        astro, "_run", _fake_routed_run(captured, {"/dags?": json.dumps(DAGS_V2_PAYLOAD)})
    )
    snapshot = astro.fetch_snapshot(_deployment(version="3.3.0"), limit=5)
    assert snapshot.calls == 3
    dags_call = next(a[-1] for a in captured if a[-1].startswith("/dags?"))
    assert "exclude_stale=false" in dags_call and "only_active" not in dags_call
    runs_call = next(a[-1] for a in captured if "dagRuns?" in a[-1])
    assert "order_by=-run_after" in runs_call
    # ...and the v2 DAG rows were read with the v2 field names.
    assert snapshot.stale_count == 1
    assert snapshot.paused_count == 1
    alpha = snapshot.dag("sync_alpha")
    assert alpha is not None and alpha.schedule == "40 * * * *"


def test_fetch_snapshot_names_a_hibernating_deployment(monkeypatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(called, []))
    try:
        astro.fetch_snapshot(_deployment("Dev", hibernating=True))
    except AstroError as exc:
        error = astro.classify_astro_error(exc)
    else:  # pragma: no cover
        raise AssertionError("a hibernating deployment must be refused")
    assert error.kind == "hibernating"
    assert called == []


def test_plain_airflow_uses_api_url_instead_of_deployment_id(monkeypatch) -> None:
    for version, suffix in ((V1, "/api/v1"), (V2, "/api/v2")):
        captured: list[list[str]] = []
        monkeypatch.setattr(astro, "_run", _fake_routed_run(captured))
        plain = Deployment(
            id="", name="local", airflow_version=version, api_url="http://localhost:8080"
        )
        astro.fetch_snapshot(plain)
        assert captured
        for args in captured:
            assert "-d" not in args
            assert args[args.index("--api-url") + 1] == "http://localhost:8080" + suffix
            assert args[args.index("--airflow-version") + 1] == version


# --- the startup version probe (plain `--api-url` targets) -----------------
#
# Astro targets never probe: discovery reports `airflowVersion`. These cover the
# only case that has no oracle.

# What Airflow 3 answers a /api/v1 path with — distinctive enough to fall
# through on, and the reason probing the wrong major is cheap.
_V1_REMOVED = "`astro …` failed: API request failed with status 404 {\"detail\": \"/api/v1 has been removed in Airflow 3\"}"


def _probe_run(captured: list[list[str]], answer):
    """A `_run` stand-in that answers /version per pinned version."""

    def run(args: list[str], *, timeout: float = 30.0, input_text: str | None = None) -> str:
        captured.append(args)
        pinned = args[args.index("--airflow-version") + 1]
        result = answer(pinned)
        if isinstance(result, Exception):
            raise result
        return result

    return run


def test_detect_version_pins_what_the_server_reports(monkeypatch) -> None:
    """One call, and the version that gets pinned afterwards is the server's own
    release — not the pin that happened to make the probe succeed, and not the
    build suffix the CLI could not load a spec for."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        astro,
        "_run",
        _probe_run(captured, lambda pinned: json.dumps({"version": "2.10.3+astro.7"})),
    )
    detected = astro.detect_version("http://localhost:8080")
    assert detected == "2.10.3"  # not the "2.11.0" pin that answered
    assert len(captured) == 1  # one call, not one per major
    args = captured[0]
    assert args[-1] == "/version"
    assert args[args.index("--api-url") + 1] == "http://localhost:8080/api/v1"


def test_detect_version_falls_through_to_the_other_major(monkeypatch) -> None:
    """Airflow 3 answers a v1 path with a distinctive 404, so a wrong first
    guess costs one call rather than a wrong answer."""
    captured: list[list[str]] = []

    def answer(pinned: str) -> object:
        if api.major_version(pinned) == 2:
            return AstroError(_V1_REMOVED)
        return json.dumps({"version": "3.3.0"})

    monkeypatch.setattr(astro, "_run", _probe_run(captured, answer))
    assert astro.detect_version("http://airflow.example.invalid") == "3.3.0"
    assert len(captured) == 2
    assert captured[0][captured[0].index("--api-url") + 1].endswith("/api/v1")
    assert captured[1][captured[1].index("--api-url") + 1].endswith("/api/v2")


def test_detect_version_tries_the_dialect_the_url_names_first(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        astro,
        "_run",
        _probe_run(captured, lambda pinned: json.dumps({"version": "3.3.0"})),
    )
    assert astro.detect_version("http://airflow.example.invalid/api/v2") == "3.3.0"
    assert len(captured) == 1
    assert captured[0][captured[0].index("--api-url") + 1].endswith("/api/v2")


def test_detect_version_exhaustion_says_what_the_user_can_do(monkeypatch) -> None:
    """On an ingress that 401s every unauthenticated path — Astro's does — a
    failed probe cannot tell "wrong version" from "no access", so the message
    must name both rather than picking one."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        astro,
        "_run",
        _probe_run(captured, lambda pinned: AstroError("`astro …` failed: 401 Not authorized")),
    )
    try:
        astro.detect_version("http://airflow.example.invalid")
    except AstroError as exc:
        raw = str(exc)
        assert "airflow.example.invalid" in raw  # names the target
        assert "/api/v1" in raw and "/api/v2" in raw  # ...and what it tried
        assert "credentials" in raw  # the "no access" reading
        assert "is not Airflow" in raw  # ...and the "wrong version" one
        assert "--airflow-version" in raw  # ...and the way out
        # And it survives classification with the actionable part intact: the
        # summary bar shows the classified message, truncated from the right.
        error = astro.classify_astro_error(exc)
        assert error.kind in astro.KINDS
        assert "--airflow-version" in error.message
    else:  # pragma: no cover
        raise AssertionError("an exhausted probe must be reported")
    assert len(captured) == 2  # one per major, then it stops


def test_detect_version_treats_an_unreadable_body_as_a_miss(monkeypatch) -> None:
    captured: list[list[str]] = []

    def answer(pinned: str) -> str:
        return "{}" if api.major_version(pinned) == 2 else json.dumps({"version": "3.3.0"})

    monkeypatch.setattr(astro, "_run", _probe_run(captured, answer))
    assert astro.detect_version("http://airflow.example.invalid") == "3.3.0"
    assert len(captured) == 2


def test_fetch_log_addresses_a_mapped_task_instance(monkeypatch) -> None:
    """A mapped instance is only reachable with its map_index; without it Airflow
    looks for map_index -1 and answers 404 for every mapped task."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_routed_run(captured))
    run = _run_()
    astro.fetch_log(_deployment(), run, _task("sensor"), 1)
    astro.fetch_log(_deployment(), run, _task("fan", map_index=3), 1)
    unmapped, mapped = (args[-1] for args in captured)
    assert "map_index" not in unmapped
    assert "map_index=3" in mapped


def test_fetch_log_bounds_what_it_holds(monkeypatch) -> None:
    """The transport cannot stream, so the body arrives whole; the tool still
    must not *hold* an unbounded one, and must say when it stopped."""
    monkeypatch.setattr(astro, "MAX_LOG_CHARS", 100)
    big = "\n".join(f"line {n:04d}" for n in range(200))
    monkeypatch.setattr(
        astro, "_run", _fake_run([], [json.dumps({"content": big, "continuation_token": "t"})])
    )
    log = astro.fetch_log(_deployment(), _run_(), _task(), 1)
    assert log.truncated
    assert len(log.content) <= 100
    assert log.content.startswith("line 0000")
    assert log.content.endswith("line 0009")  # cut on a line boundary
    assert "line 0010" not in log.content
    # And the pane says so rather than implying the log ended there.
    out = _plain_renderable(ui.render_log(_task(), log))
    assert "too large to hold in full" in out


def test_fetch_log_leaves_a_normal_log_whole(monkeypatch) -> None:
    monkeypatch.setattr(astro, "_run", _fake_run([], [json.dumps(LOG_PAYLOAD)]))
    log = astro.fetch_log(_deployment(), _run_(), _task(), 1)
    assert not log.truncated
    assert "Found logs in s3" in log.content
    out = _plain_renderable(ui.render_log(_task(), log))
    assert "end of attempt 1" in out


def test_mutations_send_their_body_on_stdin_with_an_explicit_method(monkeypatch) -> None:
    """`--input -` + `-X` rather than -f/-F, which would rewrite the method."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(captured, ["{}", "{}", "{}", "{}"]))
    deployment = _deployment()

    astro.perform(deployment, Action(kind="pause", dag_id="sync_alpha"))
    astro.perform(deployment, Action(kind="unpause", dag_id="sync_alpha"))
    astro.perform(deployment, Action(kind="trigger", dag_id="sync_alpha"))
    astro.perform(
        deployment,
        Action(kind="mark", dag_id="d", run_id="r", task_ids=("t",), state="success"),
    )

    pause, unpause, trigger, mark = captured
    assert pause[pause.index("-X") + 1] == "PATCH"
    assert "update_mask=is_paused" in pause[-3]
    assert json.loads(pause[-1]) == {"is_paused": True}
    assert json.loads(unpause[-1]) == {"is_paused": False}
    assert trigger[trigger.index("-X") + 1] == "POST"
    assert "logical_date" not in json.loads(trigger[-1])
    assert json.loads(mark[-1])["dry_run"] is False
    assert json.loads(mark[-1])["new_state"] == "success"
    assert json.loads(mark[-1])["task_id"] == "t"  # not a `task_ids` list
    for args in captured:
        assert "--input" in args and args[args.index("--input") + 1] == "-"


def test_mutations_use_the_v2_shapes_against_an_airflow_3_target(monkeypatch) -> None:
    """The seam decides the *method and path*, not just the field names — so a
    3.x deployment marks a task with a PATCH of the instance and reaches its dry
    run through a separate endpoint."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(captured, ["{}"] * 3))
    deployment = _deployment(version=V2)

    astro.perform(deployment, Action(kind="trigger", dag_id="sync_alpha"))
    astro.perform(
        deployment,
        Action(kind="mark", dag_id="d", run_id="r", task_ids=("t",), state="success"),
    )
    astro.perform(
        deployment,
        Action(
            kind="mark",
            dag_id="d",
            run_id="r",
            task_ids=("t",),
            state="failed",
            dry_run=True,
            map_index=3,
        ),
    )

    trigger, mark, dry = captured
    # v2's trigger must carry an explicit null date rather than omitting it.
    assert json.loads(trigger[-1])["logical_date"] is None
    assert mark[mark.index("-X") + 1] == "PATCH"
    assert mark[-3] == "/dags/d/dagRuns/r/taskInstances/t"
    assert json.loads(mark[-1]) == {"new_state": "success"}
    assert dry[-3] == "/dags/d/dagRuns/r/taskInstances/t/3/dry_run"
    assert "dry_run" not in json.loads(dry[-1])


def test_mark_refuses_anything_but_exactly_one_task(monkeypatch) -> None:
    """One task at a time in both majors — v1's endpoint names one `task_id`,
    v2's addresses one instance. Sending several would silently mark one of
    them, so it is refused as typed data instead."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(captured, []))
    for version in (V1, V2):
        for task_ids in ((), ("a", "b")):
            try:
                astro.perform(
                    _deployment(version=version),
                    Action(kind="mark", dag_id="d", run_id="r", task_ids=task_ids, state="success"),
                )
            except AstroError as exc:
                # The seam raises a plain ValueError; the transport is what
                # types it, so the app's one error path still catches it.
                assert "exactly one task instance" in str(exc)
            else:  # pragma: no cover
                raise AssertionError(f"{version} {task_ids!r} must be refused")
    assert captured == []  # and nothing was sent


def test_perform_reports_a_dry_run_as_a_preview(monkeypatch) -> None:
    monkeypatch.setattr(
        astro, "_run", _fake_run([], [json.dumps(TASK_INSTANCES_PAYLOAD)] * 2)
    )
    dry = astro.perform(
        _deployment(),
        Action(kind="clear", dag_id="d", run_id="r", task_ids=("t",), dry_run=True),
    )
    assert "would affect 2 task instances" in dry
    real = astro.perform(
        _deployment(),
        Action(kind="clear", dag_id="d", run_id="r", task_ids=("t",), dry_run=False),
    )
    assert "affected 2 task instances" in real


def test_perform_reports_the_created_run_id(monkeypatch) -> None:
    monkeypatch.setattr(
        astro, "_run", _fake_run([], [json.dumps(DAG_RUN_ROWS[1])])
    )
    line = astro.perform(_deployment(), Action(kind="trigger", dag_id="cw_beta"))
    assert "created run manual__2026-07-24T21:18:00+00:00" in line


def test_clear_and_mark_refuse_to_run_without_a_dag_run(monkeypatch) -> None:
    """Task ids with no run id is not "clear this task" — Airflow reads it as
    "clear this task in every run there has ever been". Nothing is sent."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _fake_run(captured, []))
    for kind in ("clear", "mark"):
        try:
            astro.perform(
                _deployment(),
                Action(kind=kind, dag_id="d", task_ids=("t",), state="success"),
            )
        except AstroError as exc:
            assert "every run" in str(exc), kind
        else:  # pragma: no cover
            raise AssertionError(f"an unscoped {kind} must be refused")
    assert captured == []


def test_perform_rejects_an_unknown_action(monkeypatch) -> None:
    monkeypatch.setattr(astro, "_run", _fake_run([], []))
    try:
        astro.perform(_deployment(), Action(kind="delete_everything", dag_id="d"))
    except AstroError as exc:
        assert "delete_everything" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unknown action must not be sent")


def test_non_json_response_becomes_a_typed_error(monkeypatch) -> None:
    monkeypatch.setattr(astro, "_run", _fake_run([], ["<html>gateway timeout</html>"]))
    try:
        astro.airflow_get(_deployment(), "/dags")
    except AstroError as exc:
        assert "not JSON" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-JSON body must not escape as a JSONDecodeError")


def test_resolve_deployment_by_id_name_and_substring() -> None:
    deployments = api.parse_deployments(DEPLOYMENTS_PAYLOAD)
    assert astro.resolve_deployment(deployments, None).name == "Production"
    assert astro.resolve_deployment(deployments, "dep-dev-2").name == "Dev"
    assert astro.resolve_deployment(deployments, "production").name == "Production"
    assert astro.resolve_deployment(deployments, "Nex").name == "Next"


def test_resolve_deployment_says_what_was_available() -> None:
    deployments = api.parse_deployments(DEPLOYMENTS_PAYLOAD)
    try:
        astro.resolve_deployment(deployments, "nope")
    except AstroError as exc:
        assert "Production" in str(exc) and "Dev" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a typo must be reported, not guessed at")
    try:
        astro.resolve_deployment([], None)
    except AstroError as exc:
        assert "astro login" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an empty org must be reported")


def test_require_astro_reports_a_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(astro.shutil, "which", lambda _: None)
    try:
        astro.require_astro()
    except AstroError as exc:
        assert "Install it" in str(exc)  # the preflight message is actionable
        assert "astro login" in str(exc)
        # ...and it classifies as a missing binary, not as an auth problem,
        # even though it mentions `astro login`.
        assert astro.classify_astro_error(exc).kind == "missing_cli"
    else:  # pragma: no cover
        raise AssertionError("a missing binary must be reported")
    monkeypatch.setattr(astro.shutil, "which", lambda _: "/usr/local/bin/astro")
    astro.require_astro()  # a which() check only: no network, no exception


# --- rendering -------------------------------------------------------------


def test_render_summary_counts_and_names_the_deployment() -> None:
    snapshot = _snapshot(
        runs=(
            _run_("a", run_id="1", state="failed"),
            _run_("b", run_id="2", state="running"),
            _run_("c", run_id="3", state="queued"),
            _run_("d", run_id="4", state="success", end=NOW),
        ),
        dags=(_dag("a", paused=True),),
        import_errors=(ImportErrorEntry(filename="/dags/broken.py"),),
    )
    text = ui.render_summary(snapshot, None).plain
    assert "Customers / Production" in text
    assert "Airflow 2.11.0" in text
    assert "4 runs" in text
    assert "1 failed" in text and "1 running" in text and "1 queued" in text
    assert "1 import errors" in text
    # Both views are named, with the active one first.
    assert "DAG runs" in text and "DAGs" in text
    # Paused/stale counts belong to the DAGs view, where they are actionable.
    dag_text = ui.render_summary(snapshot, None, view="dags").plain
    assert "1 paused" in dag_text
    assert "0 stale" in dag_text


def test_render_summary_loading_and_error() -> None:
    assert "Contacting Astro" in ui.render_summary(None, None).plain
    assert "boom" in ui.render_summary(_snapshot(), "boom").plain


def test_render_detail_walks_the_drill_levels() -> None:
    snapshot = _snapshot()
    run = snapshot.runs[0]
    task = _task("sensor", state="failed")
    log = TaskLog(content="line one\nline two", try_number=1)

    at_runs = _plain_renderable(ui.render_detail(Drill(), snapshot, run, None, NOW))
    assert run.dag_id in at_runs
    assert "enter → task instances" in at_runs

    at_tasks = _plain_renderable(
        ui.render_detail(Drill(level="tasks", run=run, tasks=(task,)), snapshot, run, task, NOW)
    )
    assert "Task instance" in at_tasks
    assert "sensor" in at_tasks

    at_log = _plain_renderable(
        ui.render_detail(
            Drill(level="log", run=run, task=task, tasks=(task,), log=log),
            snapshot,
            run,
            task,
            NOW,
        )
    )
    assert "line one" in at_log
    assert "to change attempt" in at_log


def test_render_detail_shows_loading_and_error_states() -> None:
    snapshot = _snapshot()
    run = snapshot.runs[0]
    loading = _plain_renderable(
        ui.render_detail(Drill(level="tasks", run=run, loading=True), snapshot, run, None, NOW)
    )
    assert "Fetching task instances" in loading
    failed = _plain_renderable(
        ui.render_detail(Drill(level="tasks", run=run, error="404 nope"), snapshot, run, None, NOW)
    )
    assert "404 nope" in failed
    empty = _plain_renderable(
        ui.render_detail(Drill(level="tasks", run=run, tasks=()), snapshot, run, None, NOW)
    )
    assert "no task instances" in empty.lower()


def test_render_log_marks_the_selected_attempt() -> None:
    task = _task(try_number=3, max_tries=3)
    out = _plain_renderable(ui.render_log(task, TaskLog(content="body", try_number=2)))
    assert "body" in out
    assert "1" in out and "2" in out and "3" in out
    empty = _plain_renderable(ui.render_log(task, TaskLog(content="   ", try_number=1)))
    assert "empty log" in empty
    assert "Fetching log" in _plain_renderable(ui.render_log(task, None, loading=True))


def test_render_import_errors() -> None:
    assert "No DAG import errors" in _plain_renderable(ui.render_import_errors(()))
    out = _plain_renderable(
        ui.render_import_errors(tuple(api.parse_import_errors(IMPORT_ERRORS_PAYLOAD)))
    )
    assert "broken.py" in out
    assert "ImportError: boom" in out


def test_render_deployments_explains_why_one_is_unusable() -> None:
    deployments = tuple(api.parse_deployments(DEPLOYMENTS_PAYLOAD))
    out = _plain_renderable(ui.render_deployments(deployments, "dep-prod-1"))
    assert "Customers / Production" in out
    assert "hibernating" in out  # Dev
    # The 4.0.0 one is named rather than hidden...
    assert "unsupported" in out
    assert UNSUPPORTED_VERSION in out
    # ...and the 3.x one is now offered like any other target.
    next_line = next(line for line in out.splitlines() if "Next" in line)
    assert "3.0.2" in next_line
    assert "unsupported" not in next_line
    assert "No deployments" in _plain_renderable(ui.render_deployments((), ""))


def test_render_confirm_names_the_target_and_the_stakes() -> None:
    real = _plain_renderable(ui.render_confirm(Action(kind="pause", dag_id="sync_alpha")))
    assert "Pause DAG" in real
    assert "sync_alpha" in real
    assert "changes state in Airflow" in real
    dry = _plain_renderable(
        ui.render_confirm(Action(kind="clear", dag_id="d", run_id="r", task_ids=("t",), dry_run=True))
    )
    assert "Dry run" in dry
    assert "change nothing" in dry


def test_render_activity_log_empty_and_populated() -> None:
    assert "No activity yet" in _plain_renderable(ui.render_activity_log([]))
    entries = [
        LogEntry(time=NOW, level="info", message="Production — 3 runs"),
        LogEntry(time=NOW, level="warn", message="rate limit — backing off"),
        LogEntry(time=NOW, level="action", message="Pause DAG: sync_alpha — ok"),
    ]
    out = _plain_renderable(ui.render_activity_log(entries))
    assert "3 runs" in out
    assert "rate limit" in out
    assert "Pause DAG: sync_alpha" in out
    assert "Activity log" in out


def test_render_help_covers_the_drill_and_the_actions() -> None:
    out = _plain_renderable(ui.render_help())
    assert "Drill in" in out
    assert "Switch deployment" in out
    assert "asks first" in out  # the safety promise is on the help screen


def test_render_once_lists_runs_and_reports_the_call_cost() -> None:
    snapshot = _snapshot(import_errors=(ImportErrorEntry(filename="/dags/broken.py"),))
    console = Console(width=150)
    with console.capture() as cap:
        console.print(ui.render_once(snapshot, NOW))
    out = cap.get()
    assert "sync_beta" in out and "sync_alpha" in out
    assert "4 astro calls in 1.23s" in out
    assert "broken.py" in out


def test_list_row_and_task_row_match_their_headers() -> None:
    assert len(ui.list_row(_run_(), NOW)) == len(ui.list_columns())
    assert len(ui.task_row(TaskRow(task=_task(), position=1), NOW)) == len(ui.task_columns())


def test_attention_cell_dots() -> None:
    assert ui.attention_cell(_run_(state="failed")).style == "bold red"
    assert ui.attention_cell(_run_(state="running")).style == "bold cyan"
    assert ui.attention_cell(_run_(state="success", end=NOW)).plain.strip() == ""


def test_duration_cell_counts_up_while_running() -> None:
    live = ui._duration_cell(None, NOW - timedelta(seconds=90), NOW)
    assert live.plain == "1m 30s"
    assert live.style == "yellow"  # distinct from a finished total
    assert ui._duration_cell(45.0, None, NOW).plain == "45s"
    assert ui._duration_cell(None, None, NOW).plain == "—"


def _plain_renderable(renderable, width: int = 120) -> str:
    """A renderable's text with no ANSI codes.

    `color_system=None` matters: styling otherwise injects escape sequences
    *inside* a line, so `"ERROR boom" in output` fails on a highlighted match
    even though that is exactly what is on screen.
    """
    console = Console(width=width, color_system=None)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# --- cli -------------------------------------------------------------------


def test_cli_defaults() -> None:
    args = _parse_args([])
    assert args.deployment is None
    assert args.interval == 60
    assert args.limit == 50
    assert args.state is None
    assert args.api_url is None
    assert args.once is False


def test_cli_state_is_repeatable_and_unvalidated() -> None:
    args = _parse_args(["--state", "failed", "--state", "hypothetical_future_state"])
    assert _states(args) == ("failed", "hypothetical_future_state")
    assert _states(_parse_args([])) == ()


def test_cli_plain_airflow_target_probes_for_its_version(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        astro,
        "_run",
        _probe_run(captured, lambda pinned: json.dumps({"version": "3.3.0"})),
    )
    plain = _plain_deployment(_parse_args(["--api-url", "http://localhost:8080"]))
    assert plain.api_url == "http://localhost:8080"
    assert not plain.is_astro
    assert plain.airflow_version == "3.3.0"
    assert api.supports(plain.airflow_version)
    assert len(captured) == 1


def test_cli_plain_target_probes_once_per_session(monkeypatch) -> None:
    """ADR constraint: the probe is a startup cost. `poll` runs on a timer, so a
    per-poll probe would spend ~0.7s a minute re-learning a fixed number."""
    from tools.airflow_watch.cli import _PlainTarget

    captured: list[list[str]] = []
    monkeypatch.setattr(
        astro,
        "_run",
        _probe_run(captured, lambda pinned: json.dumps({"version": "2.11.0"})),
    )
    args = _parse_args(["--api-url", "http://localhost:8080"])
    target = _PlainTarget()
    first = target.get(args)
    assert target.get(args) is first
    assert target.get(args) is first
    assert len(captured) == 1


def test_cli_plain_target_retries_after_a_failed_probe(monkeypatch) -> None:
    """A target that was unreachable once must not wedge the session: nothing is
    remembered until something succeeded."""
    from tools.airflow_watch.cli import _PlainTarget

    captured: list[list[str]] = []
    # The first `get()` exhausts its pins; everything after it answers.
    remaining_failures = [len(api.probe_pins("http://localhost:8080"))]

    def answer(pinned: str) -> object:
        if remaining_failures[0] > 0:
            remaining_failures[0] -= 1
            return AstroError("`x` failed: 401 Not authorized")
        return json.dumps({"version": "2.11.0"})

    monkeypatch.setattr(astro, "_run", _probe_run(captured, answer))
    args = _parse_args(["--api-url", "http://localhost:8080"])
    target = _PlainTarget()
    try:
        target.get(args)
    except AstroError:
        pass
    else:  # pragma: no cover
        raise AssertionError("the first probe must fail")
    assert target.get(args).airflow_version == "2.11.0"


def test_cli_a_stated_airflow_version_never_probes(monkeypatch) -> None:
    """The probe is the fallback, not the rule: a user who says what the target
    is should not pay ~0.7s to have it re-established."""
    captured: list[list[str]] = []
    monkeypatch.setattr(astro, "_run", _probe_run(captured, lambda pinned: "{}"))
    stated = _plain_deployment(
        _parse_args(["--api-url", "http://x", "--airflow-version", "2.10.5"])
    )
    assert stated.airflow_version == "2.10.5"
    assert captured == []


# --- the chart --------------------------------------------------------------


def test_chart_group_is_total() -> None:
    assert ui.chart_group("failed") == "failed"
    assert ui.chart_group("upstream_failed") == "failed"
    assert ui.chart_group("running") == "running"
    assert ui.chart_group("restarting") == "running"
    assert ui.chart_group("queued") == "queued"
    assert ui.chart_group("deferred") == "queued"
    assert ui.chart_group("success") == "success"
    # Neutral and never-heard-of states land in "other", never raise.
    assert ui.chart_group("skipped") == "other"
    assert ui.chart_group("awaiting_input") == "other"
    assert ui.chart_group("") == "other"


def test_chart_counts_places_points_on_the_time_axis() -> None:
    points = (
        (NOW - timedelta(hours=2), "success"),  # the window start -> bucket 0
        (NOW - timedelta(hours=1), "failed"),  # halfway -> the middle
        (NOW, "running"),  # now -> the last bucket
        (None, "queued"),  # undated -> skipped
    )
    counts = ui.chart_counts(points, NOW, buckets=4)
    assert counts[0] == {"success": 1}
    assert counts[2] == {"failed": 1}
    assert counts[3] == {"running": 1}
    assert sum(sum(bucket.values()) for bucket in counts) == 3


def test_chart_counts_without_dated_points_is_empty() -> None:
    assert ui.chart_counts((), NOW, buckets=3) == [{}, {}, {}]
    assert ui.chart_counts(((None, "success"),), NOW, buckets=3) == [{}, {}, {}]


def test_chart_counts_single_instant_collapses_to_one_bucket() -> None:
    counts = ui.chart_counts(((NOW, "success"), (NOW, "failed")), NOW, buckets=5)
    assert counts[0] == {"success": 1, "failed": 1}
    assert sum(sum(bucket.values()) for bucket in counts) == 2


def test_stack_cells_keeps_a_failure_visible() -> None:
    """19 successes and 1 failure in a 5-cell bar: red still shows, and at the
    bottom — rounding it away would hide the one thing worth seeing."""
    cells = ui.stack_cells({"success": 19, "failed": 1}, height=5)
    assert len(cells) == 5
    assert cells[0] == "failed"
    assert cells.count("success") == 4


def test_stack_cells_is_proportional_and_bounded() -> None:
    assert ui.stack_cells({"success": 2, "failed": 2}, height=4) == (
        "failed",
        "failed",
        "success",
        "success",
    )
    assert ui.stack_cells({}, height=5) == ()
    assert ui.stack_cells({"success": 3}, height=0) == ()
    # More groups than cells: the attention-ordered prefix wins.
    squeezed = ui.stack_cells(
        {"failed": 1, "running": 1, "queued": 1, "success": 1}, height=2
    )
    assert squeezed == ("failed", "running")


def test_render_chart_shows_runs_at_the_top_level() -> None:
    runs = (
        _run_(state="failed", start=NOW - timedelta(hours=1)),
        _run_("sync_beta", state="success"),
    )
    text = _plain_renderable(ui.render_chart(Drill(), runs, NOW), width=80)
    assert "Runs over time" in text
    assert "█" in text
    assert "now" in text  # the time axis is labelled
    assert "failed" in text and "success" in text  # the legend names the groups


def test_render_chart_follows_the_drill_into_a_run() -> None:
    drill = Drill(
        level="tasks",
        run=_run_(),
        tasks=(_task(), _task("loader", state="failed")),
    )
    text = _plain_renderable(ui.render_chart(drill, (), NOW), width=80)
    assert "Tasks over time · sync_alpha" in text
    assert "█" in text


def test_render_chart_says_when_nothing_can_be_placed() -> None:
    assert "No dated runs" in _plain_renderable(ui.render_chart(Drill(), (), NOW))
    undated = DagRun(dag_id="sync_alpha", run_id="manual", state="queued")
    text = _plain_renderable(ui.render_chart(Drill(), (undated,), NOW))
    assert "No dated runs" in text
    never_started = Drill(
        level="tasks",
        run=_run_(),
        tasks=(TaskInstance(task_id="sensor", state="scheduled"),),
    )
    text = _plain_renderable(ui.render_chart(never_started, (), NOW))
    assert "No task instance has started yet" in text


# --- the in-flight chart ----------------------------------------------------


def test_in_flight_counts_counts_overlapping_spans() -> None:
    spans = (
        # Done inside the first quarter of the window.
        (NOW - timedelta(hours=4), NOW - timedelta(minutes=210)),
        # Started mid-window, still going: runs through to `now`.
        (NOW - timedelta(minutes=150), None),
        # Overlaps the still-running span across the last two buckets.
        (NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
        # Never started: was never in flight.
        (None, NOW),
    )
    assert ui.in_flight_counts(spans, NOW, buckets=4) == [1, 1, 2, 2]


def test_in_flight_counts_without_started_spans_is_empty() -> None:
    assert ui.in_flight_counts((), NOW, buckets=3) == [0, 0, 0]
    assert ui.in_flight_counts(((None, None),), NOW, buckets=3) == [0, 0, 0]


def test_in_flight_counts_single_instant_collapses_to_one_bucket() -> None:
    counts = ui.in_flight_counts(((NOW, NOW), (NOW, None)), NOW, buckets=5)
    assert counts[0] == 2
    assert sum(counts) == 2


def test_render_in_flight_chart_counts_runs_at_the_top_level() -> None:
    runs = (
        _run_(state="running", start=NOW - timedelta(hours=1)),  # no end: to now
        _run_("sync_beta", state="success", end=NOW - timedelta(minutes=5)),
    )
    text = _plain_renderable(ui.render_in_flight_chart(Drill(), runs, NOW), width=80)
    assert "Runs in flight" in text
    assert "█" in text
    assert "now" in text  # the time axis is labelled
    assert "2" in text  # the peak: the two runs overlapped


def test_render_in_flight_chart_follows_the_drill_into_a_run() -> None:
    drill = Drill(
        level="tasks",
        run=_run_(),
        tasks=(_task(), _task("loader", state="failed")),
    )
    text = _plain_renderable(ui.render_in_flight_chart(drill, (), NOW), width=80)
    assert "Tasks in flight · sync_alpha" in text
    assert "█" in text


def test_render_in_flight_chart_says_when_nothing_has_started() -> None:
    assert "No run has started" in _plain_renderable(
        ui.render_in_flight_chart(Drill(), (), NOW)
    )
    queued = DagRun(dag_id="sync_alpha", run_id="manual", state="queued")
    assert "No run has started" in _plain_renderable(
        ui.render_in_flight_chart(Drill(), (queued,), NOW)
    )
    never_started = Drill(
        level="tasks",
        run=_run_(),
        tasks=(TaskInstance(task_id="sensor", state="scheduled"),),
    )
    assert "No task instance has started" in _plain_renderable(
        ui.render_in_flight_chart(never_started, (), NOW)
    )


# --- the actions menu --------------------------------------------------------


def test_menu_entries_narrow_to_the_context() -> None:
    """The menu offers what the footer hint used to, per level and view."""
    top = [entry.action for entry in ui.menu_entries("runs", "runs")]
    assert "drill_in" in top and "trigger_run" in top
    assert "clear_tasks" not in top and "toggle_stale" not in top
    assert top[-1] == "quit"

    tasks = [entry.action for entry in ui.menu_entries("runs", "tasks")]
    assert "clear_tasks" in tasks and "mark_tasks" in tasks
    assert "switch_view" not in tasks

    log = [entry.action for entry in ui.menu_entries("runs", "log")]
    assert "prev_try" in log and "next_try" in log
    assert "clear_tasks" not in log

    dags = [entry.action for entry in ui.menu_entries("dags", "runs")]
    assert "toggle_stale" in dags


def test_menu_entries_label_their_toggles_by_state() -> None:
    def label(entries: tuple[ui.MenuEntry, ...], action: str) -> str:
        return next(entry.label for entry in entries if entry.action == action)

    assert "Show stale" in label(
        ui.menu_entries("dags", "runs", stale_shown=False), "toggle_stale"
    )
    assert "Hide stale" in label(
        ui.menu_entries("dags", "runs", stale_shown=True), "toggle_stale"
    )
    assert "Hide the charts" in label(
        ui.menu_entries("runs", "runs", chart_shown=True), "toggle_chart"
    )
    assert "Show the charts" in label(
        ui.menu_entries("runs", "runs", chart_shown=False), "toggle_chart"
    )


def test_menu_option_names_the_direct_key() -> None:
    """Every row teaches the shortcut it replaces."""
    for entry in ui.menu_entries("runs", "runs"):
        assert entry.key and entry.label and entry.action
        row = ui.menu_option(entry).plain
        assert entry.key in row and entry.label in row


def test_render_summary_marks_hidden_stale_dags() -> None:
    snapshot = _snapshot(dags=_dag_fleet())
    hidden = ui.render_summary(snapshot, None, view="dags", stale_hidden=True).plain
    assert "1 stale hidden · s shows" in hidden
    shown = ui.render_summary(snapshot, None, view="dags").plain
    assert "1 stale" in shown and "hidden" not in shown


# --- layout ---------------------------------------------------------------


def test_layout_from_dict_defaults_bad_values() -> None:
    assert layout.from_dict(None) == layout.Layout()
    assert layout.from_dict({}) == layout.Layout()
    assert layout.from_dict({"detail_mode": "sideways", "split": "wide"}) == layout.Layout()
    assert layout.from_dict({"deployment": 7}).deployment == ""
    assert layout.from_dict({"chart": "yes"}).chart is True
    assert layout.from_dict({"chart": False}).chart is False
    # Booleans are ints in Python; they must not sneak in as a split.
    assert layout.from_dict({"split": True}).split == layout.SPLIT_DEFAULT
    assert layout.from_dict({"split": 5}).split == layout.SPLIT_MIN
    assert layout.from_dict({"split": 95}).split == layout.SPLIT_MAX


def test_layout_save_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "sub" / "layout.json"  # parent dir is created on save
    saved = layout.Layout(detail_mode="below", split=35, deployment="dep-prod-1", chart=False)
    layout.save(saved, path)
    assert layout.load(path) == saved


def test_layout_load_missing_or_corrupt_file(tmp_path) -> None:
    assert layout.load(tmp_path / "nope.json") == layout.Layout()
    corrupt = tmp_path / "layout.json"
    corrupt.write_text("{not json")
    assert layout.load(corrupt) == layout.Layout()


def test_state_path_is_this_tools_own(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert layout.state_path() == tmp_path / "airflow-watch" / "layout.json"


# --- the app --------------------------------------------------------------


def _plain(widget: Static) -> str:
    """The plain text a Static widget was last updated with, ANSI stripped."""
    return _plain_renderable(widget.content, width=140)


def _app(
    polls: list[tuple[Snapshot | None, PollError | None]] | None = None,
    *,
    tasks: list[TaskInstance] | None = None,
    task_error: PollError | None = None,
    log: TaskLog | None = None,
    log_error: PollError | None = None,
    performed: list[Action] | None = None,
    perform_error: PollError | None = None,
    layout_path=None,
    interval: int = 60,
    requested: list[PollRequest] | None = None,
    graph: dict[str, tuple[str, ...]] | None = None,
) -> AirflowWatchApp:
    """An app wired to fakes: one poll queue plus the three drill-down seams."""
    queue = list(polls) if polls else [(_snapshot(), None)]
    resolved_tasks = tasks if tasks is not None else [_task("sensor", state="failed"), _task("loader")]
    fired = performed if performed is not None else []
    asked = requested if requested is not None else []
    # Default structure: sensor feeds loader, so the pane shows the tree.
    edges = graph if graph is not None else {"sensor": ("loader",), "loader": ()}

    def poll(request):
        asked.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def fetch_tasks(_deployment, _run):
        if task_error is not None:
            return None, task_error
        ordered = order_task_instances(list(resolved_tasks), edges)
        return (
            astro.RunTasks(
                tasks=tuple(resolved_tasks),
                rows=tuple(ordered),
                total=len(resolved_tasks),
                truncated=False,
                graph=edges,
                calls=2,
            ),
            None,
        )

    def fetch_log(_deployment, _run, task, try_number):
        if log_error is not None:
            return None, log_error
        return (
            log or TaskLog(content=f"log of {task.task_id} attempt {try_number}", try_number=try_number),
            None,
        )

    def perform(_deployment, action):
        if perform_error is not None:
            return None, perform_error
        fired.append(action)
        return f"{action.summary} — ok", None

    return AirflowWatchApp(
        poll=poll,
        interval=interval,
        fetch_tasks=fetch_tasks,
        fetch_log=fetch_log,
        perform=perform,
        layout_path=layout_path,
    )


async def test_app_lists_runs_and_shows_the_selected_one() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        assert table.row_count == 2
        # The failed run sorts — and is selected — first.
        assert app._selected_key is not None and "sync_beta" in app._selected_key
        assert "sync_beta" in _plain(app.query_one("#detail", Static))
        assert "Customers / Production" in _plain(app.query_one("#summary", Static))

        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key is not None and "sync_alpha" in app._selected_key
        assert "sync_alpha" in _plain(app.query_one("#detail", Static))


async def test_app_drills_run_to_tasks_to_log() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)

        # run -> task instances: the list window swaps to the task columns.
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "tasks"
        assert len(table.columns) == len(ui.task_columns())
        assert table.row_count == 2
        detail = _plain(app.query_one("#detail", Static))
        assert "Task instance" in detail
        assert "sensor" in detail  # the failed task sorted first

        # task instance -> log
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "log"
        assert app._drill.log is not None
        assert "log of sensor attempt 1" in _plain(app.query_one("#detail", Static))

        # escape backs out one level at a time, restoring each list.
        await pilot.press("escape")
        await pilot.pause()
        assert app._drill.level == "tasks"
        await pilot.press("escape")
        await pilot.pause()
        assert app._drill.level == "runs"
        assert len(table.columns) == len(ui.list_columns())
        assert table.row_count == 2


async def test_app_steps_through_log_attempts() -> None:
    app = _app(tasks=[_task("sensor", state="failed", try_number=3, max_tries=3)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.try_number == 3  # opens on the latest attempt

        await pilot.press("less_than_sign")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.try_number == 2
        assert "attempt 2" in _plain(app.query_one("#detail", Static))

        # Walking off the start is a no-op, not an error.
        await pilot.press("less_than_sign")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.try_number == 1
        await pilot.press("less_than_sign")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.try_number == 1

        await pilot.press("greater_than_sign")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.try_number == 2


async def test_app_moving_between_tasks_follows_with_the_log() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "log of sensor" in _plain(app.query_one("#detail", Static))

        await pilot.press("down")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "log"
        assert "log of loader" in _plain(app.query_one("#detail", Static))


async def test_app_drill_error_keeps_the_run_list() -> None:
    app = _app(task_error=PollError(message="Not permitted", kind="forbidden"))
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Not permitted" in _plain(app.query_one("#detail", Static))
        assert app.activity_log[-1].level == "error"
        # Backing out returns a usable list, not a broken app.
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2


async def test_app_error_keeps_last_good_data() -> None:
    app = _app([(_snapshot(), None), (None, PollError(message="Airflow exploded"))])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2  # stale list stays usable
        assert "Airflow exploded" in _plain(app.query_one("#summary", Static))


async def test_app_backs_off_then_recovers_on_rate_limit() -> None:
    app = _app(
        [
            (_snapshot(), None),
            (None, PollError(message="rate limited", kind="rate_limited")),
            (None, PollError(message="rate limited", kind="rate_limited")),
            (_snapshot(), None),
        ]
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60  # first hit: interval * 2**0

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 120  # second consecutive hit doubles

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60  # a good poll resets the backoff


async def test_app_backoff_honours_retry_after_and_caps() -> None:
    app = _app([(None, PollError("slow down", kind="rate_limited", retry_after=99999))])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 900


async def test_app_logs_every_poll_with_its_call_cost() -> None:
    app = _app(
        [
            (_snapshot(), None),
            (None, PollError(message="rate limited", kind="rate_limited")),
            (None, PollError(message="Not permitted", kind="forbidden")),
        ]
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        first = app.activity_log[0]
        assert first.level == "info"
        assert "Customers / Production" in first.message
        assert "2 runs" in first.message
        assert "4 calls in 1.23s" in first.message  # how a slow poll is diagnosed

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.activity_log[-1].level == "warn"
        assert "Next try in" in app.activity_log[-1].message

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.activity_log[-1].level == "error"


async def test_app_hibernating_deployment_shows_its_own_state() -> None:
    app = _app(
        [
            (
                None,
                PollError(
                    message="Deployment is hibernating — its Airflow API is not running.",
                    kind="hibernating",
                ),
            )
        ]
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._error is not None and app._error.kind == "hibernating"
        assert not app._error.rate_limited  # not treated as a rate limit
        assert "hibernating" in _plain(app.query_one("#summary", Static))
        assert "hibernating" in _plain(app.query_one("#detail", Static))
        assert app.query_one(DataTable).row_count == 0


async def test_app_unsupported_version_is_refused_by_name() -> None:
    refusal = astro.classify_astro_error(
        api.UnsupportedAirflowVersion(UNSUPPORTED_VERSION)
    )
    app = _app([(None, refusal)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._error is not None and app._error.kind == "unsupported_version"
        summary = _plain(app.query_one("#summary", Static))
        assert UNSUPPORTED_VERSION in summary
        assert "not supported" in summary


async def test_app_empty_run_list() -> None:
    app = _app([(_snapshot(runs=()), None)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0
        assert "No DAG runs" in _plain(app.query_one("#detail", Static))
        # Drilling into nothing is a no-op.
        await pilot.press("enter")
        await pilot.pause()
        assert app._drill.level == "runs"


# --- the confirmation gate ------------------------------------------------


async def test_mutating_action_does_nothing_until_confirmed() -> None:
    fired: list[Action] = []
    app = _app(performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("p")  # pause the selected run's DAG
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert fired == []  # the keystroke alone changed nothing

        confirm = _plain(app.screen.query_one("#confirm", Static))
        assert "Pause DAG" in confirm
        assert "sync_beta" in confirm  # the modal names its target

        await pilot.press("n")  # decline
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert fired == []
        assert "Cancelled" in app.activity_log[-1].message


async def test_confirmed_action_fires_exactly_once_and_is_logged() -> None:
    fired: list[Action] = []
    app = _app(performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("t")  # trigger a run
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert len(fired) == 1
        assert fired[0].kind == "trigger"
        assert fired[0].dag_id == "sync_beta"
        assert not fired[0].dry_run  # trigger has no dry-run mode
        action_lines = [e for e in app.activity_log if e.level == "action"]
        assert len(action_lines) == 1
        assert "Trigger a new DAG run" in action_lines[0].message


async def test_pause_flips_to_unpause_for_an_already_paused_dag() -> None:
    fired: list[Action] = []
    snapshot = _snapshot(
        runs=(_run_("sync_beta", run_id="r", state="failed"),),
        dags=(_dag("sync_beta", paused=True),),
    )
    app = _app([(snapshot, None)], performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert "Unpause DAG" in _plain(app.screen.query_one("#confirm", Static))
        await pilot.press("enter")  # enter confirms, same as y
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [a.kind for a in fired] == ["unpause"]


async def test_clear_previews_as_a_dry_run_then_offers_the_real_call() -> None:
    """v1 defaults dry_run to true, so the preview is free — and confirming it
    is what surfaces the real call, never a single keystroke."""
    fired: list[Action] = []
    app = _app(performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # drill to task instances
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()
        preview = _plain(app.screen.query_one("#confirm", Static))
        assert "Dry run" in preview
        assert "change nothing" in preview

        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(fired) == 1
        assert fired[0].dry_run is True
        # ...and the real call is now sitting behind its own confirmation.
        assert isinstance(app.screen, ConfirmScreen)
        real = _plain(app.screen.query_one("#confirm", Static))
        assert "changes state in Airflow" in real

        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(fired) == 2
        assert fired[1].dry_run is False
        assert fired[1].kind == "clear"
        assert fired[1].task_ids == ("sensor",)


async def test_mark_defaults_to_success_for_a_failed_task() -> None:
    fired: list[Action] = []
    app = _app(performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        assert "success" in _plain(app.screen.query_one("#confirm", Static))
        await pilot.press("escape")  # escape cancels too
        await pilot.pause()
        assert fired == []


async def test_mark_carries_the_selected_instances_map_index() -> None:
    """A mapped task is several instances sharing one task id. v2 addresses the
    one being marked by `map_index` in the path, so the coordinate has to travel
    with the action — and has to survive the dry-run → real-call handoff."""
    fired: list[Action] = []
    app = _app(
        tasks=[_task("fan", state="failed", map_index=3)],
        performed=fired,
        graph={},
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # drill to task instances
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("m")
        await pilot.pause()
        await pilot.press("y")  # the dry run
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("y")  # ...then the real call it offers
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert [a.map_index for a in fired] == [3, 3]
    assert [a.dry_run for a in fired] == [True, False]
    assert all(a.task_ids == ("fan",) for a in fired)


async def test_action_failure_is_logged_and_does_not_retry() -> None:
    app = _app(perform_error=PollError(message="Not permitted", kind="forbidden"))
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        last = app.activity_log[-1]
        assert last.level == "error"
        assert "Not permitted" in last.message
        assert not isinstance(app.screen, ConfirmScreen)


async def test_actions_are_inert_without_a_selection() -> None:
    fired: list[Action] = []
    app = _app([(_snapshot(runs=()), None)], performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        for key in ("p", "t", "c", "m"):
            await pilot.press(key)
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen), key
        assert fired == []


# --- overlays and layout --------------------------------------------------


async def test_deployment_switcher_repolls_the_new_target() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.deployment_key == "dep-prod-1"

        app.action_switch_deployment()
        await pilot.pause()
        assert isinstance(app.screen, DeploymentScreen)
        assert "Staging" in _plain(app.screen.query_one("#deployments", Static))

        await pilot.press("2")  # pick the second deployment
        await pilot.pause()
        assert not isinstance(app.screen, DeploymentScreen)
        assert app._wanted_deployment == "dep-stg-2"
        assert any("Switched to" in e.message for e in app.activity_log)


async def test_deployment_switcher_cancels_cleanly() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.action_switch_deployment()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DeploymentScreen)
        assert app.deployment_key == "dep-prod-1"
        # An out-of-range number does nothing.
        app.action_switch_deployment()
        await pilot.pause()
        await pilot.press("9")
        await pilot.pause()
        assert isinstance(app.screen, DeploymentScreen)


async def test_import_error_overlay() -> None:
    app = _app(
        [
            (
                _snapshot(
                    import_errors=(
                        ImportErrorEntry(filename="/dags/broken.py", stacktrace="ImportError: boom"),
                    )
                ),
                None,
            )
        ]
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "1 import errors" in _plain(app.query_one("#summary", Static))

        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, ImportErrorScreen)
        content = _plain(app.screen.query_one("#errors-content", Static))
        assert "broken.py" in content
        assert "ImportError: boom" in content

        await pilot.press("e")
        await pilot.pause()
        assert not isinstance(app.screen, ImportErrorScreen)


async def test_activity_log_overlay_is_live() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)
        assert "2 runs" in _plain(app.screen.query_one("#log-content", Static))

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)  # still open, and updated
        assert len(app.activity_log) == 2

        await pilot.press("l")
        await pilot.pause()
        assert not isinstance(app.screen, LogScreen)


async def test_activity_log_is_capped() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        for index in range(320):
            app._append_log("info", f"line {index}")
        assert len(app.activity_log) == 200
        assert app.activity_log[-1].message == "line 319"


async def test_help_overlay_opens_and_closes() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert "Drill in" in _plain(app.screen.query_one("#help", Static))
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        assert app.is_running


async def test_cycle_detail_moves_then_hides_pane() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        body = app.query_one("#body")
        detail_pane = app.query_one("#detail-pane")
        table = app.query_one(DataTable)

        await pilot.press("d")
        await pilot.pause()
        assert body.has_class("detail-below")
        assert table.size.height < body.size.height

        await pilot.press("d")
        await pilot.pause()
        assert body.has_class("detail-hidden")
        assert not detail_pane.display
        assert table.size.width == app.size.width

        await pilot.press("d")
        await pilot.pause()
        assert not body.has_class("detail-below")
        assert not body.has_class("detail-hidden")
        assert table.size.width < app.size.width


async def test_detail_below_puts_the_charts_beside_the_run_pane() -> None:
    """In "right" mode the charts sit under the detail; "below" mode is
    height-starved, so there they move next to it instead."""
    app = _app()
    async with app.run_test(size=(150, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        scroll = app.query_one("#detail-scroll")
        charts = app.query_one("#charts")
        assert charts.region.y > scroll.region.y  # right mode: stacked

        await pilot.press("d")  # -> below
        await pilot.pause()
        assert charts.region.y == scroll.region.y  # side by side...
        assert charts.region.x > scroll.region.x  # ...detail left, charts right


async def test_chart_panes_graph_runs_and_follow_the_drill() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        chart = app.query_one("#chart", Static)
        flight = app.query_one("#in-flight-chart", Static)
        assert app.query_one("#charts").display
        assert "Runs over time" in _plain(chart)
        assert "Runs in flight" in _plain(flight)

        await pilot.press("enter")  # drill into the failed run (sync_beta)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Tasks over time · sync_beta" in _plain(chart)
        assert "Tasks in flight · sync_beta" in _plain(flight)

        await pilot.press("escape")
        await pilot.pause()
        assert "Runs over time" in _plain(chart)
        assert "Runs in flight" in _plain(flight)


async def test_charts_toggle_together_and_persist(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = _app(layout_path=path)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        charts = app.query_one("#charts")
        assert charts.display

        await pilot.press("g")
        await pilot.pause()
        assert not charts.display
        assert layout.load(path).chart is False

        await pilot.press("g")
        await pilot.pause()
        assert charts.display
        assert layout.load(path).chart is True
        # Both panes repainted on re-show.
        assert "Runs over time" in _plain(app.query_one("#chart", Static))
        assert "Runs in flight" in _plain(app.query_one("#in-flight-chart", Static))


async def test_resize_moves_divider_and_persists(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = _app(layout_path=path)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        width_before = table.size.width

        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app._split == layout.SPLIT_DEFAULT + layout.SPLIT_STEP
        assert table.size.width > width_before
        assert layout.load(path).split == app._split
        assert layout.load(path).deployment == "dep-prod-1"

        for _ in range(30):
            await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app._split == layout.SPLIT_MIN


async def test_resize_is_a_noop_when_detail_hidden(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = _app(layout_path=path)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d", "d")  # right -> below -> hidden
        await pilot.pause()
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app._split == layout.SPLIT_DEFAULT
        assert layout.load(path).detail_mode == "hidden"


async def test_saved_layout_and_deployment_are_restored(tmp_path) -> None:
    path = tmp_path / "layout.json"
    layout.save(
        layout.Layout(detail_mode="below", split=30, deployment="dep-stg-2", chart=False),
        path,
    )
    asked: list[PollRequest] = []
    app = _app(layout_path=path, requested=asked)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#body").has_class("detail-below")
        assert app._split == 30
        assert not app.query_one("#charts").display
        # The remembered deployment is what the first poll was asked to read.
        assert asked[0].deployment is not None and asked[0].deployment.key == "dep-stg-2"


async def test_first_poll_asks_for_nothing_when_there_is_no_preference() -> None:
    asked: list[PollRequest] = []
    app = _app(requested=asked)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert asked[0].deployment is None  # the caller's closure picks the default
        # Later polls carry the deployment the snapshot came back with.
        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert asked[1].deployment is not None and asked[1].deployment.key == "dep-prod-1"


async def test_switching_deployment_asks_the_poll_for_the_new_one() -> None:
    asked: list[PollRequest] = []
    app = _app(requested=asked)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.action_switch_deployment()
        await pilot.pause()
        await pilot.press("2")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert asked[-1].deployment is not None and asked[-1].deployment.key == "dep-stg-2"


async def test_switching_deployment_while_a_poll_is_in_flight_is_not_lost() -> None:
    """A poll takes seconds, so switching during one is easy. The in-flight
    result describes the deployment the user just left: adopting it would make it
    the target of the next poll too, silently undoing the switch."""
    prod = _deployment()
    staging = _deployment("Staging", id_="dep-stg-2")
    both = (prod, staging)
    asked: list[PollRequest] = []
    holding = threading.Event()
    released = threading.Event()

    def poll(request: PollRequest):
        asked.append(request)
        wanted = request.deployment
        chosen = staging if wanted is not None and wanted.key == staging.key else prod
        if len(asked) == 2:  # hold the second poll open across the switch
            holding.set()
            released.wait(5)
        return _snapshot(deployment=chosen, deployments=both), None

    app = AirflowWatchApp(poll=poll, interval=60)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.deployment_key == "dep-prod-1"

        app.action_poll_now()  # poll 2: blocks inside the fake
        for _ in range(200):  # yield to the loop so the worker thread can start
            if holding.is_set():
                break
            await asyncio.sleep(0.01)
        assert holding.is_set()
        app.action_switch_deployment()
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert app._wanted_deployment == "dep-stg-2"

        released.set()
        # Poll 2 lands and is discarded; the 1s heartbeat starts its replacement.
        for _ in range(400):
            await asyncio.sleep(0.01)
            if app.deployment is not None:
                break
        assert app.deployment_key == "dep-stg-2"
        assert app.deployment is not None and app.deployment.name == "Staging"
        assert any("Discarded" in entry.message for entry in app.activity_log)
        # The redone poll asked for the deployment the user actually chose.
        assert asked[-1].deployment is not None
        assert asked[-1].deployment.key == "dep-stg-2"


async def test_unrecoverable_failure_stops_retrying_on_the_minute() -> None:
    """A missing binary or a refused version cannot be fixed by a timer, so the
    poll stretches to the ceiling instead of respawning a doomed process."""
    for error in (
        astro.classify_astro_error(AstroError("`astro` is not on PATH")),
        astro.classify_astro_error(api.UnsupportedAirflowVersion(UNSUPPORTED_VERSION)),
    ):
        app = _app([(None, error)])
        async with app.run_test(size=(150, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._error is not None and not app._error.recoverable
            assert app._current_delay == 900

    # A recoverable one keeps the normal cadence: `astro login` may fix it.
    auth = astro.classify_astro_error(AstroError("`x` failed: no context set"))
    app = _app([(None, auth)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60


async def test_app_without_layout_path_never_persists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d", "right_square_bracket")
        await pilot.pause()
    assert not (tmp_path / "airflow-watch").exists()


# --- the DAG view -----------------------------------------------------------


def _dag_fleet() -> tuple[Dag, ...]:
    return (
        _dag("sync_alpha", paused=True),
        Dag(dag_id="sync_beta", owners=("cs",), tags=("databricks",)),
        Dag(dag_id="gone_stale", is_active=False),
        Dag(dag_id="broken_dag", has_import_errors=True),
    )


async def test_view_switch_shows_dags_with_stale_hidden_but_counted() -> None:
    """Paused DAGs are rows with labels, not omissions. Stale DAGs are hidden
    by default — but *counted* hidden, never silently absent, and `s` shows
    them."""
    snapshot = _snapshot(dags=_dag_fleet())
    app = _app([(snapshot, None)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 2  # the runs

        await pilot.press("v")
        await pilot.pause()
        assert app._view == "dags"
        assert table.row_count == 3  # gone_stale hidden, paused still a row
        assert len(table.columns) == len(ui.dag_columns())
        assert "gone_stale" not in [dag.dag_id for dag in app.visible_dags()]
        summary = _plain(app.query_one("#summary", Static))
        assert "3 dags" in summary
        assert "1 paused" in summary
        assert "1 stale hidden" in summary  # hidden rows stay on the books

        # `s` shows the stale rows; pressing it again hides them.
        await pilot.press("s")
        await pilot.pause()
        assert table.row_count == 4
        assert "gone_stale" in [dag.dag_id for dag in app.visible_dags()]
        summary = _plain(app.query_one("#summary", Static))
        assert "4 dags" in summary
        assert "1 stale" in summary and "hidden" not in summary
        await pilot.press("s")
        await pilot.pause()
        assert table.row_count == 3

        # Switching back restores the runs list, where `s` is a no-op.
        await pilot.press("v")
        await pilot.pause()
        assert app._view == "runs"
        assert table.row_count == 2
        await pilot.press("s")
        await pilot.pause()
        assert table.row_count == 2
        assert not app._show_stale


async def test_dag_view_detail_labels_the_dags_state() -> None:
    app = _app([(_snapshot(dags=_dag_fleet()), None)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        # Rows are in snapshot order; the first is the paused one.
        detail = _plain(app.query_one("#detail", Static))
        assert "sync_alpha" in detail
        assert "paused" in detail

        await pilot.press("s")  # show the stale rows to reach one
        await pilot.press("down", "down")
        await pilot.pause()
        stale = _plain(app.query_one("#detail", Static))
        assert "gone_stale" in stale
        assert "stale" in stale


async def test_summary_states_the_true_total_when_a_list_is_partial() -> None:
    """A truncated list must say so — v1 caps a page at 100 whatever you ask
    for, so a bare row count is not evidence of completeness."""
    snapshot = dataclasses.replace(
        _snapshot(dags=_dag_fleet()), runs_total=533618, dags_total=1200, dags_truncated=True
    )
    app = _app([(snapshot, None)])
    async with app.run_test(size=(160, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        summary = _plain(app.query_one("#summary", Static))
        assert "2 of 533,618 runs" in summary
        assert "DAG list truncated" in summary

        await pilot.press("v")
        await pilot.pause()
        # 3, not 4: the stale DAG is hidden — and separately counted as such.
        assert "3 of 1,200 dags" in _plain(app.query_one("#summary", Static))


async def test_pause_in_the_dag_view_targets_the_selected_dag() -> None:
    fired: list[Action] = []
    app = _app([(_snapshot(dags=_dag_fleet()), None)], performed=fired)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("p")  # sync_alpha is paused -> offers unpause
        await pilot.pause()
        confirm = _plain(app.screen.query_one("#confirm", Static))
        assert "Unpause DAG" in confirm
        assert "sync_alpha" in confirm
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [(a.kind, a.dag_id) for a in fired] == [("unpause", "sync_alpha")]


async def test_enter_on_a_dag_jumps_to_its_runs() -> None:
    app = _app([(_snapshot(dags=_dag_fleet()), None)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("down")  # sync_beta
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app._view == "runs"
        assert app._queries["runs"] == "sync_beta"
        assert app.query_one(DataTable).row_count == 1


# --- the `/` filter ---------------------------------------------------------


async def test_filter_narrows_the_runs_list_client_side() -> None:
    asked: list[PollRequest] = []
    app = _app(requested=asked)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 2
        polls_before = len(asked)

        await pilot.press("slash")
        await pilot.pause()
        assert app._filtering == "runs"
        assert "/" in _plain(app.query_one("#status", Static))

        for key in "beta":
            await pilot.press(key)
        await pilot.pause()
        assert app._queries["runs"] == "beta"
        assert table.row_count == 1  # narrowed
        assert "1 of" in _plain(app.query_one("#summary", Static)) or "1 runs" in _plain(
            app.query_one("#summary", Static)
        )
        # Filtering is client-side: not one extra poll.
        assert len(asked) == polls_before

        # Enter keeps the filter and closes the prompt; the footer still says so.
        await pilot.press("enter")
        await pilot.pause()
        assert app._filtering is None
        assert app._queries["runs"] == "beta"
        assert "/beta" in _plain(app.query_one("#status", Static))

        # Escape clears it and the full list returns.
        await pilot.press("escape")
        await pilot.pause()
        assert app._queries["runs"] == ""
        assert table.row_count == 2


async def test_filter_backspace_and_space() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("slash")
        for key in "beta":
            await pilot.press(key)
        await pilot.press("space")
        await pilot.press("f")
        await pilot.pause()
        assert app._queries["runs"] == "beta f"
        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()
        assert app._queries["runs"] == "bet"
        # A query matching nothing empties the list rather than erroring.
        for key in "zzz":
            await pilot.press(key)
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0
        assert "No DAG runs match" in _plain(app.query_one("#detail", Static))


async def test_filter_on_the_dag_list() -> None:
    app = _app([(_snapshot(dags=_dag_fleet()), None)])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("slash")
        for key in "sync":
            await pilot.press(key)
        await pilot.pause()
        assert app._filtering == "dags"
        assert app.query_one(DataTable).row_count == 2
        # Each list keeps its own query, so the runs filter is untouched.
        assert app._queries["runs"] == ""


async def test_dag_filter_goes_server_side_only_when_the_list_is_truncated() -> None:
    """Client-side is instant and free; a truncated list is the one case where it
    would be searching an incomplete set, so the pattern is pushed to Airflow."""
    asked: list[PollRequest] = []
    truncated = dataclasses.replace(
        _snapshot(dags=_dag_fleet()), dags_total=1200, dags_truncated=True
    )
    app = _app([(truncated, None)], requested=asked)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("slash")
        for key in "sync":
            await pilot.press(key)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert asked[-1].dag_pattern == "sync"

    # With a complete list, the same keystrokes never reach the server.
    asked_full: list[PollRequest] = []
    app = _app([(_snapshot(dags=_dag_fleet()), None)], requested=asked_full)
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("slash")
        for key in "sync":
            await pilot.press(key)
        await pilot.pause()
        assert all(request.dag_pattern == "" for request in asked_full)


async def test_filter_on_the_task_list() -> None:
    app = _app(tasks=[_task("sensor", state="failed"), _task("loader"), _task("notify")])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 3

        await pilot.press("slash")
        for key in "notify":
            await pilot.press(key)
        await pilot.pause()
        assert app._filtering == "tasks"
        assert table.row_count == 1
        # Escape clears the filter; a second escape backs out a level.
        await pilot.press("escape")
        await pilot.pause()
        assert table.row_count == 3
        assert app._drill.level == "tasks"
        await pilot.press("escape")
        await pilot.pause()
        assert app._drill.level == "runs"


async def test_filter_inside_a_log() -> None:
    app = _app(
        tasks=[_task("sensor", state="failed")],
        log=TaskLog(content="starting up\nERROR boom\nshutting down\n", try_number=1),
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "starting up" in _plain(app.query_one("#detail", Static))

        await pilot.press("slash")
        for key in "error":
            await pilot.press(key)
        await pilot.pause()
        assert app._filtering == "log"
        detail = _plain(app.query_one("#detail", Static))
        assert "ERROR boom" in detail
        assert "starting up" not in detail  # non-matching lines filtered out
        assert "1 of 3 lines match" in detail

        await pilot.press("escape")
        await pilot.pause()
        assert app._queries["log"] == ""
        assert "starting up" in _plain(app.query_one("#detail", Static))


async def test_view_switch_is_inert_inside_a_drill_down() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert app._view == "runs"  # the task list belongs to the run, not a view
        assert app._drill.level == "tasks"


# --- the dependency-ordered task pane ---------------------------------------


async def test_task_pane_lists_tasks_in_dependency_order() -> None:
    app = _app(
        tasks=[_task("notify"), _task("loader"), _task("sensor", state="failed")],
        graph={"sensor": ("loader",), "loader": ("notify",), "notify": ()},
    )
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = app._drill.rows
        assert [row.task.task_id for row in rows] == ["sensor", "loader", "notify"]
        assert [row.position for row in rows] == [1, 2, 3]
        assert [row.depth for row in rows] == [0, 1, 2]
        # The cursor starts on the topologically first task, not the failed one.
        assert app._task_key is not None and app._task_key.startswith("sensor")


async def test_task_pane_warns_when_the_task_list_is_truncated() -> None:
    def fetch_tasks(_deployment, _run):
        tasks = [_task("sensor", state="failed")]
        return (
            astro.RunTasks(
                tasks=tuple(tasks),
                rows=tuple(order_task_instances(tasks, {})),
                total=1500,
                truncated=True,
                graph={},
                calls=13,
            ),
            None,
        )

    app = _app()
    app._fetch_tasks = fetch_tasks
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        warning = [e for e in app.activity_log if e.level == "warn"]
        assert warning and "1 of 1500 task instances" in warning[-1].message


async def test_the_heartbeat_does_not_re_render_the_log_pane(monkeypatch) -> None:
    """A log pane has no clock in it, and laying out a large one measured ~330ms.
    Redrawing it every second would spend a third of a core on a body that cannot
    have changed — the footer countdown still updates."""
    app = _app(tasks=[_task("sensor", state="failed")])
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # into the log
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "log"

        renders: list[int] = []
        original = ui.render_log
        monkeypatch.setattr(
            ui, "render_log", lambda *a, **k: (renders.append(1), original(*a, **k))[1]
        )
        before = _plain(app.query_one("#status", Static))
        app._tick()
        app._tick()
        await pilot.pause()
        assert renders == []  # the pane was left alone
        assert _plain(app.query_one("#status", Static)) != before  # the footer moved

        # ...but any real change to the pane still redraws it.
        await pilot.press("slash", "e")
        await pilot.pause()
        assert renders


async def test_footer_points_to_the_menu_and_keeps_the_filter_state() -> None:
    """The footer no longer enumerates keys — the `o` menu does. What it keeps
    is the stateful part: an active filter, which would otherwise make a
    narrowed list indistinguishable from a short one."""
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        status = _plain(app.query_one("#status", Static))
        assert "o menu" in status and "q quit" in status

        await pilot.press("enter")  # the per-level key lists are gone
        await app.workers.wait_for_complete()
        await pilot.pause()
        status = _plain(app.query_one("#status", Static))
        assert "o menu" in status
        assert "c clear" not in status and "m mark" not in status

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("slash", "b", "e", "t", "a", "enter")
        await pilot.pause()
        status = _plain(app.query_one("#status", Static))
        assert "/beta" in status and "(1/2)" in status  # the filter survives
        assert "o menu" in status


async def test_menu_lists_actions_and_runs_the_chosen_one() -> None:
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)

        # Walk to the charts toggle and select it: the menu runs the same
        # action the direct key would, then closes.
        entries = ui.menu_entries("runs", "runs", chart_shown=True)
        for _ in range([entry.action for entry in entries].index("toggle_chart")):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, MenuScreen)
        assert not app.query_one("#charts").display  # the action ran

        # Escape closes without running anything.
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MenuScreen)
        assert not app.query_one("#charts").display  # still as it was


async def test_menu_follows_the_drill_level() -> None:
    """Inside a run the menu offers the task actions, not the run ones —
    the same narrowing the footer hint used to do."""
    app = _app()
    async with app.run_test(size=(150, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")  # drill into the failed run
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, MenuScreen)
        labels = [entry.label for entry in app.screen._entries]
        assert any("Clear" in label for label in labels)
        assert not any("Trigger" in label for label in labels)
        await pilot.press("escape")
        await pilot.pause()
