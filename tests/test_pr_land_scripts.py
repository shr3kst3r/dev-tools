"""Tests for the pr-land skill's PR-snapshot script.

The script lives under `skills/pr-land/scripts/` rather than `tools/`, because it
ships with the skill and must run in whatever repo the skill is pointed at
(stdlib only, no venv). It is loaded here by path for the same reason.

The bot comment bodies below are verbatim captures from
example-org/etl-service#945 — the HTML chrome, tracking comments, and
badge markup are exactly what Cursor Bugbot and the Codex connector really post.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "pr-land" / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ps = _load("pr_state")


CURSOR_BODY = """### FF silver table resolution crashes

**High Severity**

<!-- DESCRIPTION START -->
`_resolve_final_silver_table` reads `FINAL_SILVER_SUFFIX` from the content-set \
class, but that attribute exists only on `VendorEvents`. Calling \
`validate_content_set_currency` without an explicit `table=` raises `AttributeError`.
<!-- DESCRIPTION END -->

<!-- BUGBOT_BUG_ID: 0fa3c9da-a229-4985-8c08-f5f23472eb55 -->

<!-- LOCATIONS START
plugins/example-org/events/vendor_currency_qa.py#L312-L326
plugins/example-org/events/vendor_currency_qa.py#L117-L132
LOCATIONS END -->
<div><a href="https://cursor.com/open?link=eyJ2ZXJ" target="_blank" \
rel="noopener noreferrer"><picture><source media="(prefers-color-scheme: dark)" \
srcset="https://cursor.com/assets/images/fix-in-cursor-dark.png"><img \
alt="Fix in Cursor" width="115" height="28" \
src="https://cursor.com/assets/images/fix-in-cursor-dark.png"></picture></a></div>


