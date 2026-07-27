"""Tests for the pure layers of my-prs (parsing, attention flags, sorting,
list rendering) plus the master/detail Textual app."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.console import Console
from textual.widgets import DataTable, Static

from tools.my_prs import gw, layout, ui
from tools.my_prs.app import (
    POLL_HISTORY_LIMIT,
    GwRmScreen,
    HelpScreen,
    LogScreen,
    MyPrsApp,
)
from tools.my_prs.cli import _parse_args
from tools.my_prs.github import (
    GitHubError,
    PollError,
    build_search_query,
    classify_github_error,
    parse_search,
)
from tools.my_prs.models import PrItem, sort_items
from tools.pr_watch.models import Check, CheckState, PRMetrics, PullRequest

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_pr(
    number: int = 1,
    *,
    title: str = "A title",
    is_draft: bool = False,
    rollup: CheckState = CheckState.SUCCESS,
    n_failures: int = 0,
    n_threads: int = 0,
    review_decision: str | None = None,
    approvals: int = 0,
    updated_at: datetime | None = None,
) -> PullRequest:
    from tools.pr_watch.models import ReviewThread

    checks = [
        Check(name=f"fail-{i}", state=CheckState.FAILURE) for i in range(n_failures)
    ]
    threads = [
        ReviewThread(
            author=f"reviewer{i}",
            body=f"comment {i}",
            path="a.py",
            line=1,
            url=None,
            comment_count=1,
            is_outdated=False,
        )
        for i in range(n_threads)
    ]
    metrics = PRMetrics(
        additions=1,
        deletions=1,
        changed_files=1,
        commits=1,
        created_at=NOW - timedelta(days=2),
        updated_at=updated_at or NOW,
        review_decision=review_decision,
        mergeable="MERGEABLE",
        approvals=approvals,
        changes_requested=0,
    )
    return PullRequest(
        number=number,
        title=title,
        url=f"https://github.com/o/r/pull/{number}",
        is_draft=is_draft,
        author="me",
        rollup=rollup,
        metrics=metrics,
        checks=checks,
        threads=threads,
    )


def _item(pr: PullRequest, repo: str = "example-org/dev-tools", branch: str = "b") -> PrItem:
    return PrItem(repo=repo, branch=branch, pr=pr)


# --- search query --------------------------------------------------------


def test_build_search_query() -> None:
    q = build_search_query(14, now=NOW)
    assert q == "is:pr is:open author:@me updated:>=2026-07-01 sort:updated-desc"


def test_build_search_query_custom_author() -> None:
    assert "author:octocat" in build_search_query(7, now=NOW, author="octocat")


def test_build_search_query_review_view() -> None:
    q = build_search_query(14, now=NOW, view="review")
    assert q == "is:pr is:open review-requested:@me updated:>=2026-07-01 sort:updated-desc"
    assert "review-requested:octocat" in build_search_query(
        7, now=NOW, author="octocat", view="review"
    )


# --- error classification -------------------------------------------------


def test_classify_rate_limit_is_concise_and_backs_off() -> None:
    # The raw error embeds the whole gh command; classification must drop it.
    raw = GitHubError(
        "`gh api graphql -f query=query($q: String!, $limit: Int!) {…` failed: "
        "gh: API rate limit exceeded for user ID 114263."
    )
    err = classify_github_error(raw)
    assert err.rate_limited is True
    assert "rate limit" in err.message.lower()
    assert "gh api graphql" not in err.message  # no command dump reaches the UI


def test_classify_rate_limit_reads_retry_after() -> None:
    err = classify_github_error(
        GitHubError("`gh api …` failed: secondary rate limit. Retry-After: 45 seconds")
    )
    assert err.rate_limited is True
    assert err.retry_after == 45


def test_classify_auth_error() -> None:
    err = classify_github_error(
        GitHubError("`gh api …` failed: gh: Bad credentials (HTTP 401)")
    )
    assert err.rate_limited is False
    assert "gh auth login" in err.message


def test_classify_generic_error_keeps_gh_message_only() -> None:
    err = classify_github_error(
        GitHubError("`gh api graphql -f query=…` failed: gh: something went wrong")
    )
    assert err.rate_limited is False
    assert err.message == "gh: something went wrong"


def test_classify_graphql_rate_limit() -> None:
    # GraphQL-level errors come through without a "failed:" prefix.
    err = classify_github_error(
        GitHubError("GitHub GraphQL error: API rate limit exceeded")
    )
    assert err.rate_limited is True


def test_fetch_all_views_one_request_both_views(monkeypatch) -> None:
    import json

    from tools.my_prs import github as gh

    captured: dict[str, list[str]] = {}

    def fake_run(args: list[str], cwd) -> str:
        captured["args"] = args
        return json.dumps(
            {
                "data": {
                    "mine": {"nodes": [_search_node(1)]},
                    "review": {"nodes": [_search_node(2), _search_node(3)]},
                }
            }
        )

    monkeypatch.setattr(gh, "_run", fake_run)
    views = gh.fetch_all_views(days=14, limit=50)
    assert [i.pr.number for i in views["mine"]] == [1]
    assert [i.pr.number for i in views["review"]] == [2, 3]
    # A single gh graphql call carries both searches (one request per poll).
    assert captured["args"][:3] == ["gh", "api", "graphql"]
    joined = " ".join(captured["args"])
    assert "mine=" in joined and "review=" in joined


# --- parsing --------------------------------------------------------------


def _search_node(number: int = 7) -> dict:
    return {
        "repository": {"nameWithOwner": "example-org/dev-tools"},
        "headRefName": "s/feature",
        "number": number,
        "title": "Add my-prs",
        "url": f"https://github.com/example-org/dev-tools/pull/{number}",
        "isDraft": False,
        "author": {"login": "me"},
        "additions": 10,
        "deletions": 2,
        "changedFiles": 3,
        "createdAt": "2026-07-10T00:00:00Z",
        "updatedAt": "2026-07-14T00:00:00Z",
        "reviewDecision": "REVIEW_REQUIRED",
        "mergeable": "MERGEABLE",
        "latestOpinionatedReviews": {"nodes": []},
        "commits": {"totalCount": 2, "nodes": []},
        "reviewThreads": {"nodes": []},
    }


def test_parse_search_wraps_repo_and_branch() -> None:
    items = parse_search([_search_node()])
    assert len(items) == 1
    item = items[0]
    assert item.repo == "example-org/dev-tools"
    assert item.repo_name == "dev-tools"
    assert item.branch == "s/feature"
    assert item.key == "example-org/dev-tools#7"
    assert item.ctx.name_with_owner == "example-org/dev-tools"
    assert item.ctx.branch == "s/feature"
    assert item.pr.number == 7


def test_parse_search_skips_non_pr_nodes() -> None:
    # Search hits that aren't PRs come back as empty objects.
    items = parse_search([{}, _search_node(), {}])
    assert [i.pr.number for i in items] == [7]


# --- attention flags ------------------------------------------------------


def test_failing_checks_need_attention() -> None:
    item = _item(_make_pr(rollup=CheckState.FAILURE, n_failures=1, approvals=1))
    assert item.failing
    assert item.needs_attention


def test_open_threads_need_attention() -> None:
    item = _item(_make_pr(n_threads=2, approvals=1))
    assert item.open_threads == 2
    assert item.needs_attention


def test_review_required_needs_attention() -> None:
    item = _item(_make_pr(review_decision="REVIEW_REQUIRED"))
    assert item.review_gap
    assert item.needs_attention


def test_changes_requested_needs_attention() -> None:
    assert _item(_make_pr(review_decision="CHANGES_REQUESTED")).review_gap


def test_no_required_reviews_falls_back_to_approvals() -> None:
    # decision None: the repo requires no reviews, so any approval clears it.
    assert _item(_make_pr(review_decision=None, approvals=0)).review_gap
    assert not _item(_make_pr(review_decision=None, approvals=1)).review_gap


def test_draft_never_has_review_gap() -> None:
    item = _item(_make_pr(is_draft=True, review_decision="REVIEW_REQUIRED"))
    assert not item.review_gap


def test_approved_green_pr_needs_nothing() -> None:
    item = _item(_make_pr(review_decision="APPROVED", approvals=1))
    assert not item.needs_attention
    assert item.ready


def test_pending_checks_are_not_ready() -> None:
    item = _item(_make_pr(rollup=CheckState.PENDING, review_decision="APPROVED"))
    assert not item.needs_attention  # nothing to act on yet…
    assert not item.ready  # …but not green until checks finish


def test_draft_is_never_ready() -> None:
    assert not _item(_make_pr(is_draft=True)).ready


# --- sorting ---------------------------------------------------------------


def test_sort_attention_first_then_recency() -> None:
    calm_new = _item(_make_pr(1, review_decision="APPROVED", updated_at=NOW))
    calm_old = _item(
        _make_pr(2, review_decision="APPROVED", updated_at=NOW - timedelta(days=3))
    )
    hot_old = _item(
        _make_pr(
            3,
            rollup=CheckState.FAILURE,
            review_decision="APPROVED",
            updated_at=NOW - timedelta(days=5),
        )
    )
    ordered = sort_items([calm_old, calm_new, hot_old])
    assert [i.pr.number for i in ordered] == [3, 1, 2]


# --- list rendering ---------------------------------------------------------


def test_attention_cell_dots() -> None:
    needs_me = _item(_make_pr(rollup=CheckState.FAILURE, n_failures=1, approvals=1))
    assert ui.attention_cell(needs_me).style == "bold red"
    ready = _item(_make_pr(review_decision="APPROVED", approvals=1))
    assert ui.attention_cell(ready).style == "bold green"
    in_between = _item(_make_pr(rollup=CheckState.PENDING, review_decision="APPROVED"))
    assert ui.attention_cell(in_between).plain.strip() == ""


def test_ci_cell_states() -> None:
    assert ui.ci_cell(_item(_make_pr(rollup=CheckState.FAILURE, n_failures=2))).plain == "✖ 2"
    assert ui.ci_cell(_item(_make_pr(rollup=CheckState.SUCCESS))).plain == "✔"
    assert ui.ci_cell(_item(_make_pr(rollup=CheckState.UNKNOWN))).plain == "—"


def test_list_row_review_view_adds_author_column() -> None:
    item = _item(_make_pr())
    mine = ui.list_row(item, NOW)
    review = ui.list_row(item, NOW, "review")
    assert len(mine) == len(ui.list_columns("mine"))
    assert len(review) == len(ui.list_columns("review"))
    author_index = ui.list_columns("review").index("Author")
    assert review[author_index].plain == "me"


def test_review_cell_states() -> None:
    assert ui.review_cell(_item(_make_pr(is_draft=True))).plain == "draft"
    assert (
        ui.review_cell(_item(_make_pr(review_decision="APPROVED", approvals=2))).plain
        == "✔ 2"
    )
    assert (
        ui.review_cell(_item(_make_pr(review_decision="CHANGES_REQUESTED"))).plain
        == "✖ changes"
    )
    assert (
        ui.review_cell(_item(_make_pr(review_decision="REVIEW_REQUIRED"))).plain
        == "○ needed"
    )
    assert ui.review_cell(_item(_make_pr())).plain == "○ none"


def test_render_once_lists_every_pr() -> None:
    items = [
        _item(_make_pr(1, title="First change")),
        _item(_make_pr(2, title="Second change", n_threads=1)),
    ]
    console = Console(width=140)
    with console.capture() as capture:
        console.print(ui.render_once(items, NOW))
    output = capture.get()
    assert "First change" in output
    assert "Second change" in output
    assert "2 open" in output


def test_render_summary_counts() -> None:
    items = [
        _item(_make_pr(1, rollup=CheckState.FAILURE, n_failures=1, approvals=1)),
        _item(_make_pr(2, n_threads=3, approvals=1)),
        _item(_make_pr(3, review_decision="REVIEW_REQUIRED")),
        _item(_make_pr(4, review_decision="APPROVED", approvals=1)),
    ]
    text = ui.render_summary(items, None).plain
    assert "4 open" in text
    assert "1 failing" in text
    assert "1 with comments" in text
    assert "1 awaiting review" in text
    assert "1 ready" in text


def test_render_summary_shows_view_tabs() -> None:
    text = ui.render_summary([], None).plain
    assert "My PRs" in text
    assert "Needs my review" in text
    assert "0 open" in text
    review_text = ui.render_summary([_item(_make_pr())], None, "review").plain
    assert "1 to review" in review_text


def test_render_summary_error() -> None:
    assert "boom" in ui.render_summary(None, "boom").plain


def test_render_log_empty_and_with_entries() -> None:
    from tools.my_prs.models import LogEntry

    empty = Console(width=80)
    with empty.capture() as cap:
        empty.print(ui.render_log([]))
    assert "No activity yet" in cap.get()

    entries = [
        LogEntry(time=NOW, level="info", message="Refreshed — 2 mine · 1 review"),
        LogEntry(time=NOW, level="warn", message="rate limit — backing off"),
    ]
    console = Console(width=80)
    with console.capture() as cap:
        console.print(ui.render_log(entries))
    out = cap.get()
    assert "Refreshed" in out
    assert "rate limit" in out
    assert "Activity log" in out


# --- status bar --------------------------------------------------------------


def test_render_poll_dots_one_colored_dot_per_request() -> None:
    dots = ui.render_poll_dots(["ok", "error", "running"])
    assert dots.plain == "●●●"
    assert [span.style for span in dots.spans] == [
        "bold green",
        "bold red",
        "bold blue",
    ]


def test_render_status_bar_shows_dots_and_help_pointer_not_key_list() -> None:
    bar = ui.render_status_bar(NOW, 42, 60, ["ok", "error", "running"], refreshing=True)
    text = bar.plain
    assert "●●●" in text
    assert "refreshing…" in text
    # The keybindings live in the `?` popup now, not the bar.
    assert "? help" in text
    assert "v view" not in text
    assert "resize" not in text


def test_render_status_bar_countdown_and_no_dots_before_first_request() -> None:
    bar = ui.render_status_bar(NOW, 42, 60, [])
    assert "refresh in 42s" in bar.plain
    assert "(every 60s)" in bar.plain
    assert "●" not in bar.plain


# --- cli -------------------------------------------------------------------


def test_cli_defaults() -> None:
    args = _parse_args([])
    assert args.days == 14
    assert args.interval == 60
    assert args.limit == 50
    assert args.author == "@me"
    assert args.once is False
    assert args.view is None  # None: fall back to the saved layout's view


def test_cli_view_arg() -> None:
    assert _parse_args(["--view", "review"]).view == "review"


# --- the app ----------------------------------------------------------------


def _plain(widget: Static) -> str:
    """The plain text a Static widget was last updated with."""
    console = Console(width=120)
    with console.capture() as capture:
        console.print(widget.content)
    return capture.get()


def _fleet() -> list[PrItem]:
    return sort_items(
        [
            _item(_make_pr(1, title="Calm PR", review_decision="APPROVED", approvals=1)),
            _item(
                _make_pr(
                    2,
                    title="Broken PR",
                    rollup=CheckState.FAILURE,
                    n_failures=1,
                    review_decision="APPROVED",
                ),
                repo="example-org/other",
            ),
        ]
    )


def _review_fleet() -> list[PrItem]:
    return [
        _item(
            _make_pr(9, title="Teammate PR", review_decision="REVIEW_REQUIRED"),
            repo="example-org/backend",
        )
    ]


def _data(
    mine: list[PrItem] | None = None, review: list[PrItem] | None = None
) -> dict[str, list[PrItem]]:
    """A poll payload: both views' lists, defaulting to the standard fixtures."""
    return {
        "mine": _fleet() if mine is None else mine,
        "review": _review_fleet() if review is None else review,
    }


