"""Tests for azdo-watch: the pure layers (parsers, models, the timeline tree, error
classification, redaction), the single subprocess seam, and the Textual app
including its drill-down and its confirmation gate.

Fixtures below are trimmed captures of real responses from the `example-org` Azure DevOps
organization — build definitions, builds, a build timeline and a task log, all taken
through `az devops invoke` against API version 7.1. Names are kept because they are
already public in the repo's own skills; nothing here carries a credential, an
identity descriptor or a token.

The mutating actions are exercised **only** against fakes. Nothing here talks to a
real Azure DevOps, and nothing here may: the transport is faked at one seam
(`azdo._run`), and the app is driven with injected callables.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.style import Style
from textual.widgets import DataTable, Static

from tools.azdo_watch import api, azdo, investigate, layout, ui
from tools.azdo_watch.app import (
    RUNS_EXTEND_STEP,
    AzdoWatchApp,
    ConfirmScreen,
    DropdownScreen,
    HelpScreen,
    IssueScreen,
    LogScreen,
    ProjectScreen,
)
from tools.azdo_watch.azdo import AzdoError, PollError
from tools.azdo_watch.cli import (
    DEFAULT_RUN_LIMIT,
    MIN_INTERVAL,
    _parse_args,
    _PipelineCache,
    _states,
)
from tools.azdo_watch.models import (
    ACTION_KINDS,
    FILTER_TARGETS,
    KNOWN_RECORD_STATES,
    KNOWN_RUN_STATES,
    LOG_ERROR_QUERY,
    Action,
    Drill,
    Issue,
    Pipeline,
    PipelineList,
    PollRequest,
    Project,
    Record,
    Run,
    RunLog,
    Snapshot,
    clean_log_line,
    collect_issues,
    filter_log,
    find_urls,
    matches,
    order_records,
    record_state,
    run_state,
    sort_pipelines,
    sort_runs,
)

NOW = datetime(2026, 7, 30, 17, 0, tzinfo=timezone.utc)


# --- fixtures ---------------------------------------------------------------

# One page of `build/builds`, trimmed to the fields the parser reads plus the
# continuation token `az devops invoke` lifts out of the response header.
BUILDS_PAYLOAD = {
    "count": 3,
    "continuation_token": "2026-07-29T19:10:29.3867916Z",
    "value": [
        {
            "id": 205362,
            "buildNumber": "20260730.9",
            "status": "completed",
            "result": "failed",
            "reason": "individualCI",
            "sourceBranch": "refs/heads/develop",
            "sourceVersion": "a88456887629146b2874b225ec27ed94b0e5dcef",
            "queueTime": "2026-07-30T16:49:01.8926318Z",
            "startTime": "2026-07-30T16:49:08.8699651Z",
            "finishTime": "2026-07-30T16:52:42.4622775Z",
            "definition": {"id": 36, "name": "op-infra-tf-pi"},
            "queue": {"name": "Azure Pipelines"},
            "requestedFor": {"displayName": "Microsoft.VisualStudio.Services.TFS"},
            "tags": [],
            "_links": {
                "web": {
                    "href": "https://dev.azure.com/example-org/00000000/_build/results?buildId=205362"
                }
            },
        },
        {
            "id": 205365,
            # A pipeline with `appendCommitMessageToRunName` set: the commit subject
            # is glued onto the run number after a bullet.
            "buildNumber": "20260730.4 • Remove DDL directory from etl-service (#947)",
            "status": "inProgress",
            "result": None,
            "reason": "individualCI",
            "sourceBranch": "refs/heads/main",
            "queueTime": "2026-07-30T16:49:31.9055235Z",
            "startTime": "2026-07-30T16:49:41.2775288Z",
            "finishTime": None,
            "definition": {"id": 21, "name": "etl-service"},
            "requestedFor": {"displayName": "Microsoft.VisualStudio.Services.TFS"},
        },
        {
            "id": 205369,
            "buildNumber": "20260730.5",
            "status": "completed",
            "result": "succeeded",
            "reason": "pullRequest",
            "sourceBranch": "refs/pull/1567/merge",
            "queueTime": "2026-07-30T16:50:03.9395899Z",
            "startTime": "2026-07-30T16:50:09.8586158Z",
            "finishTime": "2026-07-30T16:52:55.6451774Z",
            "definition": {"id": 40, "name": "op-infra-tf-client-workflows"},
            "requestedFor": {"displayName": "GitHub"},
            "triggerInfo": {
                "pr.number": "1567",
                "pr.title": "Release 2026-07-30 #1",
                "pr.sourceBranch": "release/2026-07-30",
            },
        },
    ],
}

# One page of `build/definitions?includeLatestBuilds=true`.
DEFINITIONS_PAYLOAD = {
    "count": 3,
    "value": [
        {
            "id": 36,
            "name": "op-infra-tf-pi",
            "path": "\\infra",
            "queueStatus": "enabled",
            "type": "build",
            "revision": 51,
            "authoredBy": {"displayName": "Jean-Baptiste Blanchet"},
            "latestBuild": BUILDS_PAYLOAD["value"][0],
            "_links": {
                "web": {"href": "https://dev.azure.com/example-org/00000000/_build?definitionId=36"}
            },
        },
        {
            "id": 4,
            "name": "global-build",
            "path": "\\",
            "queueStatus": "paused",
            "type": "build",
            # No `latestBuild`, but a completed one — the parser falls back to it.
            "latestCompletedBuild": {
                "id": 201195,
                "buildNumber": "20260729.32",
                "status": "completed",
                "result": "partiallySucceeded",
                "reason": "manual",
                "sourceBranch": "refs/heads/master",
                "queueTime": "2026-07-29T16:52:21.429595Z",
                "startTime": "2026-07-29T16:52:46.3652881Z",
                "finishTime": "2026-07-29T17:20:00.0000000Z",
                "definition": {"id": 4, "name": "global-build"},
            },
        },
        {
            "id": 99,
            "name": "never-run",
            "path": "\\",
            "queueStatus": "disabled",
            "type": "build",
        },
    ],
}

PROJECTS_PAYLOAD = {
    "value": [
        {
            "id": "00000000-1111-2222-3333-444444444444",
            "name": "Main",
            "state": "wellFormed",
            "visibility": "private",
            "lastUpdateTime": "2024-04-05T20:25:38.263000+00:00",
        },
        {
            "id": "9f0a1b2c-0000-0000-0000-000000000000",
            "name": "Archive",
            "state": "wellFormed",
            "visibility": "private",
        },
    ]
}

# A build timeline, trimmed to one stage that failed. The real shape: Stage → Phase
# → Job → Task, joined by `parentId`, with `order` for siblings.
TIMELINE_PAYLOAD = {
    "records": [
        {
            "id": "stage-plan",
            "parentId": None,
            "order": 6,
            "type": "Stage",
            "name": "Terraform - Plan",
            "identifier": "planTerraform",
            "refName": "planTerraform",
            "state": "completed",
            "result": "failed",
            "startTime": "2026-07-30T16:51:53.1500000Z",
            "finishTime": "2026-07-30T16:52:41.5300000Z",
            "attempt": 1,
        },
        {
            "id": "phase-plan",
            "parentId": "stage-plan",
            "order": 1,
            "type": "Phase",
            "name": "Terraform > init & plan",
            "state": "completed",
            "result": "failed",
            "startTime": "2026-07-30T16:51:53.1500000Z",
            "finishTime": "2026-07-30T16:52:41.5300000Z",
            "log": {"id": 52},
            "attempt": 1,
        },
        {
            "id": "job-plan",
            "parentId": "phase-plan",
            "order": 1,
            "type": "Job",
            "name": "Terraform > init & plan",
            "state": "completed",
            "result": "failed",
            "startTime": "2026-07-30T16:51:53.1500000Z",
            "finishTime": "2026-07-30T16:52:26.3166667Z",
            "log": {"id": 63},
            "workerName": "ci-prod-2-2",
            "attempt": 1,
        },
        {
            "id": "task-init",
            "parentId": "job-plan",
            "order": 4,
            "type": "Task",
            "name": "Run > terraform init",
            "state": "completed",
            "result": "succeeded",
            "startTime": "2026-07-30T16:51:57.0000000Z",
            "finishTime": "2026-07-30T16:52:12.0000000Z",
            "log": {"id": 16},
            "workerName": "ci-prod-2-2",
            "attempt": 1,
        },
        {
            "id": "task-show",
            "parentId": "job-plan",
            "order": 6,
            "type": "Task",
            "name": "Run > terraform show",
            "state": "completed",
            "result": "failed",
            "startTime": "2026-07-30T16:52:20.0000000Z",
            "finishTime": "2026-07-30T16:52:22.0000000Z",
            "log": {"id": 58},
            "workerName": "ci-prod-2-2",
            "attempt": 1,
            "errorCount": 1,
            "issues": [
                {
                    "type": "error",
                    "category": "General",
                    "message": "Bash exited with code '1'.",
                    "data": {"logFileLineNumber": "25", "type": "error"},
                }
            ],
        },
        {
            "id": "task-publish",
            "parentId": "job-plan",
            "order": 7,
            "type": "Task",
            "name": "PublishPipelineArtifact",
            "state": "completed",
            "result": "failed",
            "startTime": "2026-07-30T16:52:22.5000000Z",
            "finishTime": "2026-07-30T16:52:23.0000000Z",
            "log": {"id": 61},
            "attempt": 1,
            "errorCount": 1,
            "issues": [
                {
                    "type": "error",
                    "message": "Path does not exist: /work/s/tf-pi/terraform.tfplan",
                    "data": {"logFileLineNumber": "10"},
                }
            ],
        },
        {
            "id": "checkpoint-plan",
            "parentId": "stage-plan",
            "order": 0,
            "type": "Checkpoint",
            "name": "Checkpoint",
            "state": "completed",
            "result": "succeeded",
            "startTime": "2026-07-30T16:51:50.0000000Z",
            "finishTime": "2026-07-30T16:51:50.0000000Z",
            "attempt": 1,
        },
        {
            "id": "stage-apply",
            "parentId": None,
            "order": 7,
            "type": "Stage",
            "name": "Terraform - Apply",
            "identifier": "applyTerraform",
            "state": "completed",
            "result": "skipped",
            "attempt": 1,
            # Warnings without errors: the record is green-with-issues, not failed.
            "warningCount": 1,
            "issues": [
                {"type": "warning", "message": "Stage skipped because Plan failed."}
            ],
        },
    ]
}

# A `$format=json` log body: an array of lines, each stamped by the agent, with an
# ANSI colour code left in by the task runner.
LOG_PAYLOAD = {
    "value": [
        "2026-07-30T16:52:20.1532775Z ##[section]Starting: Run > terraform show",
        "2026-07-30T16:52:20.2000000Z \x1b[31mError: Failed to read the given file\x1b[0m",
        "2026-07-30T16:52:20.3000000Z see https://developer.hashicorp.com/terraform.",
        "2026-07-30T16:52:21.0000000Z ##[error]Bash exited with code '1'.",
    ]
}


def _plain(renderable: object, width: int = 150) -> str:
    """A renderable's text with no ANSI codes — what a user would read.

    `color_system=None` matters: styling otherwise injects escape sequences
    *inside* a line, so `"##[error]boom" in output` fails on a highlighted match
    even though that is exactly what is on screen.
    """
    console = Console(width=width, color_system=None)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _widget_text(widget: Static) -> str:
    """The plain text a Static widget was last updated with."""
    return _plain(widget.content, width=150)


# --- the status/result fold ---------------------------------------------------


def test_run_state_folds_status_and_result() -> None:
    """A completed run *is* its result; anything in flight is its status, mapped to
    the shared vocabulary where the two agree."""
    assert run_state("completed", "succeeded") == "succeeded"
    assert run_state("completed", "failed") == "failed"
    assert run_state("completed", "partiallySucceeded") == "partiallySucceeded"
    assert run_state("completed", "canceled") == "canceled"
    assert run_state("inProgress", "") == "running"
    assert run_state("inProgress", None or "") == "running"
    assert run_state("notStarted", "") == "queued"
    assert run_state("cancelling", "") == "cancelling"
    assert run_state("postponed", "") == "postponed"


def test_run_state_survives_an_unknown_status() -> None:
    """A status Azure DevOps has not shipped yet comes back unchanged rather than
    being forced into a bucket it does not belong in."""
    assert run_state("teleporting", "") == "teleporting"
    assert run_state("", "") == "none"
    # A completed build whose result the service omitted still has to render.
    assert run_state("completed", "") == "none"


def test_record_state_folds_the_record_vocabulary() -> None:
    assert record_state("completed", "succeededWithIssues") == "succeededWithIssues"
    assert record_state("completed", "skipped") == "skipped"
    assert record_state("completed", "abandoned") == "abandoned"
    assert record_state("inProgress", "") == "running"
    assert record_state("pending", "") == "pending"
    assert record_state("", "") == "none"


def test_every_known_state_has_a_colour() -> None:
    """`state_style` is the only place a state becomes a colour, and the known set
    must never contain one it has to fall back for — that fallback is for *future*
    states, not for ones this build already names."""
    for state in (*KNOWN_RUN_STATES, *KNOWN_RECORD_STATES):
        glyph, style = ui.state_style(state)
        assert (glyph, style) != ui.FALLBACK_STATE_STYLE, state
        assert glyph and style


def test_unknown_state_gets_the_fallback_not_an_exception() -> None:
    assert ui.state_style("awaiting_alignment") == ui.FALLBACK_STATE_STYLE
    assert ui.state_style("") == ui.state_style("none")
    assert "awaiting_alignment" in _plain(ui.state_cell("awaiting_alignment"))


# --- parsing ----------------------------------------------------------------


def test_parse_runs_reads_the_fields_the_ui_shows() -> None:
    runs = api.parse_runs(BUILDS_PAYLOAD, "example-org", "Main")
    assert [run.id for run in runs] == [205362, 205365, 205369]

    failed, in_flight, pr_run = runs
    assert failed.pipeline_name == "op-infra-tf-pi"
    assert failed.state == "failed"
    assert failed.trigger == "CI"
    assert failed.branch_label == "develop"
    assert failed.short_commit == "a8845688"
    assert failed.duration is not None and round(failed.duration) == 214
    assert failed.queued_for is not None and round(failed.queued_for) == 7
    assert failed.queue_name == "Azure Pipelines"
    assert failed.web_url.endswith("buildId=205362")

    assert in_flight.state == "running"
    assert in_flight.in_flight is True
    assert in_flight.duration is None  # still going: no finish time
    assert in_flight.branch_label == "main"

    assert pr_run.state == "succeeded"
    assert pr_run.pr_number == "1567"
    assert pr_run.branch_label == "PR 1567"
    assert pr_run.description == "Release 2026-07-30 #1"


def test_parse_run_splits_an_appended_commit_subject_off_the_number() -> None:
    """A pipeline with `appendCommitMessageToRunName` puts the commit subject into
    `buildNumber`; the number stays short and the subject becomes the description."""
    run = api.parse_runs(BUILDS_PAYLOAD)[1]
    assert run.number == "20260730.4"
    assert run.description == "Remove DDL directory from etl-service (#947)"


def test_run_description_never_falls_back_to_the_branch() -> None:
    """The branch has its own column; repeating it in the description would spend
    width on something already on screen."""
    run = Run(id=1, build_number="20260730.1", branch="refs/heads/develop")
    assert run.description == ""
    assert run.branch_label == "develop"


def test_parse_run_returns_none_for_an_empty_payload() -> None:
    """The callers that reach here with nothing are asking "is there one?", and an
    exception is the wrong answer to that question."""
    assert api.parse_run({}) is None
    assert api.parse_run({"status": "completed"}) is None


def test_parse_run_survives_missing_and_null_fields() -> None:
    run = api.parse_run({"id": 7, "definition": None, "requestedFor": None})
    assert run is not None
    assert run.pipeline_name == ""
    assert run.pipeline_id == 0
    assert run.state == "none"
    assert run.happened_at is None
    assert run.number == "7"  # falls back to the id when there is no number


def test_parse_seven_digit_fractional_timestamps() -> None:
    """Azure DevOps stamps seven fractional digits — one more than `datetime`
    accepts — so the fraction is truncated rather than the stamp rejected."""
    run = api.parse_runs(BUILDS_PAYLOAD)[0]
    finished = run.finish_time
    assert finished == datetime(2026, 7, 30, 16, 52, 42, 462277, tzinfo=timezone.utc)
    assert finished is not None and finished.tzinfo is timezone.utc


def test_parse_timestamp_keeps_a_non_z_offset() -> None:
    """`az devops project list` normalizes to `+00:00`; truncating the fraction must
    not eat the offset that follows it."""
    project_payload = {"value": [{"id": "x", "name": "X"}]}
    assert api.parse_projects(project_payload)[0].name == "X"
    run = api.parse_run(
        {"id": 1, "startTime": "2026-07-30T16:49:08.1234567+00:00"}
    )
    assert run is not None and run.start_time is not None
    assert run.start_time.utcoffset() == timedelta(0)
    assert run.start_time.microsecond == 123456


def test_parse_unreadable_timestamp_becomes_none_not_an_error() -> None:
    run = api.parse_run({"id": 1, "startTime": "not a date"})
    assert run is not None and run.start_time is None


def test_parse_pipelines_folds_in_the_latest_build() -> None:
    pipelines = api.parse_pipelines(DEFINITIONS_PAYLOAD, "example-org", "Main")
    assert [p.name for p in pipelines] == [
        "op-infra-tf-pi",
        "global-build",
        "never-run",
    ]
    infra, global_build, never = pipelines

    assert infra.folder == "infra"
    assert infra.is_runnable is True
    assert infra.last_run is not None and infra.last_run.state == "failed"
    assert infra.needs_attention is True
    assert infra.authored_by == "Jean-Baptiste Blanchet"

    # No `latestBuild`, so the completed one is used rather than showing nothing.
    assert global_build.is_paused is True
    assert global_build.folder == ""  # a lone backslash is the root
    assert global_build.last_run is not None
    assert global_build.last_run.state == "partiallySucceeded"
    assert global_build.needs_attention is True

    assert never.is_disabled is True
    assert never.is_runnable is False
    assert never.last_run is None
    assert never.needs_attention is False


def test_parse_projects_is_name_ordered() -> None:
    """A switcher whose entries move between polls is one where "press 2" means
    something different each time you open it."""
    projects = api.parse_projects(PROJECTS_PAYLOAD, "example-org")
    assert [p.name for p in projects] == ["Archive", "Main"]
    assert projects[1].label == "example-org / Main"
    assert projects[1].route == "00000000-1111-2222-3333-444444444444"
    assert projects[1].web_url.startswith("https://dev.azure.com/example-org/")


def test_parse_timeline_reads_the_tree_and_its_issues() -> None:
    records = api.parse_timeline(TIMELINE_PAYLOAD)
    assert len(records) == 8
    by_id = {record.id: record for record in records}

    show = by_id["task-show"]
    assert show.display_state == "failed"
    assert show.failed is True
    assert show.log_id == 58
    assert show.worker_name == "ci-prod-2-2"
    assert len(show.errors) == 1
    assert show.errors[0].log_line == 25  # arrives as a *string* on the wire
    assert show.errors[0].message == "Bash exited with code '1'."

    # A stage has no log of its own, and `None` must not be confused with log 0 —
    # which is a real log id (the build's own container log).
    assert by_id["stage-plan"].log_id is None
    assert by_id["stage-plan"].has_log is False
    assert by_id["job-plan"].has_log is True

    # A stage is addressed in a route by its YAML identifier, which is a different
    # string from its display name — retrying by the display name is a 404.
    assert by_id["stage-plan"].name == "Terraform - Plan"
    assert by_id["stage-plan"].ref_name == "planTerraform"
    assert by_id["stage-plan"].route_name == "planTerraform"
    # A record with no identifier falls back to its name rather than an empty route
    # segment, so the request fails with something a user can read.
    assert by_id["task-show"].ref_name == ""
    assert by_id["task-show"].route_name == "Run > terraform show"

    apply_stage = by_id["stage-apply"]
    assert apply_stage.display_state == "skipped"
    assert apply_stage.failed is False
    assert apply_stage.errors == ()  # a warning is not an error
    assert apply_stage.warning_count == 1


def test_parse_timeline_treats_log_zero_as_a_real_log() -> None:
    records = api.parse_timeline(
        {"records": [{"id": "a", "type": "Task", "log": {"id": 0}}]}
    )
    assert records[0].log_id == 0
    assert records[0].has_log is True


def test_parse_timeline_drops_a_record_with_no_id() -> None:
    """Nothing can be keyed, ordered or fetched without one, and a table row with no
    key is a crash on the next render."""
    records = api.parse_timeline(
        {"records": [{"type": "Task", "name": "nameless"}, {"id": "a"}]}
    )
    assert [record.id for record in records] == ["a"]


def test_parse_log_strips_the_agent_timestamp_and_ansi_codes() -> None:
    """Both are noise in a pane with its own line numbers and colours — and the
    escape codes are control bytes, which Rich would render as mojibake."""
    log = api.parse_log(LOG_PAYLOAD, 58)
    assert log.line_count == 4
    assert log.lines[0] == "##[section]Starting: Run > terraform show"
    assert log.lines[1] == "Error: Failed to read the given file"
    assert "\x1b" not in log.content
    assert not re.search(r"\d{4}-\d\d-\d\dT", log.content)


def test_parse_log_preserves_line_numbers() -> None:
    """Cleaning is per line and never joins or drops one, so an issue's
    `logFileLineNumber` points where it says it does."""
    log = api.parse_log(LOG_PAYLOAD, 58)
    hits, total = filter_log(log.content, LOG_ERROR_QUERY)
    assert total == 4
    assert hits == [(4, "##[error]Bash exited with code '1'.")]


def test_parse_log_accepts_a_plain_text_body() -> None:
    log = api.parse_log({"value": "one\ntwo"}, 3)
    assert log.lines == ["one", "two"]
    assert api.parse_log({}, 3).content == ""


def test_clean_log_line_leaves_ordinary_text_alone() -> None:
    assert clean_log_line("plain output") == "plain output"
    assert clean_log_line("2026-07-30T16:52:20.1Z hello") == "hello"
    # A timestamp that is not the agent's prefix must survive.
    assert clean_log_line("built at 2026-07-30T16:52:20Z") == "built at 2026-07-30T16:52:20Z"


def test_continuation_token_reads_either_spelling() -> None:
    assert api.continuation_token(BUILDS_PAYLOAD) == "2026-07-29T19:10:29.3867916Z"
    assert api.continuation_token({"continuationToken": "abc"}) == "abc"
    assert api.continuation_token({}) == ""
    assert api.continuation_token({"continuation_token": ""}) == ""


def test_parse_error_detail_quotes_the_service() -> None:
    assert (
        api.parse_error_detail({"message": "TF400813: not authorized", "typeKey": "X"})
        == "TF400813: not authorized"
    )
    assert (
        api.parse_error_detail({"innerException": {"message": "deeper"}}) == "deeper"
    )
    assert api.parse_error_detail("not a dict") is None
    assert api.parse_error_detail({}) is None


# --- query parameters -------------------------------------------------------


def test_builds_params_order_by_queue_time() -> None:
    """The default orders by finish time, which puts every *running* build — the ones
    with no finish time at all — in an undocumented order."""
    params = api.builds_params(top=50)
    assert params["queryOrder"] == "queueTimeDescending"
    assert params["$top"] == "50"
    assert "statusFilter" not in params
    assert "continuationToken" not in params


def test_builds_params_clamp_top_and_carry_filters() -> None:
    assert api.builds_params(top=10_000)["$top"] == str(api.MAX_TOP)
    assert api.builds_params(top=0)["$top"] == "1"
    params = api.builds_params(
        states=("inProgress", "notStarted"), definition_ids=(4, 36), continuation="tok"
    )
    assert params["statusFilter"] == "inProgress,notStarted"
    assert params["definitions"] == "4,36"
    assert params["continuationToken"] == "tok"


def test_definitions_params_ask_for_the_latest_builds() -> None:
    """Without it the Pipelines view cannot say whether a pipeline's last run passed
    unless that run happens to be inside the loaded window."""
    params = api.definitions_params()
    assert params["includeLatestBuilds"] == "true"
    # A user typing into a filter box means "contains", not "starts with".
    assert api.definitions_params(name_filter="infra")["name"] == "*infra*"


def test_log_params_request_json() -> None:
    """`text/plain` is the alternative, and `az devops invoke` cannot hand it back."""
    assert api.log_params() == {"$format": "json"}


# --- mutation requests ------------------------------------------------------


def test_queue_request_omits_the_branch_when_none_is_named() -> None:
    """An empty branch lets the service use the pipeline's own default; guessing
    `refs/heads/main` would be wrong for every repo on master or develop."""
    method, body, params = api.queue_request(36)
    assert method == "POST"
    assert body == {"definition": {"id": 36}}
    assert params == {}
    _, with_branch, _ = api.queue_request(36, "refs/heads/develop")
    assert with_branch["sourceBranch"] == "refs/heads/develop"


def test_cancel_request_asks_for_the_transitional_state() -> None:
    """The caller asks the orchestrator to stop; the run reaches `canceled` itself.
    Asking for the terminal state directly is rejected by the service."""
    method, body, _ = api.cancel_request(205362)
    assert method == "PATCH"
    assert body == {"status": "cancelling"}


def test_retry_stage_request() -> None:
    method, body, _ = api.retry_stage_request(205362, "Terraform - Plan")
    assert method == "PATCH"
    assert body["state"] == "retry"


def test_mutation_requests_refuse_what_must_not_be_sent() -> None:
    """Refused *before* anything reaches the service, rather than falling through to
    a default that does something."""
    for bad in (
        Action(kind="queue", pipeline_id=0),
        Action(kind="cancel", run_id=0),
        Action(kind="retry_stage", run_id=1, stage_name="  "),
        Action(kind="retry_stage", run_id=0, stage_name="Plan"),
        Action(kind="explode", run_id=1),
    ):
        try:
            api.mutation_request(bad)
        except api.AzdoApiError:
            continue
        raise AssertionError(f"{bad.kind} was not refused")


def test_every_action_kind_has_a_request_and_a_title() -> None:
    """The taxonomy cannot rot: a kind added to `ACTION_KINDS` without a request
    builder or a modal title is a failing test rather than a silent no-op."""
    for kind in ACTION_KINDS:
        action = Action(
            kind=kind, pipeline_id=1, run_id=1, stage_name="Stage", pipeline_name="p"
        )
        method, _body, _params = api.mutation_request(action)
        assert method in ("POST", "PATCH")
        assert action.title != kind  # a real sentence, not the raw discriminator
        assert action.target


# --- the timeline tree -------------------------------------------------------


def test_order_records_builds_the_azdo_tree() -> None:
    rows = order_records(api.parse_timeline(TIMELINE_PAYLOAD))
    labels = [row.label for row in rows]
    assert labels == [
        "Terraform - Plan",
        "├─ Checkpoint",
        "└─ Terraform > init & plan",
        "   └─ Terraform > init & plan",
        "      ├─ Run > terraform init",
        "      ├─ Run > terraform show",
        "      └─ PublishPipelineArtifact",
        "Terraform - Apply",
    ]
    assert [row.depth for row in rows] == [0, 1, 1, 2, 3, 3, 3, 0]
    assert [row.position for row in rows] == list(range(1, 9))
    assert not any(row.unplaced for row in rows)


def test_order_records_is_total() -> None:
    """Every record handed in comes back out exactly once, whether or not the tree
    could place it. Dropping a row from a monitoring view is a correctness bug."""
    records = api.parse_timeline(TIMELINE_PAYLOAD)
    rows = order_records(records)
    assert len(rows) == len(records)
    assert {row.record.id for row in rows} == {record.id for record in records}


def test_order_records_reparents_an_orphan_to_the_root() -> None:
    """A record whose parent is absent from the timeline is a truthful top-level row
    — not an error, and not a dropped one."""
    records = [
        Record(id="a", name="root", type="Stage", order=1),
        Record(id="b", name="orphan", parent_id="missing", type="Job", order=2),
    ]
    rows = order_records(records)
    assert [(row.label, row.depth, row.unplaced) for row in rows] == [
        ("root", 0, False),
        ("orphan", 0, False),
    ]


def test_order_records_marks_a_cycle_instead_of_looping() -> None:
    records = [
        Record(id="a", name="a", parent_id="b"),
        Record(id="b", name="b", parent_id="a"),
        Record(id="c", name="c"),
    ]
    rows = order_records(records)
    assert len(rows) == 3
    unplaced = {row.record.id for row in rows if row.unplaced}
    assert unplaced == {"a", "b"}
    assert [row.label for row in rows if not row.unplaced] == ["c"]


def test_order_records_orders_siblings_stably() -> None:
    """Ties break all the way down to the name, because rows that reshuffle between
    refreshes read as the pipeline having changed."""
    records = [
        Record(id="z", name="zebra", order=1),
        Record(id="a", name="apple", order=1),
        Record(id="m", name="mango", order=0),
    ]
    first = [row.label for row in order_records(records)]
    second = [row.label for row in order_records(list(reversed(records)))]
    assert first == ["mango", "apple", "zebra"]
    assert first == second


def test_order_records_of_nothing_is_nothing() -> None:
    assert order_records([]) == []


def test_collect_issues_puts_errors_before_warnings_in_tree_order() -> None:
    pairs = collect_issues(api.parse_timeline(TIMELINE_PAYLOAD))
    assert [(record.name, issue.type) for record, issue in pairs] == [
        ("Run > terraform show", "error"),
        ("PublishPipelineArtifact", "error"),
        ("Terraform - Apply", "warning"),
    ]


# --- sorting and derived counts ---------------------------------------------


def test_sort_runs_is_newest_first_and_dates_a_queued_run() -> None:
    """A queued run has no start time and must still have a place in time —
    otherwise the newest thing on the dashboard sorts to the bottom."""
    queued = Run(id=3, status="notStarted", queue_time=NOW)
    old = Run(id=1, status="completed", result="succeeded", start_time=NOW - timedelta(hours=2))
    recent = Run(id=2, status="completed", result="failed", start_time=NOW - timedelta(minutes=5))
    undatable = Run(id=4, status="completed", result="succeeded")
    ordered = sort_runs([old, recent, queued, undatable])
    assert [run.id for run in ordered] == [3, 2, 1, 4]


def test_sort_pipelines_is_most_recently_run_first() -> None:
    ran_now = Pipeline(id=1, name="hot", last_run=Run(id=1, start_time=NOW))
    ran_old = Pipeline(
        id=2, name="cold", last_run=Run(id=2, start_time=NOW - timedelta(days=30))
    )
    never_b = Pipeline(id=3, name="b-never")
    never_a = Pipeline(id=4, name="a-never")
    ordered = sort_pipelines([never_b, ran_old, never_a, ran_now])
    assert [p.name for p in ordered] == ["hot", "cold", "a-never", "b-never"]


def _snapshot(
    runs: tuple[Run, ...] | None = None,
    pipelines: tuple[Pipeline, ...] | None = None,
    *,
    runs_more: bool = False,
    projects: tuple[Project, ...] | None = None,
) -> Snapshot:
    project = Project(id="00000000", name="Main", org="example-org")
    archive = Project(id="9f0a", name="Archive", org="example-org")
    parsed_runs = runs if runs is not None else tuple(api.parse_runs(BUILDS_PAYLOAD))
    parsed_pipelines = (
        pipelines
        if pipelines is not None
        else tuple(api.parse_pipelines(DEFINITIONS_PAYLOAD))
    )
    return Snapshot(
        project=project,
        projects=projects if projects is not None else (project, archive),
        runs=tuple(sort_runs(list(parsed_runs))),
        pipelines=tuple(sort_pipelines(list(parsed_pipelines))),
        calls=4,
        elapsed=4.46,
        runs_more=runs_more,
        pipelines_total=len(parsed_pipelines),
    )


def test_snapshot_derives_in_flight_counts_per_pipeline() -> None:
    snapshot = _snapshot()
    assert snapshot.in_flight_counts() == {21: 1}  # etl-service is inProgress
    assert snapshot.state_counts("failed") == {36: 1}
    assert snapshot.state_counts("succeeded") == {40: 1}


def test_snapshot_latest_run_prefers_the_fresher_of_cache_and_window() -> None:
    """The cached inventory can be up to its TTL stale and the runs window only
    reaches back so far, so the newer of the two wins. Without this, a pipeline that
    started a run since the inventory was cached would show its *previous* run for
    the rest of the TTL."""
    stale = Run(id=1, build_number="old", pipeline_id=36, start_time=NOW - timedelta(hours=1))
    fresh = Run(id=2, build_number="new", pipeline_id=36, start_time=NOW)
    pipeline = Pipeline(id=36, name="op-infra-tf-pi", last_run=stale)
    snapshot = _snapshot(runs=(fresh,), pipelines=(pipeline,))
    latest = snapshot.latest_run_for(pipeline)
    assert latest is not None and latest.build_number == "new"

    # And the other direction: a pipeline whose last run predates the window keeps
    # the cached one rather than showing nothing.
    ancient = Pipeline(id=99, name="op-glue", last_run=stale)
    kept = _snapshot(runs=(fresh,), pipelines=(ancient,)).latest_run_for(ancient)
    assert kept is not None and kept.build_number == "old"


def test_snapshot_lookups() -> None:
    snapshot = _snapshot()
    assert snapshot.pipeline(36) is not None
    assert snapshot.pipeline(12345) is None
    assert snapshot.run(205362) is not None
    assert snapshot.run(1) is None
    assert snapshot.paused_count == 1
    assert snapshot.disabled_count == 1


# --- filtering ---------------------------------------------------------------


def test_matches_requires_every_term_in_any_order() -> None:
    assert matches("", "anything")
    assert matches("failed infra", "op-infra-tf-pi failed")
    assert matches("infra failed", "op-infra-tf-pi failed")
    assert not matches("failed lint", "op-infra-tf-pi failed")
    assert matches("FAILED", "failed")  # case-insensitive


def test_filter_log_keeps_original_line_numbers() -> None:
    content = "alpha\nbeta\ngamma\nbeta again"
    hits, total = filter_log(content, "beta")
    assert total == 4
    assert hits == [(2, "beta"), (4, "beta again")]
    assert filter_log(content, "  ")[0] == list(enumerate(content.splitlines(), 1))


def test_run_search_text_covers_what_a_user_would_type() -> None:
    run = api.parse_runs(BUILDS_PAYLOAD)[2]
    for term in ("client-workflows", "1567", "Release", "PR", "succeeded", "GitHub"):
        assert matches(term, run.search_text), term


def test_record_search_text_includes_issue_messages() -> None:
    """`/tfplan` should find the step that could not find the plan file."""
    records = api.parse_timeline(TIMELINE_PAYLOAD)
    show = next(r for r in records if r.name == "PublishPipelineArtifact")
    assert matches("terraform.tfplan", show.search_text)


def test_filter_targets_cover_every_list() -> None:
    assert set(FILTER_TARGETS) == {"runs", "pipelines", "watched", "records", "log"}


# --- links in a log ----------------------------------------------------------


def test_find_urls_returns_spans_and_trims_trailing_prose() -> None:
    line = "see https://dev.azure.com/example-org/_build?buildId=1, or (https://x.test/a)."
    spans = find_urls(line)
    assert [url for _, _, url in spans] == [
        "https://dev.azure.com/example-org/_build?buildId=1",
        "https://x.test/a",
    ]
    start, end, url = spans[0]
    assert line[start:end] == url


def test_find_urls_keeps_a_balanced_parenthesis() -> None:
    assert find_urls("https://host/a_(b)")[0][2] == "https://host/a_(b)"


def test_log_line_styles_the_azdo_markers() -> None:
    """`##[error]` is the structure inside an otherwise flat log; a log that skims
    the way it does in the browser is the point."""
    assert ui.marker_style("##[error]Bash exited with code '1'.") == "bold red"
    assert ui.marker_style("  ##[warning]deprecated") == "bold yellow"
    assert ui.marker_style("##[section]Starting: Lint") == "bold cyan"
    assert ui.marker_style("ordinary output") is None


def test_log_line_makes_urls_clickable_and_names_the_app_action() -> None:
    rendered = ui.log_line("see https://x.test/a", "")
    styles = [
        style
        for _start, _end, style in rendered.spans
        if isinstance(style, Style)
    ]
    assert any(style.link == "https://x.test/a" for style in styles)
    assert any(ui.LINK_ACTION in str(style.meta.get("@click", "")) for style in styles)


def test_the_link_action_exists_on_the_app() -> None:
    """`ui.LINK_ACTION` is the one place the render layer names the app it renders
    into, so a rename that breaks every clickable link should break a test."""
    assert ui.LINK_ACTION == "app.open_link"
    assert hasattr(AzdoWatchApp, "action_open_link")


# --- error classification ----------------------------------------------------


def _classify(detail: str) -> PollError:
    return azdo.classify_azdo_error(
        AzdoError(f"`az devops invoke …` failed: {detail}")
    )


def test_classification_covers_every_kind() -> None:
    """Every kind must be reachable, so the taxonomy cannot rot into "error"."""
    cases = {
        "missing_cli": "`az` is not installed or not on PATH.",
        "missing_extension": "'devops' is not in the 'az' command group",
        "auth": "Before you can run this command you need to log in",
        "rate_limited": "TF429: too many requests. Retry-After: 42",
        "forbidden": "TF400813: The user is not authorized to access this resource",
        "not_found": "Error: API request failed with status 404",
        "unknown": "the pipe broke",
    }
    seen = {_classify(detail).kind for detail in cases.values()}
    assert seen == set(azdo.KINDS), sorted(set(azdo.KINDS) - seen)
    for kind, detail in cases.items():
        assert _classify(detail).kind == kind, detail


def test_rate_limit_carries_the_services_retry_hint() -> None:
    error = _classify("429 too many requests. Retry-After: 42")
    assert error.rate_limited is True
    assert error.retry_after == 42
    assert _classify("429 too many requests").retry_after is None


def test_unrecoverable_kinds_are_not_worth_a_timer() -> None:
    for detail, recoverable in (
        ("`az` is not installed or not on PATH.", False),
        ("'devops' is not in the 'az' command group", False),
        ("Error: API request failed with status 404", False),
        ("TF400813: The user is not authorized", True),
        ("the pipe broke", True),
    ):
        assert _classify(detail).recoverable is recoverable, detail


def test_a_401_classifies_as_auth_not_forbidden() -> None:
    """A 401 is "this credential is not usable"; a 403 is "this credential is fine
    but you may not read that". The fixes are different commands."""
    assert _classify("API request failed with status 401").kind == "auth"
    assert _classify("API request failed with status 403").kind == "forbidden"