<sup>Reviewed by [Cursor Bugbot](https://cursor.com/bugbot) for commit \
756940989e82b1025e9f6fd0da4cd938d99b3b2d. Configure \
[here](https://www.cursor.com/dashboard/bugbot).</sup>
"""

CODEX_BODY = """**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)\
</sub></sub>  Resolve the Fundamentals silver suffix explicitly**

When the runbook calls `validate_content_set_currency(..., "VENDOR-FUNDAMENTALS")` \
without overriding `table`, this dereference raises `AttributeError` because \
`VendorFull` has no `FINAL_SILVER_SUFFIX`.

AGENTS.md reference: [designs/AGENTS.md:L10-L12](https://github.com/o/r/blob/abc/designs/AGENTS.md#L10-L12)

Useful? React with 👍 / 👎.
"""

HUMAN_BODY = "This branch of the conditional never runs for the empty case. Can you add a test?"


# --- azdo details URL ---------------------------------------------------------


def test_parse_azdo_details_url_extracts_build_and_project():
    url = (
        "https://dev.azure.com/example-org/00000000-1111-2222-3333-444444444444"
        "/_build/results?buildId=204627&view=logs&jobId=3a559e2a-952e-58d2-b8db-2e604a9266d7"
    )
    got = ps.parse_azdo_details_url(url)
    assert got == {
        "buildId": "204627",
        "org": "example-org",
        "project": "00000000-1111-2222-3333-444444444444",
        "jobId": "3a559e2a-952e-58d2-b8db-2e604a9266d7",
    }


def test_parse_azdo_details_url_ignores_non_azdo():
    assert ps.parse_azdo_details_url("https://github.com/o/r/actions/runs/1/job/2") is None
    assert ps.parse_azdo_details_url("https://cursor.com/docs/bugbot") is None
    assert ps.parse_azdo_details_url(None) is None


def test_parse_azdo_details_url_needs_a_build_id():
    assert ps.parse_azdo_details_url("https://dev.azure.com/example-org/Main/_build") is None


# --- check dedupe -------------------------------------------------------------


def _check_run(name, conclusion, *, started, completed=None, url=None, status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": url,
        "startedAt": started,
        "completedAt": completed or started,
    }


def test_latest_checks_drops_stale_reruns():
    """A re-run leaves the failed attempt in the rollup; the newest one wins."""
    nodes = [
        _check_run("linear / Check Linear Link", "FAILURE", started="2026-07-28T17:00:00Z"),
        _check_run("linear / Check Linear Link", "FAILURE", started="2026-07-28T17:01:00Z"),
        _check_run("linear / Check Linear Link", "SUCCESS", started="2026-07-28T17:50:00Z"),
        _check_run("etl-service (Lint)", "SUCCESS", started="2026-07-28T17:10:00Z"),
    ]
    checks = ps.latest_checks(nodes)
    assert [c["name"] for c in checks] == ["etl-service (Lint)", "linear / Check Linear Link"]
    assert {c["name"]: c["state"] for c in checks}["linear / Check Linear Link"] == "passing"
    assert ps.effective_rollup(checks) == "SUCCESS"


def test_latest_checks_prefers_an_inflight_rerun_over_an_older_failure():
    nodes = [
        _check_run("Test", "FAILURE", started="2026-07-28T17:00:00Z"),
        _check_run("Test", None, started="2026-07-28T17:30:00Z", completed=None, status="IN_PROGRESS"),
    ]
    checks = ps.latest_checks(nodes)
    assert len(checks) == 1
    assert checks[0]["state"] == "pending"
    assert ps.effective_rollup(checks) == "PENDING"


def test_effective_rollup_ranks_failure_over_pending():
    checks = ps.latest_checks(
        [
            _check_run("A", "FAILURE", started="2026-07-28T17:00:00Z"),
            _check_run("B", None, started="2026-07-28T17:00:00Z", status="QUEUED"),
            _check_run("C", "SUCCESS", started="2026-07-28T17:00:00Z"),
        ]
    )
    assert ps.effective_rollup(checks) == "FAILURE"


def test_status_context_is_normalized_alongside_check_runs():
    checks = ps.latest_checks(
        [
            {
                "__typename": "StatusContext",
                "context": "legacy/ci",
                "state": "FAILURE",
                "targetUrl": "https://dev.azure.com/example-org/Main/_build/results?buildId=99",
                "createdAt": "2026-07-28T17:00:00Z",
            }
        ]
    )
    assert checks[0]["state"] == "failing"
    assert checks[0]["azdo"]["buildId"] == "99"


def test_neutral_conclusions_do_not_fail_the_rollup():
    checks = ps.latest_checks(
        [
            _check_run("skipped-job", "SKIPPED", started="2026-07-28T17:00:00Z"),
            _check_run("ok", "SUCCESS", started="2026-07-28T17:00:00Z"),
        ]
    )
    assert ps.effective_rollup(checks) == "SUCCESS"


def test_group_azdo_builds_collapses_jobs_into_one_build():
    url = "https://dev.azure.com/example-org/Main/_build/results?buildId=204627"
    checks = ps.latest_checks(
        [
            _check_run("etl-service", "SUCCESS", started="2026-07-28T17:00:00Z", url=url),
            _check_run(
                "etl-service (Lint)",
                "FAILURE",
                started="2026-07-28T17:00:00Z",
                url=url + "&view=logs&jobId=abc",
            ),
            _check_run("Cursor Bugbot", "SUCCESS", started="2026-07-28T17:00:00Z",
                       url="https://cursor.com/docs/bugbot"),
        ]
    )
    builds = ps.group_azdo_builds(checks)
    assert len(builds) == 1
    assert builds[0]["buildId"] == "204627"
    assert builds[0]["state"] == "failing"
    assert builds[0]["failingChecks"] == ["etl-service (Lint)"]
    assert builds[0]["url"].endswith("buildId=204627")


# --- author classification ----------------------------------------------------


def test_classify_author():
    assert ps.classify_author("cursor") == ps.SOURCE_CURSOR
    assert ps.classify_author("cursor[bot]") == ps.SOURCE_CURSOR
    assert ps.classify_author("chatgpt-codex-connector") == ps.SOURCE_CODEX
    assert ps.classify_author("coderabbitai") == ps.SOURCE_BOT
    assert ps.classify_author("some-app[bot]") == ps.SOURCE_BOT
    assert ps.classify_author("alice") == ps.SOURCE_HUMAN
    assert ps.classify_author(None) == ps.SOURCE_HUMAN


# --- bot body parsing ---------------------------------------------------------


def test_parse_cursor_body():
    got = ps.parse_cursor_body(CURSOR_BODY)
    assert got["title"] == "FF silver table resolution crashes"
    assert got["severity"] == "high"
    assert got["bug_id"] == "0fa3c9da-a229-4985-8c08-f5f23472eb55"
    assert got["reviewed_commit"] == "756940989e82b1025e9f6fd0da4cd938d99b3b2d"
    assert [(loc.path, loc.start, loc.end) for loc in got["locations"]] == [
        ("plugins/example-org/events/vendor_currency_qa.py", 312, 326),
        ("plugins/example-org/events/vendor_currency_qa.py", 117, 132),
    ]
    # The description survives; the CTA markup and footer do not.
    assert "_resolve_final_silver_table" in got["body"]
    assert "Fix in Cursor" not in got["body"]
    assert "cursor.com/assets" not in got["body"]
    assert "Reviewed by" not in got["body"]


def test_parse_codex_body():
    got = ps.parse_codex_body(CODEX_BODY)
    assert got["title"] == "Resolve the Fundamentals silver suffix explicitly"
    assert got["severity"] == "P1"
    assert "VendorFull" in got["body"]
    assert "img.shields.io" not in got["body"]
    assert "Useful?" not in got["body"]
    assert got["references"] == ["designs/AGENTS.md:L10-L12"]


def test_parse_human_body_titles_from_the_first_sentence():
    got = ps.parse_human_body(HUMAN_BODY)
    assert got["title"] == "This branch of the conditional never runs for the empty case"
    assert got["severity"] is None


def test_parse_locations_tolerates_bare_paths_and_bullets():
    locs = ps.parse_locations(
        "plugins/a.py#L10-L20\n- `plugins/b.py#L5`\nplugins/c.py\n\n"
    )
    assert [(loc.path, loc.start, loc.end) for loc in locs] == [
        ("plugins/a.py", 10, 20),
        ("plugins/b.py", 5, 5),
        ("plugins/c.py", None, None),
    ]


def test_strip_chrome_is_idempotent_on_plain_text():
    assert ps.strip_chrome("just prose") == "just prose"


# --- whole-payload parsing ----------------------------------------------------


def _thread(
    tid,
    author,
    body,
    *,
    path="plugins/a.py",
    line=None,
    original_line=None,
    resolved=False,
    outdated=False,
    replies=(),
):
    comments = [{"databaseId": 1, "author": {"login": author}, "createdAt": "2026-07-28T00:00:00Z", "body": body}]
    for i, (who, text) in enumerate(replies, start=2):
        comments.append(
            {"databaseId": i, "author": {"login": who}, "createdAt": "2026-07-28T01:00:00Z", "body": text}
        )
    return {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": outdated,
        "isCollapsed": False,
        "path": path,
        "line": line,
        "originalLine": original_line,
        "comments": {"nodes": comments},
    }


def _payload(threads, checks=None):
    return {
        "data": {
            "viewer": {"login": "shr3kst3r"},
            "repository": {
                "pullRequest": {
                    "number": 945,
                    "title": "CX-79 currency-consistency validation",
                    "url": "https://github.com/o/r/pull/945",
                    "isDraft": False,
                    "state": "OPEN",
                    "baseRefName": "main",
                    "headRefName": "feat-79",
                    "headRefOid": "7569409" + "0" * 33,
                    "updatedAt": "2026-07-29T18:01:53Z",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "APPROVED",
                    "author": {"login": "shr3kst3r"},
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": "7569409" + "0" * 33,
                                    "statusCheckRollup": {
                                        "state": "FAILURE",
                                        "contexts": {"nodes": checks or []},
                                    },
                                }
                            }
                        ]
                    },
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"login": "alice"},
                                "state": "APPROVED",
                                "submittedAt": "2026-07-29T00:00:00Z",
                                "body": "",
                            }
                        ]
                    },
                    "reviewThreads": {"nodes": threads},
                }
            },
        }
    }


def test_parse_pr_state_end_to_end():
    payload = _payload(
        threads=[
            _thread("T1", "cursor", CURSOR_BODY, line=None, outdated=True),
            _thread("T2", "chatgpt-codex-connector", CODEX_BODY, line=644),
            _thread("T3", "alice", HUMAN_BODY, line=12),
            _thread("T4", "cursor", CURSOR_BODY, resolved=True),
        ],
        checks=[
            _check_run("linear / Check Linear Link", "FAILURE", started="2026-07-28T17:00:00Z"),
            _check_run("linear / Check Linear Link", "SUCCESS", started="2026-07-28T17:50:00Z"),
            _check_run(
                "etl-service (Test)",
                "FAILURE",
                started="2026-07-28T17:10:00Z",
                url="https://dev.azure.com/example-org/Main/_build/results?buildId=204627",
            ),
        ],
    )
    state = ps.parse_pr_state(payload)

    assert state["pr"]["number"] == 945
    assert state["viewer"] == "shr3kst3r"

    # GitHub said FAILURE partly because of the stale linear re-run; the azdo
    # test failure is the only real one, so the effective rollup stays FAILURE
    # but the stale duplicate is gone.
    assert state["checks"]["reportedRollup"] == "FAILURE"
    assert state["checks"]["rollup"] == "FAILURE"
    assert state["checks"]["staleDuplicatesDropped"] == 1
    assert [c["name"] for c in state["checks"]["failing"]] == ["etl-service (Test)"]
    assert state["checks"]["azdoBuilds"][0]["buildId"] == "204627"

    # Resolved thread dropped; human sorted first; cursor line recovered from LOCATIONS.
    assert [t["id"] for t in state["threads"]] == ["T3", "T2", "T1"]
    by_id = {t["id"]: t for t in state["threads"]}
    assert by_id["T1"]["line"] == 312
    assert by_id["T1"]["lineFrom"] == "locations"
    assert by_id["T1"]["outdated"] is True
    assert by_id["T2"]["severity"] == "P1"
    assert by_id["T2"]["lineFrom"] == "thread"
    assert by_id["T3"]["source"] == "human"
    assert state["summary"] == {
        "failingChecks": 1,
        "pendingChecks": 0,
        "openThreads": 3,
        "threadsBySource": {"human": 1, "codex": 1, "cursor": 1},
        "actionable": True,
        "waiting": False,
    }


def test_include_resolved_keeps_resolved_threads():
    payload = _payload(threads=[_thread("T4", "cursor", CURSOR_BODY, resolved=True)])
    assert ps.parse_pr_state(payload)["threads"] == []
    kept = ps.parse_pr_state(payload, include_resolved=True)["threads"]
    assert [t["id"] for t in kept] == ["T4"]


def test_unanswered_only_drops_threads_the_viewer_replied_to():
    payload = _payload(
        threads=[
            _thread("T1", "cursor", CURSOR_BODY, replies=[("shr3kst3r", "declined: see below")]),
            _thread("T2", "cursor", CURSOR_BODY),
        ]
    )
    everything = ps.parse_pr_state(payload)
    assert {t["id"]: t["answeredByViewer"] for t in everything["threads"]} == {
        "T1": True,
        "T2": False,
    }
    unanswered = ps.parse_pr_state(payload, unanswered_only=True)
    assert [t["id"] for t in unanswered["threads"]] == ["T2"]


def test_waiting_state_when_only_checks_are_in_flight():
    payload = _payload(
        threads=[],
        checks=[_check_run("Test", None, started="2026-07-28T17:00:00Z", status="IN_PROGRESS")],
    )
    summary = ps.parse_pr_state(payload)["summary"]
    assert summary["actionable"] is False
    assert summary["waiting"] is True


def test_green_pr_is_neither_actionable_nor_waiting():
    payload = _payload(
        threads=[], checks=[_check_run("Test", "SUCCESS", started="2026-07-28T17:00:00Z")]
    )
    summary = ps.parse_pr_state(payload)["summary"]
    assert (summary["actionable"], summary["waiting"]) == (False, False)


def test_thread_with_no_comments_is_skipped():
    payload = _payload(threads=[{"id": "T0", "comments": {"nodes": []}}])
    assert ps.parse_pr_state(payload)["threads"] == []


def test_payload_without_pull_request_raises():
    try:
        ps.parse_pr_state({"data": {"repository": {"pullRequest": None}}})
    except ValueError as exc:
        assert "no pullRequest" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# --- CLI ----------------------------------------------------------------------


def test_main_exit_codes_from_a_saved_payload(tmp_path, capsys):
    green = tmp_path / "green.json"
    green.write_text(
        json.dumps(_payload(threads=[], checks=[_check_run("T", "SUCCESS", started="2026-07-28T00:00:00Z")])),
        encoding="utf-8",
    )
    assert ps.main(["--parse-file", str(green), "--exit-code"]) == ps.EXIT_GREEN
    capsys.readouterr()  # discard the green report so the JSON below stands alone

    red = tmp_path / "red.json"
    red.write_text(
        json.dumps(_payload(threads=[_thread("T1", "cursor", CURSOR_BODY)])), encoding="utf-8"
    )
    assert ps.main(["--parse-file", str(red), "--exit-code", "--json"]) == ps.EXIT_ACTIONABLE
    assert json.loads(capsys.readouterr().out)["summary"]["openThreads"] == 1

    waiting = tmp_path / "waiting.json"
    waiting.write_text(
        json.dumps(
            _payload(
                threads=[],
                checks=[_check_run("T", None, started="2026-07-28T00:00:00Z", status="QUEUED")],
            )
        ),
        encoding="utf-8",
    )
    assert ps.main(["--parse-file", str(waiting), "--exit-code"]) == ps.EXIT_WAITING


def test_main_reports_missing_file_as_error(capsys):
    assert ps.main(["--parse-file", "/nonexistent/payload.json"]) == ps.EXIT_ERROR
    assert "pr_state:" in capsys.readouterr().err


def test_render_flags_the_stale_rollup_discrepancy():
    payload = _payload(
        threads=[_thread("T1", "cursor", CURSOR_BODY, line=10)],
        checks=[
            _check_run("linear", "FAILURE", started="2026-07-28T17:00:00Z"),
            _check_run("linear", "SUCCESS", started="2026-07-28T17:50:00Z"),
        ],
    )
    text = ps.render(ps.parse_pr_state(payload))
    assert "stale re-run duplicate(s) ignored" in text
    assert "Open threads: 1" in text
    assert "FF silver table resolution crashes" in text


def test_render_stays_quiet_when_a_pr_has_no_checks_at_all():
    """A null reported rollup is not a stale-duplicate story."""
    text = ps.render(ps.parse_pr_state(_payload(threads=[], checks=[])))
    assert "stale re-run duplicate(s)" not in text
    assert "Checks: NEUTRAL" in text
    assert "Open threads: none" in text
