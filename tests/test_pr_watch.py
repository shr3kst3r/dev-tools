"""Tests for the pure parsing layer of pr-watch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools.pr_watch.cli import _parse_args
from tools.pr_watch.github import parse_pull_request
from tools.pr_watch.models import CheckState, RepoContext
from tools.pr_watch.ui import format_relative


def _node() -> dict:
    """A representative GraphQL PR node exercising every branch."""
    return {
        "number": 42,
        "title": "Add pr-watch",
        "url": "https://github.com/o/r/pull/42",
        "isDraft": False,
        "author": {"login": "octocat"},
        "additions": 120,
        "deletions": 8,
        "changedFiles": 5,
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-07-09T00:00:00Z",
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeable": "CONFLICTING",
        "latestOpinionatedReviews": {
            "nodes": [
                {"state": "APPROVED"},
                {"state": "CHANGES_REQUESTED"},
                {"state": "APPROVED"},
            ]
        },
        "commits": {
            "totalCount": 7,
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "FAILURE",
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "unit-tests",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "detailsUrl": "https://ci/1",
                                    },
                                    {
                                        "__typename": "CheckRun",
                                        "name": "lint",
                                        "status": "IN_PROGRESS",
                                        "conclusion": None,
                                        "detailsUrl": "https://ci/2",
                                    },
                                    {
                                        "__typename": "StatusContext",
                                        "context": "legacy/deploy",
                                        "state": "ERROR",
                                        "targetUrl": "https://ci/3",
                                    },
                                ]
                            },
                        }
                    }
                }
            ]
        },
        "reviewThreads": {
            "nodes": [
                {
                    "isResolved": True,
                    "isOutdated": False,
                    "comments": {
                        "totalCount": 1,
                        "nodes": [
                            {
                                "author": {"login": "someone"},
                                "body": "resolved already",
                                "path": "a.py",
                                "line": 1,
                                "originalLine": 1,
                                "url": "u",
                            }
                        ],
                    },
                },
                {
                    "isResolved": False,
                    "isOutdated": True,
                    "comments": {
                        "totalCount": 3,
                        "nodes": [
                            {
                                "author": {"login": "reviewer"},
                                "body": "  please rename   this  ",
                                "path": "b.py",
                                "line": None,
                                "originalLine": 88,
                                "url": "u2",
                            }
                        ],
                    },
                },
            ]
        },
    }


def test_parse_basic_fields() -> None:
    pr = parse_pull_request(_node())
    assert pr.number == 42
    assert pr.author == "octocat"
    assert pr.rollup is CheckState.FAILURE


def test_metrics_parsed() -> None:
    m = parse_pull_request(_node()).metrics
    assert (m.additions, m.deletions, m.changed_files) == (120, 8, 5)
    assert m.commits == 7
    assert m.review_decision == "CHANGES_REQUESTED"
    assert m.mergeable == "CONFLICTING"
    assert m.approvals == 2
    assert m.changes_requested == 1
    assert m.created_at == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_check_states_normalized() -> None:
    pr = parse_pull_request(_node())
    by_name = {c.name: c.state for c in pr.checks}
    assert by_name["unit-tests"] is CheckState.SUCCESS
    assert by_name["lint"] is CheckState.PENDING  # in_progress -> pending
    assert by_name["legacy/deploy"] is CheckState.FAILURE  # ERROR status ctx


def test_only_unresolved_threads_kept() -> None:
    pr = parse_pull_request(_node())
    assert len(pr.threads) == 1
    thread = pr.threads[0]
    assert thread.author == "reviewer"
    assert thread.comment_count == 3
    assert thread.is_outdated is True
    # line falls back to originalLine when line is null (outdated).
    assert thread.line == 88
    assert thread.location == "b.py:88"


def test_no_pr_node_shapes() -> None:
    # Missing commit/rollup should not raise; yields empty checks + unknown rollup.
    pr = parse_pull_request(
        {
            "number": 1,
            "title": "t",
            "url": "u",
            "isDraft": True,
            "author": {"login": "x"},
            "commits": {"nodes": []},
            "reviewThreads": {"nodes": []},
        }
    )
    assert pr.checks == []
    assert pr.threads == []
    assert pr.rollup is CheckState.UNKNOWN
    assert pr.is_draft is True
    # Missing metric fields degrade to safe defaults, never raise.
    assert pr.metrics.additions == 0
    assert pr.metrics.commits == 0
    assert pr.metrics.mergeable == "UNKNOWN"
    assert pr.metrics.created_at is None


def test_repo_context_name() -> None:
    assert RepoContext("o", "r", "main").name_with_owner == "o/r"


def test_format_relative() -> None:
    now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    ago = lambda **kw: format_relative(now - timedelta(**kw), now)  # noqa: E731
    assert format_relative(None, now) == "—"
    assert ago(seconds=30) == "just now"
    assert ago(minutes=15) == "15m ago"
    assert ago(hours=3) == "3h ago"
    assert ago(days=4) == "4d ago"
    assert ago(days=14) == "2w ago"
    assert ago(days=60) == "2mo ago"
    # A future timestamp clamps to "just now" rather than going negative.
    assert format_relative(now + timedelta(hours=1), now) == "just now"


def test_default_directory_uses_spg_invocation_dir(monkeypatch) -> None:
    # When launched via an spg wrapper (which cd's into the repo), the caller's
    # directory arrives in $SPG_INVOCATION_DIR and becomes the default target.
    monkeypatch.setenv("SPG_INVOCATION_DIR", "/some/where")
    assert _parse_args([]).directory == "/some/where"


def test_default_directory_falls_back_to_cwd(monkeypatch) -> None:
    monkeypatch.delenv("SPG_INVOCATION_DIR", raising=False)
    assert _parse_args([]).directory == "."


def test_explicit_directory_overrides_spg_invocation_dir(monkeypatch) -> None:
    monkeypatch.setenv("SPG_INVOCATION_DIR", "/some/where")
    assert _parse_args(["/other/place"]).directory == "/other/place"