def test_classification_quotes_the_services_own_message() -> None:
    body = json.dumps({"message": "The pipeline 999 does not exist.", "typeKey": "X"})
    error = _classify(f"Error: {body}")
    assert error.message == "The pipeline 999 does not exist."


def test_a_refused_request_is_typed_and_not_retried() -> None:
    """`api` refuses a request that must not be sent; it never reached the service,
    so it is not a transport failure."""
    error = azdo.classify_azdo_error(api.AzdoApiError("A pipeline id is required."))
    assert error.kind == "not_found"
    assert error.recoverable is False
    assert "pipeline id" in error.message


def test_a_timeout_says_so_without_the_command() -> None:
    error = azdo.classify_azdo_error(AzdoError("`az devops invoke …` timed out after 60s."))
    assert "timed out" in error.message
    assert "az devops" not in error.message


# --- redaction ---------------------------------------------------------------


def test_redaction_removes_pat_and_bearer_shapes() -> None:
    """An error path is exactly where a credential is most likely to be echoed back
    at us — `az --debug` prints whole requests, and a 401 body can quote the header
    it rejected."""
    pat = "a" * 26 + "b" * 26  # 52 chars of the alphabet a PAT uses
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef"
    for secret, text in (
        (pat, f"auth failed for token {pat}"),
        (jwt, f"Authorization: Bearer {jwt}"),
        (jwt, f"bearer {jwt} was rejected"),
    ):
        scrubbed = azdo._redact(text)
        assert secret not in scrubbed, text
        assert azdo.REDACTED in scrubbed


