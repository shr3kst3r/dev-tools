"""Tests for the pure parsing layer of pr-watch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.console import Console
from textual.containers import VerticalScroll

from tools.pr_watch.app import PrWatchApp
from tools.pr_watch.cli import _parse_args
from tools.pr_watch.github import parse_pull_request
from tools.pr_watch.models import (
    Check,
    CheckState,
    PRMetrics,
    PullRequest,
    RepoContext,
    ReviewThread,
)
from tools.pr_watch.ui import format_duration, format_relative, render_body


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
                                        "startedAt": "2026-07-09T00:00:00Z",
                                        "completedAt": "2026-07-09T00:03:20Z",
                                    },
                                    {
                                        "__typename": "CheckRun",
                                        "name": "lint",
                                        "status": "IN_PROGRESS",
                                        "conclusion": None,
                                        "detailsUrl": "https://ci/2",
                                        "startedAt": "2026-07-09T00:00:00Z",
                                        "completedAt": None,
                                    },
                                    {
                                        "__typename": "StatusContext",
                                        "context": "legacy/deploy",
                                        "state": "ERROR",
                                        "targetUrl": "https://ci/3",
                                        "createdAt": "2026-07-09T00:00:00Z",
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


def test_check_timing_parsed() -> None:
    pr = parse_pull_request(_node())
    by_name = {c.name: c for c in pr.checks}
    start = datetime(2026, 7, 9, tzinfo=timezone.utc)
    # Finished CheckRun keeps both timestamps (duration = completed - started).
    assert by_name["unit-tests"].started_at == start
    assert by_name["unit-tests"].completed_at == start + timedelta(minutes=3, seconds=20)
    # Running CheckRun has a start but no completion yet.
    assert by_name["lint"].started_at == start
    assert by_name["lint"].completed_at is None
    # StatusContext createdAt is treated as the start; no completion timestamp.
    assert by_name["legacy/deploy"].started_at == start
    assert by_name["legacy/deploy"].completed_at is None


def test_format_duration() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(200) == "3m 20s"
    assert format_duration(3600) == "1h 00m"
    assert format_duration(3660) == "1h 01m"
    # Negative (clock skew / future start) clamps to zero rather than going negative.
    assert format_duration(-5) == "0s"


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


# --- rendering + scrolling ----------------------------------------------------
# The live view is a Textual app whose body sits in a scrollable viewport, so
# the renderer never truncates: everything is reachable by scrolling.


def _make_pr(n_checks: int, n_threads: int, n_failures: int = 0) -> PullRequest:
    checks = [
        Check(name=f"fail-{i}", state=CheckState.FAILURE) for i in range(n_failures)
    ] + [
        Check(name=f"pass-{i:03d}", state=CheckState.SUCCESS)
        for i in range(n_checks - n_failures)
    ]
    threads = [
        ReviewThread(
            author=f"reviewer{i}",
            body=f"comment number {i} " * 20,
            path=f"src/file_{i}.py",
            line=i + 1,
            url=None,
            comment_count=2,
            is_outdated=False,
        )
        for i in range(n_threads)
    ]
    metrics = PRMetrics(
        additions=1,
        deletions=1,
        changed_files=1,
        commits=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        review_decision=None,
        mergeable="MERGEABLE",
        approvals=0,
        changes_requested=0,
    )
    return PullRequest(
        number=1,
        title="A title",
        url="https://github.com/o/r/pull/1",
        is_draft=False,
        author="octocat",
        rollup=CheckState.FAILURE if n_failures else CheckState.SUCCESS,
        metrics=metrics,
        checks=checks,
        threads=threads,
    )


def _render_text(pr: PullRequest, width: int = 100) -> str:
    console = Console(width=width)
    view = render_body(pr, None, RepoContext("o", "r", "branch"))
    with console.capture() as capture:
        console.print(view)
    return capture.get()


def test_render_body_never_truncates() -> None:
    pr = _make_pr(n_checks=60, n_threads=25, n_failures=2)
    output = _render_text(pr)
    assert "fail-0" in output and "fail-1" in output
    assert "pass-057" in output  # last check alphabetically — nothing dropped
    assert "@reviewer24" in output  # last thread — nothing dropped


def test_render_body_states() -> None:
    ctx = RepoContext("o", "r", "branch")
    console = Console(width=80)

    def text_of(view) -> str:
        with console.capture() as capture:
            console.print(view)
        return capture.get()

    assert "Contacting GitHub" in text_of(render_body(None, None, ctx, loading=True))
    assert "boom" in text_of(render_body(None, "boom", ctx))
    assert "No open PR" in text_of(render_body(None, None, ctx))


async def test_live_app_scrolls_long_content() -> None:
    # The whole point of the Textual rewrite: content taller than the screen
    # must be reachable by scrolling instead of being cropped.
    pr = _make_pr(n_checks=60, n_threads=25, n_failures=2)
    app = PrWatchApp(
        ctx=RepoContext("o", "r", "branch"),
        poll=lambda: (pr, None),
        interval=30,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        viewport = app.query_one(VerticalScroll)
        assert viewport.max_scroll_y > 0, "long content should overflow the viewport"
        assert viewport.scroll_y == 0

        viewport.scroll_end(animate=False)
        await pilot.pause()
        assert viewport.scroll_y == viewport.max_scroll_y