async def test_app_lists_prs_and_shows_detail() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        assert table.row_count == 2
        # Attention-needing PR is sorted (and selected) first.
        assert app._selected_key == "example-org/other#2"
        detail = app.query_one("#detail", Static)
        assert "Broken PR" in _plain(detail)

        # Moving the cursor swaps the detail pane.
        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"
        assert "Calm PR" in _plain(app.query_one("#detail", Static))


async def test_app_keeps_selection_across_refresh() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"
        assert app.query_one(DataTable).cursor_row == 1


async def test_app_error_keeps_last_good_list() -> None:
    polls: list[tuple[dict[str, list[PrItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="GitHub exploded")),
    ]
    app = MyPrsApp(poll=lambda: polls.pop(0), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The stale list stays usable; the error lands in the summary bar.
        assert app.query_one(DataTable).row_count == 2
        summary = app.query_one("#summary", Static)
        assert "GitHub exploded" in _plain(summary)


async def test_app_backs_off_then_recovers_on_rate_limit() -> None:
    polls: list[tuple[dict[str, list[PrItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="rate limited", rate_limited=True)),
        (None, PollError(message="rate limited", rate_limited=True)),
        (_data(), None),
    ]
    app = MyPrsApp(poll=lambda: polls.pop(0), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60  # normal cadence after a clean poll

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60  # first rate-limit hit: interval * 2**0

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 120  # second consecutive hit doubles

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._current_delay == 60  # a good poll resets the backoff


async def test_app_backoff_respects_retry_after_and_caps() -> None:
    app = MyPrsApp(
        poll=lambda: (None, PollError("slow down", rate_limited=True, retry_after=99999)),
        interval=60,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # retry_after is honored but capped at the 15-minute ceiling.
        assert app._current_delay == 900


async def test_app_logs_each_poll() -> None:
    polls: list[tuple[dict[str, list[PrItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="rate limited", rate_limited=True)),
    ]
    app = MyPrsApp(poll=lambda: polls.pop(0), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.activity_log) == 1
        assert app.activity_log[0].level == "info"
        assert "2 mine" in app.activity_log[0].message and "1 review" in app.activity_log[0].message

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.activity_log[-1].level == "warn"
        assert "rate limited" in app.activity_log[-1].message


async def test_status_bar_tracks_github_request_history() -> None:
    polls: list[tuple[dict[str, list[PrItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="boom")),
    ]
    app = MyPrsApp(poll=lambda: polls.pop(0), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._poll_history == ["ok"]

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._poll_history == ["ok", "error"]
        status = _plain(app.query_one("#status", Static))
        assert status.count("●") == 2  # one dot per request so far
        assert "? help" in status
        assert "v view" not in status  # keys moved to the `?` popup


async def test_poll_history_keeps_only_the_last_ten() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_history[:] = ["error"] * POLL_HISTORY_LIMIT
        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app._poll_history) == POLL_HISTORY_LIMIT
        assert app._poll_history[-1] == "ok"  # newest kept…
        assert app._poll_history[0] == "error"  # …oldest dropped


async def test_log_overlay_opens_and_closes_and_is_live() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)
        content = _plain(app.screen.query_one("#log-content", Static))
        assert "Refreshed" in content  # the first poll's line is visible

        await pilot.press("l")
        await pilot.pause()
        assert not isinstance(app.screen, LogScreen)


async def test_app_empty_state() -> None:
    app = MyPrsApp(poll=lambda: (_data(mine=[], review=[]), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0
        detail = _plain(app.query_one("#detail", Static))
        assert "No open PRs" in detail


# --- view switching -----------------------------------------------------------


async def test_switch_view_swaps_list_and_selection() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        assert app._view == "mine"
        assert table.row_count == 2
        # Park the cursor off the top row so we can check it's remembered.
        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"

        await pilot.press("v")
        await pilot.pause()
        assert app._view == "review"
        assert table.row_count == 1
        assert app._selected_key == "example-org/backend#9"
        assert "Teammate PR" in _plain(app.query_one("#detail", Static))
        assert "1 to review" in _plain(app.query_one("#summary", Static))
        # The review view grows an Author column.
        assert len(table.columns) == len(ui.list_columns("review"))

        # Switching back restores the other view's selection.
        await pilot.press("v")
        await pilot.pause()
        assert app._view == "mine"
        assert app._selected_key == "example-org/dev-tools#1"
        assert table.cursor_row == 1
        assert len(table.columns) == len(ui.list_columns("mine"))


async def test_review_view_empty_state() -> None:
    app = MyPrsApp(poll=lambda: (_data(review=[]), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        detail = _plain(app.query_one("#detail", Static))
        assert "No PRs waiting on your review" in detail


async def test_view_persisted_and_restored(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
    assert layout.load(path).view == "review"

    reopened = MyPrsApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with reopened.run_test(size=(140, 40)) as pilot:
        await reopened.workers.wait_for_complete()
        await pilot.pause()
        assert reopened._view == "review"
        assert reopened.query_one(DataTable).row_count == 1


async def test_initial_view_overrides_saved(tmp_path) -> None:
    path = tmp_path / "layout.json"
    layout.save(layout.Layout(view="review"), path)
    app = MyPrsApp(
        poll=lambda: (_data(), None),
        interval=60,
        layout_path=path,
        initial_view="mine",
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._view == "mine"


async def test_cycle_detail_moves_then_hides_pane() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        body = app.query_one("#body")
        detail_scroll = app.query_one("#detail-scroll")
        table = app.query_one(DataTable)
        assert not body.has_class("detail-below")
        assert not body.has_class("detail-hidden")

        # First press: detail moves below the list.
        await pilot.press("d")
        await pilot.pause()
        assert body.has_class("detail-below")
        assert not body.has_class("detail-hidden")
        assert detail_scroll.display
        assert table.size.height < body.size.height

        # Second press: detail disappears and the list gets the full window.
        await pilot.press("d")
        await pilot.pause()
        assert body.has_class("detail-hidden")
        assert not body.has_class("detail-below")
        assert not detail_scroll.display
        assert table.size.width == app.size.width

        # Third press: back to the side-by-side default.
        await pilot.press("d")
        await pilot.pause()
        assert not body.has_class("detail-below")
        assert not body.has_class("detail-hidden")
        assert detail_scroll.display
        assert table.size.width < app.size.width


async def test_help_overlay_opens_and_closes() -> None:
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        help_text = _plain(app.screen.query_one("#help", Static))
        assert "Quit" in help_text
        assert "Cycle the detail pane" in help_text
        assert "last 10 GitHub requests" in help_text  # the dots legend

        # `q` closes the overlay without quitting the app.
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        assert app.is_running

        # escape works too, and `?` toggles from the keyboard's perspective.
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_open_pr_uses_selected_url(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("tools.my_prs.app.webbrowser.open", opened.append)
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("o")
        assert opened == ["https://github.com/o/r/pull/2"]


# --- goblin-watcher hand-off ---------------------------------------------------


def test_gw_new_pr_command() -> None:
    url = "https://github.com/o/r/pull/2"
    assert gw.new_pr_command(url) == ["gw", "new", "--pr", url]
    assert gw.new_pr_command(url, rm=True) == ["gw", "new", "--pr", url, "--rm"]


def test_gw_parse_exists_matches_collision_error() -> None:
    stderr = (
        "Error: Task 's-feature' already exists in project 'dev-tools'.\n"
        "Hint: Use `gw run s-feature` to resume it. Pass --rm to remove it."
    )
    existing = gw.parse_exists(stderr)
    assert existing == gw.ExistingTask(task_id="s-feature", project="dev-tools")


def test_gw_parse_exists_survives_rich_line_wrapping() -> None:
    # gw prints errors through a rich Console, which wraps at 80 columns when
    # stderr is piped — a long task id splits the message across lines. This
    # is real captured output.
    task_id = "s-" + "x" * 55
    stderr = (
        f"Error: Task '{task_id}' already\n"
        "exists in project 'dev-tools'.\n"
        "Hint: Pass --rm to remove it and start over.\n"
    )
    existing = gw.parse_exists(stderr)
    assert existing == gw.ExistingTask(task_id=task_id, project="dev-tools")
    # error_line joins the wrapped message back together and drops the hint.
    assert gw.error_line(stderr) == (
        f"Task '{task_id}' already exists in project 'dev-tools'."
    )


def test_gw_parse_exists_ignores_other_errors() -> None:
    # --rm's own refusal on a dirty worktree must NOT read as a collision,
    # or the app would loop offering --rm forever.
    dirty = "Error: Existing task 's-feature' has uncommitted changes in /x.\n"
    assert gw.parse_exists(dirty) is None
    assert gw.parse_exists("Error: No registered project matches the PR's repo.") is None


def test_gw_classify() -> None:
    assert gw.classify(0, "") == gw.GwLaunch(ok=True)

    exists = gw.classify(1, "Error: Task 'a' already exists in project 'b'.")
    assert exists.ok is False
    assert exists.exists == gw.ExistingTask(task_id="a", project="b")
    assert exists.error == "Task 'a' already exists in project 'b'."

    other = gw.classify(1, "Error: something broke\nHint: try again\n")
    assert other.exists is None
    assert other.error == "something broke"

    silent = gw.classify(1, "")
    assert silent.error == "gw failed with no error output."


async def test_open_in_gw_runs_gw_new(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        calls.append((url, rm))
        return gw.GwLaunch(ok=True)

    monkeypatch.setattr("tools.my_prs.gw.run_new", fake_run_new)
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The attention-needing PR (example-org/other#2) is selected first.
        await pilot.press("g")
        await pilot.pause()
        assert calls == [("https://github.com/o/r/pull/2", False)]
        assert app.activity_log[-1].message.startswith("gw: created task")
        assert "example-org/other#2" in app.activity_log[-1].message


async def test_open_in_gw_existing_task_confirms_then_retries_with_rm(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    exists = gw.ExistingTask(task_id="s-feature", project="dev-tools")

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        calls.append((url, rm))
        if rm:
            return gw.GwLaunch(ok=True)
        return gw.GwLaunch(ok=False, exists=exists, error="Task exists")

    monkeypatch.setattr("tools.my_prs.gw.run_new", fake_run_new)
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, GwRmScreen)
        content = _plain(app.screen.query_one("#gw-exists", Static))
        assert "s-feature" in content
        assert "dev-tools" in content

        await pilot.press("y")
        await pilot.pause()
        assert not isinstance(app.screen, GwRmScreen)
        assert calls == [
            ("https://github.com/o/r/pull/2", False),
            ("https://github.com/o/r/pull/2", True),
        ]
        assert app.activity_log[-1].message.startswith("gw: created task")


async def test_open_in_gw_existing_task_declined_keeps_it(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    exists = gw.ExistingTask(task_id="s-feature", project="dev-tools")

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        calls.append((url, rm))
        return gw.GwLaunch(ok=False, exists=exists, error="Task exists")

    monkeypatch.setattr("tools.my_prs.gw.run_new", fake_run_new)
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, GwRmScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, GwRmScreen)
        assert calls == [("https://github.com/o/r/pull/2", False)]
        assert "kept existing task" in app.activity_log[-1].message


async def test_open_in_gw_rm_failure_does_not_reprompt(monkeypatch) -> None:
    # A collision surviving --rm (e.g. gw refused a dirty worktree) must land
    # in the log as an error, never reopen the confirm dialog.
    exists = gw.ExistingTask(task_id="s-feature", project="dev-tools")

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        if rm:
            return gw.GwLaunch(ok=False, error="has uncommitted changes")
        return gw.GwLaunch(ok=False, exists=exists, error="Task exists")

    monkeypatch.setattr("tools.my_prs.gw.run_new", fake_run_new)
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert not isinstance(app.screen, GwRmScreen)
        assert app.activity_log[-1].level == "error"
        assert "uncommitted changes" in app.activity_log[-1].message


async def test_open_in_gw_error_is_logged(monkeypatch) -> None:
    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        return gw.GwLaunch(ok=False, error="No registered project matches the PR's repo.")

    monkeypatch.setattr("tools.my_prs.gw.run_new", fake_run_new)
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert app.activity_log[-1].level == "error"
        assert "No registered project" in app.activity_log[-1].message


async def test_open_in_gw_without_selection_is_a_noop(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "tools.my_prs.gw.run_new",
        lambda url, *, rm=False: calls.append(url) or gw.GwLaunch(ok=True),
    )
    app = MyPrsApp(poll=lambda: (_data(mine=[], review=[]), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert calls == []


# --- window layout: resizing + persistence ------------------------------------


def test_layout_from_dict_defaults_bad_values() -> None:
    assert layout.from_dict(None) == layout.Layout()
    assert layout.from_dict({}) == layout.Layout()
    assert layout.from_dict({"detail_mode": "sideways", "split": "wide"}) == layout.Layout()
    # An unknown view falls back to the default, like the other fields.
    assert layout.from_dict({"view": "everything"}).view == "mine"
    assert layout.from_dict({"view": "review"}).view == "review"
    # Booleans are ints in Python; they must not sneak in as a split.
    assert layout.from_dict({"split": True}).split == layout.SPLIT_DEFAULT
    # Out-of-range splits are clamped, not rejected.
    assert layout.from_dict({"split": 5}).split == layout.SPLIT_MIN
    assert layout.from_dict({"split": 95}).split == layout.SPLIT_MAX


def test_layout_save_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "sub" / "layout.json"  # parent dir is created on save
    saved = layout.Layout(detail_mode="below", split=35, view="review")
    layout.save(saved, path)
    assert layout.load(path) == saved


def test_layout_load_missing_or_corrupt_file(tmp_path) -> None:
    assert layout.load(tmp_path / "nope.json") == layout.Layout()
    corrupt = tmp_path / "layout.json"
    corrupt.write_text("{not json")
    assert layout.load(corrupt) == layout.Layout()


async def test_resize_moves_divider_and_saves(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        width_before = table.size.width
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app._split == layout.SPLIT_DEFAULT + layout.SPLIT_STEP
        assert table.size.width > width_before
        assert layout.load(path).split == app._split

        # `[` steps back, and the divider never walks past the bounds.
        for _ in range(30):
            await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app._split == layout.SPLIT_MIN
        assert layout.load(path) == layout.Layout(detail_mode="right", split=layout.SPLIT_MIN)


async def test_resize_is_noop_when_detail_hidden(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("d", "d")  # right -> below -> hidden
        await pilot.pause()
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app._split == layout.SPLIT_DEFAULT
        assert layout.load(path) == layout.Layout(detail_mode="hidden")


async def test_layout_restored_on_next_launch(tmp_path) -> None:
    path = tmp_path / "layout.json"
    layout.save(layout.Layout(detail_mode="below", split=30), path)

    app = MyPrsApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        body = app.query_one("#body")
        assert body.has_class("detail-below")
        assert app._split == 30
        # The saved split lands on the vertical axis in below mode: the list
        # window gets ~30% of the body's height.
        table = app.query_one(DataTable)
        assert table.size.height < body.size.height // 2


async def test_app_without_layout_path_never_persists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = MyPrsApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d", "right_square_bracket")
        await pilot.pause()
    assert not (tmp_path / "my-prs").exists()