def test_redaction_reaches_classified_messages() -> None:
    pat = "z" * 52
    error = _classify(f"401 unauthorized: pat={pat}")
    assert pat not in error.message


def test_redaction_leaves_ordinary_text_alone() -> None:
    text = "TF400813: The user is not authorized to access this resource."
    assert azdo._redact(text) == text


def test_the_displayed_command_is_redacted_and_truncated() -> None:
    pat = "q" * 52
    shown = azdo._display_command(["az", "devops", "invoke", "--pat", pat, "x" * 80])
    assert pat not in shown
    assert "…" in shown


# --- the transport seam ------------------------------------------------------


class _Fake:
    """Records every argv `azdo._run` was handed and replays canned stdout.

    Routed by the argv rather than by call order, because `fetch_snapshot` fans its
    calls out across a thread pool — a positional queue would hand the definitions
    payload to whichever call happened to win the race, which is how a fake starts
    testing the scheduler instead of the code.
    """

    def __init__(self, *responses: object, **by_resource: object) -> None:
        self.calls: list[list[str]] = []
        self.responses = list(responses)
        self.by_resource = dict(by_resource)

    def __call__(self, args: list[str], *, timeout: float = 0.0, **_: object) -> str:
        self.calls.append(list(args))
        payload = self._pick(args)
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload)

    def _pick(self, args: list[str]) -> object:
        if self.by_resource:
            resource = args[args.index("--resource") + 1] if "--resource" in args else ""
            in_flight = any(arg.startswith("statusFilter=inProgress") for arg in args)
            for key in (("in_flight" if in_flight else ""), resource):
                if key and key in self.by_resource:
                    return self.by_resource[key]
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _project() -> Project:
    return Project(id="00000000", name="Main", org="example-org")


def test_every_invoke_pins_the_api_version(monkeypatch) -> None:
    """Left to the extension's default, the timeline and stage resources resolve to
    whichever preview the installed `az devops` prefers — so a monitoring tool would
    change behaviour on an unrelated `az extension update`."""
    fake = _Fake(TIMELINE_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    azdo.fetch_run_timeline(_project(), Run(id=205362))
    argv = fake.calls[0]
    assert argv[0] == azdo.AZ
    assert argv[1:3] == ["devops", "invoke"]
    assert "--api-version" in argv
    assert argv[argv.index("--api-version") + 1] == api.API_VERSION
    assert "--org" in argv
    assert argv[argv.index("--org") + 1] == "https://dev.azure.com/example-org"
    route = argv.index("--route-parameters")
    assert "project=00000000" in argv[route:]
    assert "buildId=205362" in argv[route:]


def test_org_name_accepts_a_name_or_a_url() -> None:
    for given in ("example-org", "https://dev.azure.com/example-org", "https://dev.azure.com/example-org/"):
        assert azdo.org_name(given) == "example-org"
    assert azdo.org_url("example-org") == "https://dev.azure.com/example-org"
    assert azdo.org_url("https://dev.azure.com/example-org") == "https://dev.azure.com/example-org"


def test_fetch_snapshot_asks_for_in_flight_runs_separately(monkeypatch) -> None:
    """The central promise of the dashboard: the main window is bounded and ordered
    by queue time, so a build running since last week is not in it. One extra call
    guarantees that if something is running, it is on screen."""
    fake = _Fake(builds=BUILDS_PAYLOAD, definitions=DEFINITIONS_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    snapshot = azdo.fetch_snapshot(_project(), limit=50)

    filters = [
        arg
        for argv in fake.calls
        for arg in argv
        if arg.startswith("statusFilter=")
    ]
    assert filters == ["statusFilter=inProgress,notStarted,cancelling"]
    # The overlap between the two lists is deduplicated by run id, not appended —
    # a repeat would double a row and break the table's keying.
    assert len(snapshot.runs) == 3
    assert len({run.key for run in snapshot.runs}) == 3


def test_fetch_snapshot_skips_the_in_flight_call_when_states_are_given(monkeypatch) -> None:
    """An explicit `--state` is the user saying which states they want; silently
    adding two back would be ignoring them."""
    fake = _Fake(builds=BUILDS_PAYLOAD, definitions=DEFINITIONS_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    azdo.fetch_snapshot(_project(), limit=50, states=("completed",))
    filters = {
        arg for argv in fake.calls for arg in argv if arg.startswith("statusFilter=")
    }
    assert filters == {"statusFilter=completed"}


def test_fetch_snapshot_reuses_a_cached_inventory(monkeypatch) -> None:
    """The inventory is the expensive half of a poll (~4.9s and a megabyte) while it
    only changes when someone edits a pipeline."""
    fake = _Fake(builds=BUILDS_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    cached = PipelineList(
        pipelines=tuple(api.parse_pipelines(DEFINITIONS_PAYLOAD)), total=3
    )
    snapshot = azdo.fetch_snapshot(_project(), limit=50, pipelines=cached)
    assert len(snapshot.pipelines) == 3
    assert not any(
        "resource" in argv and "definitions" in argv for argv in fake.calls
    )


def test_fetch_snapshot_reports_that_more_runs_exist(monkeypatch) -> None:
    """Azure DevOps reports no total, so "is there more?" is answered by the presence
    of a continuation token — which is why the bar says "more available", not
    "N of M"."""
    fake = _Fake(builds=BUILDS_PAYLOAD, definitions=DEFINITIONS_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    # A limit of 3 is filled by page one, and the token says there is more behind it.
    snapshot = azdo.fetch_snapshot(_project(), limit=3)
    assert snapshot.runs_more is True

    no_token = {**BUILDS_PAYLOAD}
    no_token.pop("continuation_token")
    fake = _Fake(builds=no_token, definitions=DEFINITIONS_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    assert azdo.fetch_snapshot(_project(), limit=50).runs_more is False


def test_run_paging_follows_continuation_tokens(monkeypatch) -> None:
    fake = _Fake(builds=BUILDS_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    runs, calls, more = azdo._walk_runs(_project(), limit=api.MAX_TOP * 3, states=())
    # Every page carries a token, so the walk stops at its own ceiling and says so.
    assert calls == azdo.MAX_RUN_PAGES
    assert more is True
    tokens = [
        arg for argv in fake.calls for arg in argv if arg.startswith("continuationToken=")
    ]
    assert tokens and all(
        token == "continuationToken=2026-07-29T19:10:29.3867916Z" for token in tokens
    )
    assert len(runs) == 3 * azdo.MAX_RUN_PAGES


def test_fetch_log_of_a_record_with_no_log_returns_an_empty_one(monkeypatch) -> None:
    """A stage is a perfectly reasonable thing to have the cursor on; the pane says
    "no log", which is the truth, rather than the fetch raising."""
    fake = _Fake(LOG_PAYLOAD)
    monkeypatch.setattr(azdo, "_run", fake)
    log = azdo.fetch_log(_project(), Run(id=1), Record(id="s", type="Stage"))
    assert log.content == ""
    assert fake.calls == []  # nothing was fetched at all


def test_fetch_log_bounds_a_pathological_log(monkeypatch) -> None:
    """The body arrives whole — there is no range parameter the CLI can pass — so the
    only defence is to stop *holding* one."""
    huge = {"value": ["x" * 1000] * 5000}
    monkeypatch.setattr(azdo, "_run", _Fake(huge))
    monkeypatch.setattr(azdo, "MAX_LOG_CHARS", 5_000)
    log = azdo.fetch_log(_project(), Run(id=1), Record(id="t", log_id=58))
    assert log.truncated is True
    assert len(log.content) <= 5_000
    assert not log.content.endswith("x" * 1000 + "\n")  # cut on a line boundary


def test_perform_routes_each_action_to_its_resource(monkeypatch) -> None:
    fake = _Fake({"id": 205400, "buildNumber": "20260730.10"}, {}, {})
    monkeypatch.setattr(azdo, "_run", fake)
    project = _project()

    line = azdo.perform(project, Action(kind="queue", pipeline_id=36, pipeline_name="p"))
    assert "queued run 20260730.10" in line
    assert "--in-file" in fake.calls[0]  # the body travels in a file, not argv
    assert "-X" not in fake.calls[0]
    assert fake.calls[0][fake.calls[0].index("--http-method") + 1] == "POST"

    line = azdo.perform(project, Action(kind="cancel", run_id=205362))
    assert "requested" in line
    assert fake.calls[1][fake.calls[1].index("--http-method") + 1] == "PATCH"
    assert "buildId=205362" in fake.calls[1]

    azdo.perform(project, Action(kind="retry_stage", run_id=205362, stage_name="Plan"))
    assert "stages" in fake.calls[2]
    assert "stageRefName=Plan" in fake.calls[2]


def test_resolve_project_by_id_name_or_substring() -> None:
    projects = api.parse_projects(PROJECTS_PAYLOAD, "example-org")
    assert azdo.resolve_project(projects, None).name == "Archive"  # first, name-ordered
    assert azdo.resolve_project(projects, "Main").name == "Main"
    assert azdo.resolve_project(projects, "main").name == "Main"
    assert azdo.resolve_project(projects, "00000000-1111-2222-3333-444444444444").name == "Main"
    assert azdo.resolve_project(projects, "Arch").name == "Archive"


def test_resolve_project_says_what_the_options_were() -> None:
    projects = api.parse_projects(PROJECTS_PAYLOAD, "example-org")
    for wanted, expected in (("nope", "No project matches"), (None, None)):
        if expected is None:
            continue
        try:
            azdo.resolve_project(projects, wanted)
        except AzdoError as exc:
            assert expected in str(exc)
            assert "Main" in str(exc) and "Archive" in str(exc)
        else:
            raise AssertionError("a typo should be reported, not ignored")
    try:
        azdo.resolve_project([], None)
    except AzdoError as exc:
        assert "az devops login" in str(exc)
    else:
        raise AssertionError("an empty org should be reported")


def test_run_bundle_gathers_failures_and_jobs_not_every_task(monkeypatch) -> None:
    """A report an agent has to read should not be four megabytes of successful
    `apt-get` output — but a job's log covers its tasks in one fetch."""
    monkeypatch.setattr(azdo, "_run", _Fake(TIMELINE_PAYLOAD, LOG_PAYLOAD))
    bundle = azdo.fetch_run_bundle(_project(), Run(id=205362, build_number="20260730.9"))
    gathered = {entry.record.name for entry in bundle.logs}
    assert "Run > terraform show" in gathered  # failed
    assert "PublishPipelineArtifact" in gathered  # failed
    assert "Terraform > init & plan" in gathered  # a Job
    assert "Run > terraform init" not in gathered  # a task that simply passed
    assert bundle.skipped == ()


def test_run_bundle_reports_a_log_it_could_not_fetch(monkeypatch) -> None:
    """A failed fetch is a fact worth reporting, not grounds to abandon the bundle —
    the run may be worth summarizing precisely because one step is misbehaving."""
    responses: list[object] = [TIMELINE_PAYLOAD] + [
        AzdoError("`az` failed: 500 server error")
    ]
    monkeypatch.setattr(azdo, "_run", _Fake(*responses))
    bundle = azdo.fetch_run_bundle(_project(), Run(id=1))
    assert bundle.logs
    assert all(entry.error is not None for entry in bundle.logs)
    assert all("500" in (entry.error or "") for entry in bundle.logs)


def test_run_bundle_names_what_it_skipped(monkeypatch) -> None:
    monkeypatch.setattr(azdo, "_run", _Fake(TIMELINE_PAYLOAD, LOG_PAYLOAD))
    bundle = azdo.fetch_run_bundle(_project(), Run(id=1), max_logs=1)
    assert len(bundle.logs) == 1
    assert len(bundle.skipped) == 3
    assert all(" " in name for name in bundle.skipped)  # "<Type> <name>"


def test_json_parse_failure_stays_inside_the_boundary(monkeypatch) -> None:
    def bad(_args: list[str], **_: object) -> str:
        return "<html>gateway timeout</html>"

    monkeypatch.setattr(azdo, "_run", bad)
    try:
        azdo.invoke("example-org", "build", "builds")
    except AzdoError as exc:
        assert "not JSON" in str(exc)
    else:
        raise AssertionError("a non-JSON body should raise a typed transport error")


def test_a_null_body_is_a_success_with_nothing_in_it(monkeypatch) -> None:
    """`az devops invoke` prints that for a 204, which is what a cancel returns."""
    monkeypatch.setattr(azdo, "_run", lambda *_a, **_k: "null")
    assert azdo.invoke("example-org", "build", "builds") == {}


# --- rendering ---------------------------------------------------------------


def test_run_row_shows_the_number_the_description_and_the_branch_once() -> None:
    run = api.parse_runs(BUILDS_PAYLOAD)[2]
    cells = [cell.plain for cell in ui.run_row(run, NOW)]
    assert "op-infra-tf-client-workflows" in cells[1]
    assert cells[2].startswith("20260730.5")
    assert "Release 2026-07-30 #1" in cells[2]
    assert cells[3] == "✔ succeeded"
    assert cells[4] == "PR"
    assert cells[5] == "PR 1567"


def test_attention_cell_marks_failure_partial_and_flight() -> None:
    assert ui.attention_cell(Run(id=1, status="completed", result="failed")).plain == "●"
    partial = ui.attention_cell(Run(id=2, status="completed", result="partiallySucceeded"))
    assert partial.plain == "●"
    assert ui.attention_cell(Run(id=3, status="inProgress")).plain == "●"
    assert ui.attention_cell(Run(id=4, status="completed", result="succeeded")).plain == " "
    # The star has to survive the run finishing, or a watched run that passed would
    # look like one that was never marked.
    done = Run(id=5, status="completed", result="succeeded")
    assert "★" in ui.attention_cell(done, watched=True).plain


def test_record_row_carries_the_tree_prefix_and_the_error_count() -> None:
    rows = order_records(api.parse_timeline(TIMELINE_PAYLOAD))
    show = next(row for row in rows if row.record.name == "Run > terraform show")
    cells = [cell.plain for cell in ui.record_row(show, NOW)]
    assert cells[1] == "●"  # the failure marker
    assert cells[2].startswith("      ├─ Run > terraform show")
    assert "⚠ 1" in cells[2]
    assert cells[3] == "✖ failed"
    assert cells[4] == "Task"
    assert cells[5] == "ci-prod-2-2"


def test_record_row_labels_a_retry_attempt() -> None:
    row = order_records([Record(id="a", name="Lint", attempt=2)])[0]
    assert "attempt 2" in ui.record_row(row, NOW)[2].plain


def test_pipeline_row_matches_the_recent_tab() -> None:
    pipelines = api.parse_pipelines(DEFINITIONS_PAYLOAD)
    cells = [cell.plain for cell in ui.pipeline_row(pipelines[0], NOW, live=2)]
    assert cells[1] == "op-infra-tf-pi"
    assert cells[2] == "● 2"
    assert cells[3].startswith("20260730.9")
    assert cells[4] == "✖ failed"
    assert cells[6] == "infra"


def test_pipeline_row_labels_paused_and_never_run() -> None:
    pipelines = api.parse_pipelines(DEFINITIONS_PAYLOAD)
    paused = [cell.plain for cell in ui.pipeline_row(pipelines[1], NOW)]
    assert "paused" in paused[1]
    never = [cell.plain for cell in ui.pipeline_row(pipelines[2], NOW)]
    assert "disabled" in never[1]
    assert never[3] == "never run"


def test_pipeline_row_prefers_the_reconciled_last_run() -> None:
    pipeline = api.parse_pipelines(DEFINITIONS_PAYLOAD)[0]
    fresher = Run(id=9, build_number="20260730.99", status="inProgress")
    cells = [cell.plain for cell in ui.pipeline_row(pipeline, NOW, fresher)]
    assert cells[3].startswith("20260730.99")
    assert cells[4] == "● running"


def test_summary_bar_counts_and_says_more_is_available() -> None:
    bar = _plain(ui.render_summary(_snapshot(runs_more=True), None))
    assert "example-org / Main" in bar
    assert "3 runs" in bar
    assert "more available" in bar
    assert "1 failed" in bar
    assert "1 running" in bar


def test_summary_bar_never_claims_more_than_is_on_screen() -> None:
    """A list emptied by a filter must not read as a project with nothing in it."""
    bar = _plain(ui.render_summary(_snapshot(), None, shown=1))
    assert "1 runs" in bar


def test_summary_bar_names_an_active_state_filter() -> None:
    bar = _plain(ui.render_summary(_snapshot(), None, state_filter="failed"))
    assert "failed only" in bar
    assert "R cycles" in bar


def test_summary_bar_counts_stopped_pipelines_and_says_they_are_hidden() -> None:
    """The count never goes away: rows silently absent is exactly the failure mode
    this bar exists to prevent."""
    bar = _plain(
        ui.render_summary(_snapshot(), None, view="pipelines", hidden_stopped=True)
    )
    assert "2 paused/disabled" in bar
    assert "s shows" in bar


def test_summary_bar_counts_watched_runs_outside_the_window() -> None:
    """The one way a row can be absent from the Watched view without being
    unwatched."""
    runs = tuple(api.parse_runs(BUILDS_PAYLOAD))
    bar = _plain(
        ui.render_summary(
            _snapshot(),
            None,
            view="watched",
            watched_runs=runs[:1],
            watched_total=4,
        )
    )
    assert "3 outside the loaded runs" in bar


def test_summary_bar_leads_with_an_error_and_drops_the_counts() -> None:
    bar = _plain(ui.render_summary(_snapshot(), "Azure DevOps rate limit hit"))
    assert "rate limit" in bar
    assert "failed" not in bar


def test_summary_bar_before_the_first_poll() -> None:
    assert "Contacting Azure DevOps" in _plain(ui.render_summary(None, None))


def test_detail_pane_maps_view_and_level() -> None:
    snapshot = _snapshot()
    run = snapshot.runs[0]
    records = api.parse_timeline(TIMELINE_PAYLOAD)
    rows = tuple(order_records(records))

    at_runs = _plain(ui.render_detail(Drill(), snapshot, run, None, NOW))
    assert run.pipeline_name in at_runs
    assert "enter → stages, jobs and tasks" in at_runs

    record = next(r for r in records if r.name == "Run > terraform show")
    drill = Drill(level="records", run=run, records=tuple(records), rows=rows)
    at_records = _plain(ui.render_detail(drill, snapshot, run, record, NOW))
    assert "Run > terraform show" in at_records
    assert "Bash exited with code" in at_records  # issues hoisted out of the log
    assert "line 25" in at_records

    log = api.parse_log(LOG_PAYLOAD, 58)
    at_log = _plain(
        ui.render_detail(
            Drill(level="log", run=run, record=record, records=tuple(records), log=log),
            snapshot,
            run,
            record,
            NOW,
        )
    )
    assert "Starting: Run > terraform show" in at_log
    assert "end of log" in at_log

    pipeline = snapshot.pipelines[0]
    at_pipeline = _plain(
        ui.render_detail(Drill(), snapshot, None, None, NOW, view="pipelines", pipeline=pipeline)
    )
    assert pipeline.name in at_pipeline
    assert "t queue a run" in at_pipeline


def test_detail_pane_distinguishes_a_filtered_empty_list_from_an_empty_project() -> None:
    snapshot = _snapshot()
    filtered = _plain(ui.render_detail(Drill(), snapshot, None, None, NOW, shown=0))
    assert "No runs match the current filter" in filtered
    unwatched = _plain(
        ui.render_detail(Drill(), snapshot, None, None, NOW, view="watched", shown=0)
    )
    assert "Nothing watched" in unwatched
    assert "w on a run marks it" in unwatched


def test_detail_pane_separates_a_drill_failure_from_a_poll_failure() -> None:
    """The list is still good; only this one fetch is not."""
    body = _plain(
        ui.render_detail(
            Drill(level="records", error="Not found."), _snapshot(), None, None, NOW
        )
    )
    assert "Could not load" in body
    assert "Not found." in body


def test_log_pane_shows_the_tail_of_a_long_log_not_its_head() -> None:
    """A pipeline log explains its failure in its last hundred lines; showing the
    first four thousand of a fifty-thousand line log is showing the agent's setup."""
    content = "\n".join(f"line {n}" for n in range(1, ui.MAX_LOG_LINES + 501))
    log = RunLog(content=content, log_id=1, line_count=content.count("\n") + 1)
    body = _plain(ui.render_log(Record(id="t", name="Test", log_id=1), log))
    assert f"line {ui.MAX_LOG_LINES + 500}" in body  # the end is shown
    assert "line 1\n" not in body  # the beginning is not
    assert "first 500 lines not shown" in body


def test_log_pane_says_when_the_fetch_itself_was_truncated() -> None:
    log = RunLog(content="a\nb", log_id=1, line_count=2, truncated=True)
    body = _plain(ui.render_log(Record(id="t", name="T", log_id=1), log))
    assert "too large to hold in full" in body


def test_log_pane_states_when_a_record_has_no_log() -> None:
    body = _plain(ui.render_log(Record(id="s", name="Plan", type="Stage"), None))
    assert "has no log of its own" in body
    assert "open a job or task inside it" in body


def test_log_pane_reports_a_filter_that_matches_nothing() -> None:
    log = api.parse_log(LOG_PAYLOAD, 58)
    body = _plain(ui.render_log(Record(id="t", name="T", log_id=58), log, query="zzz"))
    assert "No log line matches" in body


def test_log_pane_reports_an_empty_log() -> None:
    body = _plain(
        ui.render_log(Record(id="t", name="T", log_id=58), RunLog(content="  ", log_id=58))
    )
    assert "empty log" in body


def test_issue_overlay_lists_errors_with_their_step_and_line() -> None:
    records = tuple(api.parse_timeline(TIMELINE_PAYLOAD))
    run = Run(id=205362, build_number="20260730.9", pipeline_name="op-infra-tf-pi")
    body = _plain(ui.render_issues(run, records))
    assert "2 errors, 1 warning" in body
    assert "Run > terraform show:25" in body
    assert "terraform.tfplan" in body


def test_issue_overlay_before_a_drill_and_with_nothing_wrong() -> None:
    assert "Drill into a run" in _plain(ui.render_issues(None, ()))
    clean = _plain(ui.render_issues(Run(id=1, build_number="20260730.1"), ()))
    assert "No errors or warnings" in clean


def test_project_switcher_marks_the_active_project() -> None:
    projects = tuple(api.parse_projects(PROJECTS_PAYLOAD, "example-org"))
    body = _plain(ui.render_projects(projects, projects[1].key))
    assert "Main" in body and "Archive" in body
    assert "1-9 to switch" in body
    assert "No projects visible" in _plain(ui.render_projects((), ""))


def test_confirm_modal_names_the_target_and_admits_there_is_no_dry_run() -> None:
    body = _plain(
        ui.render_confirm(
            Action(kind="cancel", run_id=205362, pipeline_name="op-infra-tf-pi")
        )
    )
    assert "Cancel this run" in body
    assert "op-infra-tf-pi" in body
    assert "run 205362" in body
    assert "This changes state in Azure DevOps." in body
    assert "y / enter confirm" in body


def test_render_once_prints_both_lists() -> None:
    runs = _plain(ui.render_once(_snapshot(), NOW))
    assert "op-infra-tf-pi" in runs
    assert "az calls in" in runs
    pipelines = _plain(ui.render_once(_snapshot(), NOW, "pipelines"))
    assert "never-run" in pipelines


def test_help_and_menu_stay_in_step_with_the_bindings() -> None:
    """The menu bar is the complete map, so every entry must name an action the app
    actually has — a renamed action would otherwise become a silent no-op."""
    actions = {
        entry.action
        for category in ui.menu_categories()
        for entry in category.entries
    }
    for action in actions:
        assert hasattr(AzdoWatchApp, f"action_{action}"), action
    body = _plain(ui.render_help())
    for key in ("enter", "escape", "E", "< / >", "P", "i", "o"):
        assert key in body


def test_menu_toggles_label_where_they_are_going() -> None:
    """The menu must never point the wrong direction."""
    hidden = ui.menu_categories(chart_shown=False, stopped_shown=False)
    labels = {e.action: e.label for c in hidden for e in c.entries}
    assert labels["toggle_chart"] == "Show the charts"
    assert labels["toggle_stopped"] == "Show paused / disabled pipelines"

    shown = ui.menu_categories(chart_shown=True, stopped_shown=True)
    labels = {e.action: e.label for c in shown for e in c.entries}
    assert labels["toggle_chart"] == "Hide the charts"
    assert labels["toggle_stopped"] == "Hide paused / disabled pipelines"


def test_state_filter_menu_label_names_the_next_state() -> None:
    labels = [ui._state_filter_menu_label(state) for state in ui.STATE_FILTERS]
    assert labels[0] == "Show only running runs / pipelines"
    assert "running → failed" in labels[1]
    assert labels[-1] == "Show all runs and pipelines"


def test_activity_log_is_newest_first_and_marks_actions() -> None:
    from tools.azdo_watch.models import LogEntry

    entries = [
        LogEntry(time=datetime(2026, 7, 30, 17, 0), level="info", message="polled"),
        LogEntry(time=datetime(2026, 7, 30, 17, 1), level="action", message="cancelled"),
    ]
    body = _plain(ui.render_activity_log(entries))
    assert body.index("cancelled") < body.index("polled")
    assert "⚡" in body
    assert "No activity yet" in _plain(ui.render_activity_log([]))


# --- the charts --------------------------------------------------------------


def test_chart_group_buckets_partial_success_with_failure() -> None:
    """On a chart whose job is "where should I look", a run that failed some of its
    jobs belongs with the red."""
    assert ui.chart_group("partiallySucceeded") == "failed"
    assert ui.chart_group("abandoned") == "failed"
    assert ui.chart_group("succeededWithIssues") == "succeeded"
    assert ui.chart_group("running") == "running"
    assert ui.chart_group("teleporting") == "other"  # total, like state_style


def test_chart_counts_bucket_over_the_window() -> None:
    points = [
        (NOW - timedelta(hours=2), "succeeded"),
        (NOW - timedelta(hours=2), "failed"),
        (NOW, "running"),
    ]
    counts = ui.chart_counts(points, NOW, 4)
    assert len(counts) == 4
    assert counts[0] == {"succeeded": 1, "failed": 1}
    assert counts[-1] == {"running": 1}
    # Undated points have nowhere on a time axis to go.
    assert ui.chart_counts([(None, "failed")], NOW, 4) == [{}, {}, {}, {}]


def test_stack_cells_never_rounds_a_failure_away() -> None:
    """A bucket of 19 successes and 1 failure must still show red — hiding it would
    hide exactly what a monitoring chart exists to show."""
    cells = ui.stack_cells({"succeeded": 19, "failed": 1}, ui.CHART_BAR_ROWS)
    assert cells[0] == "failed"
    assert len(cells) == ui.CHART_BAR_ROWS
    assert ui.stack_cells({}, 5) == ()
    assert ui.stack_cells({"failed": 1}, 0) == ()


def test_in_flight_counts_run_an_unfinished_span_to_now() -> None:
    spans = [
        (NOW - timedelta(minutes=10), NOW - timedelta(minutes=5)),
        (NOW - timedelta(minutes=6), None),  # still going
    ]
    counts = ui.in_flight_counts(spans, NOW, 10)
    assert max(counts) == 2
    assert counts[-1] == 1  # only the unfinished one reaches the last bucket
    # A span that never started was never in flight.
    assert ui.in_flight_counts([(None, None)], NOW, 4) == [0, 0, 0, 0]


def test_charts_follow_the_drill_and_report_an_empty_axis() -> None:
    snapshot = _snapshot()
    records = tuple(api.parse_timeline(TIMELINE_PAYLOAD))
    at_runs = _plain(ui.render_chart(Drill(), snapshot.runs, NOW))
    assert "Runs over time" in at_runs

    drill = Drill(level="records", run=snapshot.runs[0], records=records)
    inside = _plain(ui.render_chart(drill, snapshot.runs, NOW))
    assert "Steps over time" in inside
    assert _plain(ui.render_in_flight_chart(drill, snapshot.runs, NOW)).count("Steps in flight")

    empty = _plain(ui.render_chart(Drill(), (Run(id=1),), NOW))
    assert "No dated runs to graph" in empty
    assert "No run has started yet" in _plain(
        ui.render_in_flight_chart(Drill(), (Run(id=1),), NOW)
    )


def test_chart_body_draws_bars_and_a_time_axis() -> None:
    body = ui.ChartBody(
        points=((NOW - timedelta(hours=1), "failed"), (NOW, "succeeded")), now=NOW
    )
    drawn = _plain(body)
    assert "█" in drawn
    assert "now" in drawn
    assert "┤" in drawn
    # Too narrow to draw anything rather than clipping into nonsense.
    narrow = Console(width=6)
    with narrow.capture() as capture:
        narrow.print(body)
    assert "█" not in capture.get()


# --- layout state ------------------------------------------------------------


def test_layout_round_trips(tmp_path) -> None:
    path = tmp_path / "layout.json"
    saved = layout.Layout(detail_mode="below", split=30, project="00000000", chart=False)
    layout.save(saved, path)
    assert layout.load(path) == saved


def test_layout_shrugs_off_a_broken_state_file(tmp_path) -> None:
    """A broken state file must never take the dashboard down — it just means default
    layout."""
    path = tmp_path / "layout.json"
    path.write_text("{not json")
    assert layout.load(path) == layout.Layout()
    assert layout.load(tmp_path / "absent.json") == layout.Layout()
    assert layout.from_dict([1, 2, 3]) == layout.Layout()
    assert layout.from_dict({"detail_mode": "sideways", "split": "wide"}) == layout.Layout()
    assert layout.from_dict({"split": True}).split == layout.SPLIT_DEFAULT


def test_layout_save_to_an_unwritable_path_is_silent(tmp_path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    layout.save(layout.Layout(), blocked / "layout.json")  # must not raise


def test_split_is_clamped() -> None:
    assert layout.clamp_split(0) == layout.SPLIT_MIN
    assert layout.clamp_split(999) == layout.SPLIT_MAX
    assert layout.clamp_split(50) == 50


def test_layout_state_path_is_tool_specific(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/cfg")
    assert layout.state_path() == pathlib.Path("/tmp/cfg/azdo-watch/layout.json")


# --- the CLI -----------------------------------------------------------------


def test_cli_defaults() -> None:
    args = _parse_args([])
    assert args.interval == 60
    assert args.limit == DEFAULT_RUN_LIMIT
    assert args.view == "runs"
    assert args.once is False
    assert args.org is None and args.project is None
    assert _states(args) == ()


def test_cli_parses_repeatable_states() -> None:
    args = _parse_args(["--state", "inProgress", "--state", "notStarted"])
    assert _states(args) == ("inProgress", "notStarted")
    assert _states(_parse_args(["--state", ""])) == ()


def test_cli_once_cannot_ask_for_the_watched_view() -> None:
    """The watch list is session state inside the live app, so a one-shot snapshot
    has nothing to show."""
    try:
        _parse_args(["--view", "watched"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--view watched should be a usage error")


def test_pipeline_cache_expires_and_can_be_dropped() -> None:
    cache = _PipelineCache(ttl=10)
    listing = PipelineList(pipelines=(Pipeline(id=1, name="p"),), total=1)
    cache.put("proj", listing, now=100.0)
    assert cache.get("proj", now=105.0) is listing
    assert cache.get("proj", now=120.0) is None  # past the TTL
    cache.put("proj", listing, now=200.0)
    cache.drop("proj")
    assert cache.get("proj", now=200.0) is None


def test_min_interval_is_a_floor() -> None:
    """Every call starts a Python interpreter and loads an extension against an API
    humans are also using, so restraint is ours to impose."""
    assert MIN_INTERVAL >= 20
    assert max(MIN_INTERVAL, _parse_args(["--interval", "1"]).interval) == MIN_INTERVAL


# --- the app -----------------------------------------------------------------


def _investigation(name: str = "azdo-test") -> investigate.Investigation:
    return investigate.Investigation(
        name=name,
        prompt="read the report and summarize",
        path=pathlib.Path(f"/tmp/{name}.md"),
        steps=8,
        logs=3,
        calls=4,
        elapsed=3.0,
    )


class _TestApp(AzdoWatchApp):
    """The app with `open_url` recorded rather than launched.

    Subclassed instead of patched onto the instance: `o` and a clicked link both go
    through `App.open_url`, which would otherwise open a real browser out of the
    test run.
    """

    opened: list[str]

    def open_url(self, url: str, *, new_tab: bool = True) -> None:
        self.opened.append(url)


def _app(
    polls: list[tuple[Snapshot | None, PollError | None]] | None = None,
    *,
    records: list[Record] | None = None,
    timeline_error: PollError | None = None,
    log: RunLog | None = None,
    log_error: PollError | None = None,
    performed: list[Action] | None = None,
    perform_error: PollError | None = None,
    layout_path=None,
    interval: int = 60,
    requested: list[PollRequest] | None = None,
    prepared: list | None = None,
    prepare_error: PollError | None = None,
    launched: list | None = None,
    launch_error: str | None = None,
    opened: list[str] | None = None,
) -> AzdoWatchApp:
    """An app wired to fakes: one poll queue plus the drill-down seams."""
    queue = list(polls) if polls else [(_snapshot(), None)]
    resolved = records if records is not None else api.parse_timeline(TIMELINE_PAYLOAD)
    fired = performed if performed is not None else []
    asked = requested if requested is not None else []
    gathered = prepared if prepared is not None else []
    handed_off = launched if launched is not None else []
    urls = opened if opened is not None else []

    def poll(request):
        asked.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def fetch_timeline(_project, _run):
        if timeline_error is not None:
            return None, timeline_error
        return (
            azdo.RunTimeline(
                records=tuple(resolved),
                rows=tuple(order_records(list(resolved))),
                calls=1,
            ),
            None,
        )

    def fetch_log(_project, _run, record):
        if log_error is not None:
            return None, log_error
        return (
            log or api.parse_log(LOG_PAYLOAD, record.log_id or -1),
            None,
        )

    def perform(_project, action):
        if perform_error is not None:
            return None, perform_error
        fired.append(action)
        return f"{action.summary} — ok", None

    def prepare(_project, run, pipeline):
        if prepare_error is not None:
            return None, prepare_error
        gathered.append((run, pipeline))
        return _investigation(), None

    def launch(inv):
        if launch_error is not None:
            return None, launch_error
        handed_off.append(inv)
        return f"gw: opened scratch {inv.name}.", None

    app = _TestApp(
        poll=poll,
        interval=interval,
        fetch_timeline=fetch_timeline,
        fetch_log=fetch_log,
        perform=perform,
        investigate=prepare,
        launch=launch,
        layout_path=layout_path,
    )
    app.opened = urls
    return app


async def test_app_lists_runs_and_shows_the_selected_one() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        assert table.row_count == 3
        assert app._selected_key == "205369"  # newest first
        detail = _widget_text(app.query_one("#detail", Static))
        assert "op-infra-tf-client-workflows" in detail
        assert "example-org / Main" in _widget_text(app.query_one("#summary", Static))

        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key == "205365"
        assert "etl-service" in _widget_text(app.query_one("#detail", Static))


async def test_app_drills_run_to_timeline_to_log_and_back() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "records"
        assert app.query_one(DataTable).row_count == 8
        # azdo marks the stage, the phase, the job and the task all failed; only the
        # task says what actually went wrong, so that is where the cursor lands — and
        # it is the row whose log `enter` can then open.
        landed = app._selected_record()
        assert landed is not None
        assert (landed.name, landed.type) == ("Run > terraform show", "Task")

        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "log"
        assert "Starting: Run > terraform show" in _widget_text(
            app.query_one("#detail", Static)
        )

        await pilot.press("escape")
        await pilot.pause()
        assert app._drill.level == "records"
        await pilot.press("escape")
        await pilot.pause()
        assert app._drill.level == "runs"
        assert app.query_one(DataTable).row_count == 3


async def test_app_moving_the_cursor_in_the_log_view_follows_the_step() -> None:
    """Keeping the record list on screen while reading a log is only worth it if the
    cursor takes the log with it."""
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        first = app._drill.record
        await pilot.press("down")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._drill.level == "log"
        assert app._drill.record is not None and app._drill.record != first


async def test_app_jumps_between_failed_steps() -> None:
    """`<`/`>` are the CI form of "show me the thing that broke": a long timeline can
    bury the one failure that matters between fifty green tasks."""
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        # azdo marks the whole chain failed: the stage, its phase, its job, and the
        # two tasks that actually broke.
        failures = [row.record.key for row in app.visible_rows() if row.record.failed]
        assert failures == [
            "stage-plan",
            "phase-plan",
            "job-plan",
            "task-show",
            "task-publish",
        ]
        seen = []
        for _ in range(len(failures) + 1):
            seen.append(app._record_key)
            await pilot.press(">")
            await pilot.pause()
        assert set(seen) <= set(failures)
        # It wraps, so `>` keeps cycling the failures instead of stopping.
        assert app._record_key in failures


async def test_app_e_lists_the_runs_recorded_issues() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, IssueScreen)
        body = _widget_text(app.screen.query_one("#issues-content", Static))
        assert "Bash exited with code" in body
        assert "Run > terraform show:25" in body
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, IssueScreen)


async def test_app_E_filters_the_log_to_errors() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("E")
        await pilot.pause()
        assert app._queries["log"] == LOG_ERROR_QUERY
        body = _widget_text(app.query_one("#detail", Static))
        assert "##[error]Bash exited" in body
        assert "Task         : Bash" not in body  # the rest is filtered out

        # A preset, not a mode: `esc` clears it like any other query.
        await pilot.press("escape")
        await pilot.pause()
        assert app._queries["log"] == ""


async def test_app_filter_narrows_the_list_and_the_bar_says_so() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        for key in "infra":
            await pilot.press(key)
        await pilot.pause()
        assert app._queries["runs"] == "infra"
        assert app.query_one(DataTable).row_count == 2
        assert "/infra" in app._hint()

        await pilot.press("escape")
        await pilot.pause()
        assert app._queries["runs"] == ""
        assert app.query_one(DataTable).row_count == 3


async def test_app_each_list_keeps_its_own_filter() -> None:
    """Narrowing the runs list and then narrowing a log must not fight each other."""
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("slash")
        for key in "infra":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("v")  # to Pipelines
        await pilot.pause()
        assert app._queries["pipelines"] == ""
        assert app._queries["runs"] == "infra"


async def test_app_state_filter_cycles_over_both_views() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("R")
        await pilot.pause()
        assert app._state_filter == "running"
        assert app.query_one(DataTable).row_count == 1
        assert "running only" in _widget_text(app.query_one("#summary", Static))

        await pilot.press("v")  # the same narrowing applies to Pipelines
        await pilot.pause()
        assert [p.name for p in app.visible_pipelines()] == []  # none ran in-flight
        # …except the pipeline whose in-window run is running.
        await pilot.press("R")
        await pilot.pause()
        assert app._state_filter == "failed"
        assert [p.name for p in app.visible_pipelines()] == ["op-infra-tf-pi"]


async def test_app_hides_stopped_pipelines_until_s() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert [p.name for p in app.visible_pipelines()] == ["op-infra-tf-pi"]
        summary = _widget_text(app.query_one("#summary", Static))
        assert "2 paused/disabled" in summary and "s shows" in summary

        await pilot.press("s")
        await pilot.pause()
        assert len(app.visible_pipelines()) == 3


async def test_app_enter_on_a_pipeline_jumps_to_its_runs() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app._view == "runs"
        assert app._queries["runs"] == "op-infra-tf-pi"
        assert app.query_one(DataTable).row_count == 1


async def test_app_watch_list_is_its_own_view() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert app._watched == {"205369"}
        assert "★" in _plain(app.query_one(DataTable).get_row_at(0)[0])

        await pilot.press("v")  # Pipelines
        await pilot.press("v")  # Watched
        await pilot.pause()
        assert app._view == "watched"
        assert app.query_one(DataTable).row_count == 1

        await pilot.press("W")
        await pilot.pause()
        assert app._watched == set()
        assert "Nothing watched" in _widget_text(app.query_one("#detail", Static))


async def test_app_scrolling_near_the_bottom_loads_older_runs() -> None:
    """The runs list is not a fixed window: moving the cursor near its bottom asks
    the next poll for a wider window, and every later refresh keeps it."""
    requested: list[PollRequest] = []
    snapshot = _snapshot(runs_more=True)
    app = _app([(snapshot, None)], requested=requested)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert requested[-1].run_limit is None

        await pilot.press("down")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._wanted_runs == 3 + RUNS_EXTEND_STEP
        assert requested[-1].run_limit == 3 + RUNS_EXTEND_STEP

        await pilot.press("r")  # a manual refresh keeps the grown window
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert requested[-1].run_limit == 3 + RUNS_EXTEND_STEP


async def test_app_bottom_of_a_fully_loaded_run_list_asks_for_nothing() -> None:
    """`runs_more` false means the service has nothing more to hand over."""
    requested: list[PollRequest] = []
    app = _app([(_snapshot(runs_more=False), None)], requested=requested)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert app._wanted_runs is None
        assert all(request.run_limit is None for request in requested)


async def test_app_project_switch_drops_the_old_projects_data() -> None:
    """Showing one project's runs under another's name is the failure this prevents;
    a watched build id from the old project could only be stale or collide."""
    app = _app([(_snapshot(runs_more=True), None)])
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("w")
        await pilot.press("down")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._watched and app._wanted_runs is not None

        await pilot.press("P")
        await pilot.pause()
        assert isinstance(app.screen, ProjectScreen)
        await pilot.press("2")  # Archive; "1" is the project already open
        await pilot.pause()
        assert app._watched == set()
        assert app._wanted_runs is None
        assert app._drill.level == "runs"


async def test_app_discards_a_poll_that_landed_after_a_project_switch() -> None:
    app = _app([(_snapshot(), None)])
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._target_epoch += 1  # as a switch would
        app._apply_poll(_snapshot(), None, epoch=0)
        await pilot.pause()
        assert any(
            "Discarded a refresh" in entry.message for entry in app.activity_log
        )
        assert app._poll_again is True


async def test_app_keeps_the_last_good_list_through_a_failed_poll() -> None:
    error = PollError(message="Azure DevOps rate limit hit", kind="rate_limited")
    app = _app([(_snapshot(), None), (None, error)])
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 3  # the stale rows stay readable
        assert "rate limit" in _widget_text(app.query_one("#summary", Static))


async def test_app_backs_off_exponentially_on_a_rate_limit() -> None:
    error = PollError(message="rate limit", kind="rate_limited")
    app = _app([(None, error)], interval=60)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60
        assert app._delay_after(error) == 120
        assert app._delay_after(error) == 240
        # A successful poll resets it immediately.
        assert app._delay_after(None) == 60


async def test_app_unrecoverable_failure_stops_hammering() -> None:
    """The timer cannot install a CLI; spawning a doomed process every minute only
    hides the message."""
    error = PollError(message="`az` not found on PATH", kind="missing_cli")
    app = _app([(None, error)])
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        from tools.azdo_watch.app import MAX_BACKOFF_SECONDS

        assert app._current_delay == MAX_BACKOFF_SECONDS


async def test_app_every_mutation_goes_through_the_confirmation_gate() -> None:
    performed: list[Action] = []
    app = _app(performed=performed)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("t")  # queue
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.action_request.kind == "queue"
        await pilot.press("n")  # cancelled: nothing is sent
        await pilot.pause()
        assert performed == []
        assert any("Cancelled:" in e.message for e in app.activity_log)

        await pilot.press("t")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [action.kind for action in performed] == ["queue"]
        assert any(e.level == "action" for e in app.activity_log)


async def test_app_queue_reuses_the_selected_runs_branch_but_not_a_pr_ref() -> None:
    """`refs/pull/N/merge` is the merge commit azdo built, not a branch anyone can
    queue against."""
    performed: list[Action] = []
    app = _app(performed=performed)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Row 0 is the PR build.
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.action_request.branch == ""
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("down")  # the develop CI build
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.action_request.branch == "refs/heads/main"


async def test_app_refuses_to_cancel_a_finished_run() -> None:
    """The service answers a cancel of a finished build with an error; a modal that
    offers an action which cannot work is worse than one that explains why."""
    performed: list[Action] = []
    app = _app(performed=performed)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("c")  # row 0 succeeded
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert performed == []

        await pilot.press("down")  # the inProgress build
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.action_request.kind == "cancel"
        assert app.screen.action_request.run_id == 205365


async def test_app_retry_stage_needs_a_stage_row() -> None:
    performed: list[Action] = []
    app = _app(performed=performed)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The cursor landed on a failed *task*, which cannot be retried alone.
        selected = app._selected_record()
        assert selected is not None and selected.type == "Task"
        await pilot.press("Y")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)

        # Move to the stage row and it is offered.
        stage_row = next(
            index
            for index, row in enumerate(app.visible_rows())
            if row.record.type == "Stage" and row.record.failed
        )
        app.query_one(DataTable).move_cursor(row=stage_row)
        await pilot.pause()
        await pilot.press("Y")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert app.screen.action_request.kind == "retry_stage"
        # The route takes the stage's refName, not the name shown in the tree.
        assert app.screen.action_request.stage_name == "planTerraform"


async def test_app_a_failed_action_is_logged_not_swallowed() -> None:
    error = PollError(message="TF400813: not authorized", kind="forbidden")
    app = _app(perform_error=error)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any(
            e.level == "error" and "not authorized" in e.message
            for e in app.activity_log
        )


async def test_app_o_opens_the_run_then_the_pipeline() -> None:
    opened: list[str] = []
    app = _app(opened=opened)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert opened and "buildId=205369" in opened[-1]

        await pilot.press("v")  # Pipelines
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert "definitionId=36" in opened[-1]


async def test_app_i_hands_the_selected_run_to_gw() -> None:
    prepared: list = []
    launched: list = []
    app = _app(prepared=prepared, launched=launched)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("i")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(prepared) == 1
        run, pipeline = prepared[0]
        assert run.id == 205369
        assert len(launched) == 1
        assert any("report ready" in e.message for e in app.activity_log)
        assert any(e.level == "action" for e in app.activity_log)


async def test_app_i_reports_a_gather_that_failed() -> None:
    app = _app(prepare_error=PollError(message="404 not found", kind="not_found"))
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("i")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any(
            e.level == "error" and "404" in e.message for e in app.activity_log
        )
        assert app._investigating is False  # and the flag is released


async def test_app_drill_failure_leaves_the_list_intact() -> None:
    app = _app(timeline_error=PollError(message="404 not found", kind="not_found"))
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Could not load" in _widget_text(app.query_one("#detail", Static))
        await pilot.press("escape")
        await pilot.pause()
        assert app._drill.level == "runs"
        assert app.query_one(DataTable).row_count == 3


async def test_app_overlays_open_and_close() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        for key, screen in (
            ("question_mark", HelpScreen),
            ("l", LogScreen),
            ("P", ProjectScreen),
            ("M", DropdownScreen),
        ):
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, screen), key
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, screen), key


async def test_app_menu_runs_the_action_it_names() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("M")
        await pilot.pause()
        # `←`/`→` slide between categories the way a menu bar does everywhere else.
        await pilot.press("right")
        await pilot.pause()
        assert isinstance(app.screen, DropdownScreen)
        await pilot.press("escape")
        await pilot.pause()

        app._on_dropdown_picked(0, "switch_view")
        await pilot.pause()
        assert app._view == "pipelines"


async def test_app_layout_persists(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = _app(layout_path=path)
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")  # right -> below
        await pilot.press("left_square_bracket")
        await pilot.press("g")
        await pilot.pause()

    saved = layout.load(path)
    assert saved.detail_mode == "below"
    assert saved.split == layout.SPLIT_DEFAULT - layout.SPLIT_STEP
    assert saved.chart is False
    assert saved.project == "00000000"

    # And a fresh app starts where the last one left off.
    restored = _app(layout_path=path)
    async with restored.run_test(size=(160, 44)) as pilot:
        await restored.workers.wait_for_complete()
        await pilot.pause()
        assert restored._detail_mode == "below"
        assert restored._chart_shown is False


async def test_app_hidden_detail_pane_ignores_the_divider_keys() -> None:
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d")  # below
        await pilot.press("d")  # hidden
        await pilot.pause()
        before = app._split
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app._split == before  # one window fills the screen


async def test_app_a_second_refresh_while_one_is_running_is_remembered() -> None:
    """Dropping the request would leave the wrong answer on screen."""
    app = _app()
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._polling = True
        app.action_poll_now()
        assert app._poll_again is True


async def test_app_renders_without_a_snapshot() -> None:
    """The very first frame, before any poll has landed."""
    app = _app([(None, None)])
    async with app.run_test(size=(160, 44)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0
        assert "Contacting Azure DevOps" in _widget_text(
            app.query_one("#summary", Static)
        )


# --- the gw report ----------------------------------------------------------


def _bundle() -> azdo.RunBundle:
    records = api.parse_timeline(TIMELINE_PAYLOAD)
    rows = tuple(order_records(records))
    log = api.parse_log(LOG_PAYLOAD, 58)
    show = next(r for r in records if r.name == "Run > terraform show")
    job = next(r for r in records if r.type == "Job")
    return azdo.RunBundle(
        run=Run(id=205362, build_number="20260730.9", pipeline_name="op-infra-tf-pi"),
        records=tuple(records),
        rows=rows,
        logs=(
            azdo.RecordLog(record=show, log=log),
            azdo.RecordLog(record=job, error="404 not found"),
        ),
        skipped=("Task Stop Containers",),
        calls=4,
        elapsed=3.0,
    )


def test_report_leads_with_the_recorded_issues() -> None:
    """The answer often enough that burying it would be perverse."""
    bundle = _bundle()
    report = investigate.render_report(_project(), bundle.run, None, bundle, NOW)
    assert report.index("Issues Azure DevOps recorded") < report.index("Timeline")
    assert "log line 25" in report
    assert "Bash exited with code '1'." in report
    assert "terraform.tfplan" in report


def test_report_keeps_the_tree_shape_as_plain_text() -> None:
    bundle = _bundle()
    report = investigate.render_report(_project(), bundle.run, None, bundle, NOW)
    assert "- └─ Terraform > init & plan:" in report
    assert "-       ├─ Run > terraform show:" in report


def test_report_states_every_omission_where_it_happened() -> None:
    """The reader cannot see what was left out of a file."""
    bundle = _bundle()
    report = investigate.render_report(_project(), bundle.run, None, bundle, NOW)
    assert "Log could not be fetched: 404 not found" in report
    assert "1 logs were not fetched" in report
    assert "Task Stop Containers" in report


def test_report_bounds_each_log_to_its_tail_and_says_so() -> None:
    """Failures announce themselves at the end of a log, and an agent asked to read
    a 50 MB file summarizes nothing."""
    huge = "\n".join(f"line {n}" for n in range(200_000))
    tail, cut = investigate.log_tail(huge)
    assert cut is True
    assert len(tail) <= investigate.LOG_TAIL_CHARS
    assert tail.endswith("line 199999")
    assert not tail.startswith("line 0")  # cut on a line boundary
    short, uncut = investigate.log_tail("small")
    assert (short, uncut) == ("small", False)


def test_report_says_what_it_deliberately_left_out() -> None:
    bundle = _bundle()
    report = investigate.render_report(_project(), bundle.run, None, bundle, NOW)
    assert "individually successful tasks do not" in report


def test_report_with_no_issues_says_so_rather_than_nothing() -> None:
    bundle = azdo.RunBundle(
        run=Run(id=1, build_number="20260730.1", pipeline_name="p"),
        records=(),
        rows=(),
        logs=(),
        skipped=(),
        calls=1,
        elapsed=0.1,
    )
    report = investigate.render_report(_project(), bundle.run, None, bundle, NOW)
    assert "The timeline records no error or warning" in report
    assert "No logs were fetched." in report


def test_prompt_forbids_changes_and_points_at_the_issues_first() -> None:
    """The agent is handed a report *about* CI in a scratch space with no project
    attached; its job ends at understanding."""
    prompt = investigate.build_prompt(
        _project(), Run(id=1, build_number="20260730.1", pipeline_name="p"), pathlib.Path("/tmp/r.md")
    )
    assert "Do not make any changes of any kind" in prompt
    assert "no re-running the pipeline" in prompt
    assert "Start from the recorded issues" in prompt
    assert "stop and wait for instructions" in prompt
    assert "/tmp/r.md" in prompt


def test_scratch_name_is_readable_and_collision_proof() -> None:
    run = Run(id=1, pipeline_name="op-infra-tf-pi")
    name = investigate.scratch_name(run, NOW)
    assert name == "azdo-op-infra-tf-pi-20260730-170000"
    # Nothing a filesystem or tmux will fight.
    odd = investigate.scratch_name(Run(id=2, pipeline_name="pi/server (auto)"), NOW)
    assert re.fullmatch(r"azdo-[a-z0-9-]+-\d{8}-\d{6}", odd)


def test_scratch_command_seeds_the_prompt() -> None:
    assert investigate.scratch_command("n", "p") == ["gw", "scratch", "n", "--prompt", "p"]


def test_write_report_uses_a_recognizable_path(tmp_path) -> None:
    path = investigate.write_report("body", "azdo-x", tmp_path)
    assert path == tmp_path / investigate.REPORT_DIR / "azdo-x.md"
    assert path.read_text() == "body"


def test_prepare_is_deterministic_apart_from_the_write(tmp_path) -> None:
    bundle = _bundle()
    prepared = investigate.prepare(_project(), bundle.run, None, bundle, NOW, tmp_path)
    assert prepared.name == "azdo-op-infra-tf-pi-20260730-170000"
    assert prepared.steps == 8
    assert prepared.logs == 2
    assert prepared.calls == 4
    assert prepared.path.read_text().startswith("# Azure DevOps run report")


# --- structural guards ------------------------------------------------------


def _module_source(name: str) -> str:
    return (
        pathlib.Path(__file__).resolve().parents[1] / "tools" / "azdo_watch" / f"{name}.py"
    ).read_text()


def _imports(name: str) -> set[str]:
    """The top-level packages a module imports.

    Read from the AST rather than grepped, so a *comment* naming `subprocess` — of
    which there are several, explaining precisely why the module does not use it —
    cannot fail the test it is documenting.
    """
    tree = ast.parse(_module_source(name))
    return {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


def test_only_the_transport_module_spawns_processes() -> None:
    """`azdo.py` is the single subprocess seam, so tests can fake one function and
    know nothing reaches the network. `investigate.py` is the documented exception:
    it spawns `gw`, and nothing else."""
    for name in ("api", "app", "cli", "layout", "models", "ui"):
        assert "subprocess" not in _imports(name), name
    assert "subprocess" in _imports("azdo")
    assert "subprocess" in _imports("investigate")


def test_the_pure_layers_import_no_io() -> None:
    """`models` and `ui` are pure functions of data plus a clock, which is what makes
    every layout snapshottable and every parser directly testable."""
    for name in ("models", "ui"):
        assert not _imports(name) & {
            "subprocess",
            "socket",
            "urllib",
            "http",
            "os",
            "textual",
        }, name


def _code_strings(name: str) -> set[str]:
    """Every string literal in a module's *code* — docstrings excluded.

    The distinction matters for the guards below: `azdo.py`'s docstring names the
    credential commands it promises never to run, and a grep over the source cannot
    tell that promise from a call.
    """
    tree = ast.parse(_module_source(name))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docstrings
    }


def test_the_transport_never_asks_for_a_credential() -> None:
    """`az account get-access-token` and `az devops login` both handle live
    credentials; neither is ever invoked."""
    literals = _code_strings("azdo")
    assert "get-access-token" not in literals
    assert "login" not in literals
    # And the one place `az` subcommands are named lists only read-only ones.
    assert {"invoke", "extension", "configure"} & literals


def test_action_kinds_and_bindings_do_not_drift() -> None:
    """Every kind the models declare is reachable from a key, and every key the app
    binds resolves to a method."""
    bound = {
        action.split("(")[0]
        for _keys, action, _description in AzdoWatchApp.BINDINGS  # type: ignore[misc]
    }
    for action in bound:
        assert hasattr(AzdoWatchApp, f"action_{action}"), action
    assert {"queue_run", "cancel_run", "retry_stage"} <= bound
    assert len(ACTION_KINDS) == 3


def test_issue_is_error_reads_the_type() -> None:
    assert Issue(type="error").is_error is True
    assert Issue(type="Error").is_error is True
    assert Issue(type="warning").is_error is False
