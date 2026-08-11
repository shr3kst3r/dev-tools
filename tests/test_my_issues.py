"""Tests for the pure layers of my-issues (query building, parsing, sorting,
hiding, list/detail rendering) plus the master/detail Textual app.

Two ADR constraints are enforced here on purpose, so that re-adding what they
forbid fails a test rather than shipping:

- `docs/adrs/2026-08-11-issues-get-no-attention-dot.md` — no attention column,
  no verdict property on `IssueItem`, recency-only sort.
- `docs/adrs/2026-08-11-my-issues-copies-the-my-prs-shell.md` — state files live
  under `$XDG_CONFIG_HOME/my-issues/`, never `my-prs/`.

Fixtures use `example-org` and anonymised logins; the captured API shapes are
real in structure only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.console import Console
from textual.widgets import DataTable, Static

from tools.my_issues import gw, hidden, layout, ui
from tools.my_issues.app import (
    POLL_HISTORY_LIMIT,
    GwRmScreen,
    HelpScreen,
    LogScreen,
    MyIssuesApp,
)
from tools.my_issues.cli import _parse_args
from tools.my_issues.github import (
    GitHubError,
    PollError,
    build_search_query,
    classify_github_error,
    parse_issue,
    parse_search,
)
from tools.my_issues.models import (
    VIEWS,
    Issue,
    IssueComment,
    IssueItem,
    Label,
    partition_hidden,
    sort_items,
)

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _make_issue(
    number: int = 1,
    *,
    title: str = "A title",
    author: str = "alice",
    state_reason: str | None = None,
    body: str = "Something is broken.",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    milestone: str | None = None,
    comment_count: int = 0,
    reactions: int = 0,
    labels: list[Label] | None = None,
    assignees: list[str] | None = None,
    comments: list[IssueComment] | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        url=f"https://github.com/example-org/dev-tools/issues/{number}",
        author=author,
        state="OPEN",
        state_reason=state_reason,
        body=body,
        created_at=created_at or NOW - timedelta(days=2),
        updated_at=updated_at or NOW,
        milestone=milestone,
        comment_count=comment_count,
        reactions=reactions,
        labels=labels if labels is not None else [],
        assignees=assignees if assignees is not None else [],
        comments=comments if comments is not None else [],
    )


def _item(issue: Issue, repo: str = "example-org/dev-tools") -> IssueItem:
    return IssueItem(repo=repo, issue=issue)


# --- search query --------------------------------------------------------


def test_build_search_query_assigned_view() -> None:
    q = build_search_query(14, now=NOW)
    assert q == "is:issue is:open assignee:@me updated:>=2026-07-28 sort:updated-desc"


def test_build_search_query_created_view() -> None:
    q = build_search_query(14, now=NOW, view="created")
    assert q == "is:issue is:open author:@me updated:>=2026-07-28 sort:updated-desc"


def test_build_search_query_mentioned_view() -> None:
    q = build_search_query(14, now=NOW, view="mentioned")
    assert q == "is:issue is:open mentions:@me updated:>=2026-07-28 sort:updated-desc"


def test_build_search_query_custom_user_fills_every_qualifier() -> None:
    # --user is not --author: the same login fills all three qualifiers.
    assert "assignee:alice" in build_search_query(7, now=NOW, user="alice")
    assert "author:alice" in build_search_query(7, now=NOW, user="alice", view="created")
    assert "mentions:alice" in build_search_query(
        7, now=NOW, user="alice", view="mentioned"
    )
    # The window follows --days.
    assert "updated:>=2026-08-04" in build_search_query(7, now=NOW)


# --- error classification -------------------------------------------------


def test_classify_rate_limit_is_concise_and_backs_off() -> None:
    # The raw error embeds the whole gh command; classification must drop it.
    raw = GitHubError(
        "`gh api graphql -f query=fragment IssueFields on Issue {…` failed: "
        "gh: API rate limit exceeded for user ID 1."
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


def test_fetch_all_views_is_one_request_carrying_all_three(monkeypatch) -> None:
    import json

    from tools.my_issues import github as gh

    calls: list[list[str]] = []

    def fake_run(args: list[str], cwd) -> str:
        calls.append(args)
        return json.dumps(
            {
                "data": {
                    "assigned": {"nodes": [_search_node(1)]},
                    "created": {"nodes": [_search_node(2), _search_node(3)]},
                    "mentioned": {"nodes": []},
                }
            }
        )

    monkeypatch.setattr(gh, "_run", fake_run)
    views = gh.fetch_all_views(days=14, limit=50)
    assert [i.issue.number for i in views["assigned"]] == [1]
    assert [i.issue.number for i in views["created"]] == [2, 3]
    assert views["mentioned"] == []

    # Exactly one gh graphql call, carrying all three searches. Splitting this
    # into three requests would triple the per-poll rate-limit cost.
    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "api", "graphql"]
    joined = " ".join(calls[0])
    assert "assigned=" in joined
    assert "created=" in joined
    assert "mentioned=" in joined
    assert "limit=50" in joined


# --- parsing --------------------------------------------------------------


def _search_node(number: int = 7) -> dict:
    """One `search(type: ISSUE)` hit, in the shape the live API returns.

    Anonymised: the org, repo and logins are placeholders. Note `color` is six
    bare hex digits with no leading `#`, and `stateReason` is null on an
    ordinary open issue.
    """
    return {
        "repository": {"nameWithOwner": "example-org/dev-tools"},
        "number": number,
        "title": "Add a my-issues dashboard",
        "url": f"https://github.com/example-org/dev-tools/issues/{number}",
        "state": "OPEN",
        "stateReason": None,
        "body": "It would be nice to see issues the way my-prs shows PRs.",
        "author": {"login": "alice"},
        "createdAt": "2026-08-04T00:00:00Z",
        "updatedAt": "2026-08-10T00:00:00Z",
        "milestone": {"title": "v1"},
        "reactions": {"totalCount": 3},
        "labels": {"nodes": [{"name": "enhancement", "color": "a2eeef"}]},
        "assignees": {"nodes": [{"login": "bob"}]},
        "comments": {
            "totalCount": 5,
            "nodes": [
                {
                    "author": {"login": "bob"},
                    "body": "On it.",
                    "createdAt": "2026-08-09T00:00:00Z",
                    "url": "https://github.com/example-org/dev-tools/issues/7#issuecomment-1",
                }
            ],
        },
    }


def test_parse_search_wraps_the_repo() -> None:
    items = parse_search([_search_node()])
    assert len(items) == 1
    item = items[0]
    assert item.repo == "example-org/dev-tools"
    assert item.repo_name == "dev-tools"
    assert item.key == "example-org/dev-tools#7"
    assert item.issue.number == 7


def test_parse_issue_reads_labels_assignees_and_milestone() -> None:
    issue = parse_issue(_search_node())
    assert issue.labels == [Label(name="enhancement", color="a2eeef")]
    assert issue.assignees == ["bob"]
    assert issue.milestone == "v1"
    assert issue.reactions == 3
    assert issue.state == "OPEN"
    assert issue.state_reason is None
    assert issue.author == "alice"
    assert issue.created_at == datetime(2026, 8, 4, tzinfo=timezone.utc)
    assert issue.updated_at == datetime(2026, 8, 10, tzinfo=timezone.utc)


def test_parse_issue_keeps_the_comment_tail_and_the_real_total() -> None:
    issue = parse_issue(_search_node())
    # The query fetches only the tail; totalCount is the whole thread.
    assert issue.comment_count == 5
    assert len(issue.comments) == 1
    assert issue.comments[0] == IssueComment(
        author="bob",
        body="On it.",
        created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        url="https://github.com/example-org/dev-tools/issues/7#issuecomment-1",
    )


def test_parse_issue_handles_empty_body_null_author_and_no_milestone() -> None:
    node = _search_node()
    node["body"] = ""  # GitHub returns "", not null, for an empty issue
    node["author"] = None  # a ghosted account
    node["milestone"] = None
    node["labels"] = {"nodes": []}
    node["assignees"] = {"nodes": []}
    node["comments"] = {"totalCount": 0, "nodes": []}
    node["reactions"] = None
    issue = parse_issue(node)
    assert issue.body == ""
    assert issue.author == "unknown"
    assert issue.milestone is None
    assert issue.labels == []
    assert issue.assignees == []
    assert issue.comments == []
    assert issue.comment_count == 0
    assert issue.reactions == 0


def test_parse_issue_reads_a_reopened_state_reason() -> None:
    node = _search_node()
    node["stateReason"] = "REOPENED"
    assert _item(parse_issue(node)).reopened is True


def test_parse_search_skips_non_issue_nodes() -> None:
    # search(type: ISSUE) spans PRs too; a PR hit comes back as {} because the
    # `... on Issue` fragment doesn't match.
    items = parse_search([{}, _search_node(), {}])
    assert [i.issue.number for i in items] == [7]


# --- the item model -------------------------------------------------------


def test_issue_item_key_and_repo_name() -> None:
    item = _item(_make_issue(4), repo="example-org/backend")
    assert item.key == "example-org/backend#4"
    assert item.repo_name == "backend"
    # A repo with no owner still yields something usable.
    assert IssueItem(repo="solo", issue=_make_issue(1)).repo_name == "solo"


def test_issue_item_reopened_and_unassigned_are_facts() -> None:
    assert _item(_make_issue(state_reason="REOPENED")).reopened is True
    assert _item(_make_issue(state_reason=None)).reopened is False
    assert _item(_make_issue(assignees=[])).unassigned is True
    assert _item(_make_issue(assignees=["alice"])).unassigned is False


def test_issue_item_has_no_attention_verdict() -> None:
    """ADR-constrained: `docs/adrs/2026-08-11-issues-get-no-attention-dot.md`.

    my-issues reports facts, never a judgment about whether an issue wants you.
    If you are here because this test failed, you are adding an attention
    heuristic — supersede that ADR first.
    """
    item = _item(_make_issue(comment_count=9, assignees=[]))
    for forbidden in ("needs_attention", "ready", "failing", "review_gap"):
        assert not hasattr(item, forbidden), f"{forbidden} is ADR-forbidden"
        assert forbidden not in dir(IssueItem)


# --- sorting ---------------------------------------------------------------


def test_sort_is_pure_recency_newest_first() -> None:
    old = _item(_make_issue(1, updated_at=NOW - timedelta(days=3)))
    new = _item(_make_issue(2, updated_at=NOW))
    middle = _item(_make_issue(3, updated_at=NOW - timedelta(hours=5)))
    assert [i.issue.number for i in sort_items([old, new, middle])] == [2, 3, 1]


def test_sort_puts_a_missing_timestamp_last() -> None:
    # Built directly rather than through _make_issue, whose default fills in a
    # timestamp — the point here is an issue that genuinely has none.
    dated = _item(_make_issue(1, updated_at=NOW - timedelta(days=400)))
    undated = _item(Issue(number=2, updated_at=None))
    assert [i.issue.number for i in sort_items([undated, dated])] == [1, 2]


def test_sort_does_not_promote_a_noisy_old_issue() -> None:
    """ADR-constrained: recency only, no attention or popularity term.

    A loud, unassigned, heavily labeled old issue must still sink below a quiet
    new one — sorting it first is exactly the invented urgency
    `docs/adrs/2026-08-11-issues-get-no-attention-dot.md` rejects.
    """
    noisy_old = _item(
        _make_issue(
            1,
            updated_at=NOW - timedelta(days=6),
            comment_count=42,
            reactions=30,
            assignees=[],
            labels=[Label("bug", "d73a4a"), Label("priority:high", "b60205")],
        )
    )
    quiet_new = _item(_make_issue(2, updated_at=NOW, assignees=["alice"]))
    assert [i.issue.number for i in sort_items([noisy_old, quiet_new])] == [2, 1]


# --- the hide list ------------------------------------------------------------


def test_hidden_state_path_is_under_my_issues_not_my_prs(monkeypatch, tmp_path) -> None:
    """ADR-constrained: each tool's state lives in its own directory.

    The hide-list keys are `owner/repo#number` for an issue *and* for a PR, so
    sharing the file would silently clobber my-prs' hide list.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = hidden.state_path()
    assert path == tmp_path / "my-issues" / "hidden.json"
    assert "my-prs" not in str(path)


def test_hidden_from_dict_reads_keys_and_times() -> None:
    parsed = hidden.from_dict({"hidden": {"example-org/r#1": "2026-08-11T12:00:00+00:00"}})
    assert parsed == {"example-org/r#1": NOW}


def test_hidden_from_dict_normalizes_naive_timestamps_to_utc() -> None:
    parsed = hidden.from_dict({"hidden": {"example-org/r#1": "2026-08-11T12:00:00"}})
    assert parsed["example-org/r#1"] == NOW


def test_hidden_from_dict_accepts_a_bare_list_of_keys() -> None:
    # A hand-edited file is a supported way to hide something.
    parsed = hidden.from_dict({"hidden": ["example-org/r#1", "example-org/r#2"]})
    assert sorted(parsed) == ["example-org/r#1", "example-org/r#2"]


def test_hidden_from_dict_shrugs_off_junk() -> None:
    assert hidden.from_dict("nope") == {}
    assert hidden.from_dict({"hidden": 7}) == {}
    # An unusable timestamp keeps the entry — the key is the part that matters.
    assert hidden.from_dict({"hidden": {"example-org/r#1": "nope"}}).keys() == {
        "example-org/r#1"
    }


def test_hidden_to_dict_is_sorted_and_iso() -> None:
    assert hidden.to_dict({"example-org/r#2": NOW, "example-org/r#1": NOW}) == {
        "hidden": {
            "example-org/r#1": "2026-08-11T12:00:00+00:00",
            "example-org/r#2": "2026-08-11T12:00:00+00:00",
        }
    }


def test_hidden_load_returns_empty_for_missing_or_broken_file(tmp_path) -> None:
    assert hidden.load(tmp_path / "nope.json") == {}
    broken = tmp_path / "hidden.json"
    broken.write_text("{not json")
    assert hidden.load(broken) == {}


def test_hidden_roundtrips_through_a_file(tmp_path) -> None:
    path = tmp_path / "sub" / "hidden.json"  # parent dir is created on save
    hidden.save({"example-org/r#1": NOW}, path)
    assert hidden.load(path) == {"example-org/r#1": NOW}


# --- partitioning the hidden issues out ---------------------------------------


def _poll_data() -> dict[str, list[IssueItem]]:
    return {
        "assigned": [
            _item(_make_issue(1)),
            _item(_make_issue(2), repo="example-org/other"),
        ],
        "created": [_item(_make_issue(9), repo="example-org/backend")],
        "mentioned": [_item(_make_issue(12), repo="example-org/frontend")],
    }


def test_partition_hidden_moves_an_issue_into_the_hidden_view() -> None:
    views = partition_hidden(_poll_data(), {"example-org/other#2": NOW})
    assert [i.key for i in views["assigned"]] == ["example-org/dev-tools#1"]
    assert [i.key for i in views["created"]] == ["example-org/backend#9"]
    assert [i.key for i in views["mentioned"]] == ["example-org/frontend#12"]
    assert [i.key for i in views["hidden"]] == ["example-org/other#2"]


def test_partition_hidden_leaves_everything_alone_when_nothing_is_hidden() -> None:
    views = partition_hidden(_poll_data(), {})
    assert len(views["assigned"]) == 2
    assert views["hidden"] == []
    assert set(views) == set(VIEWS)


def test_partition_hidden_orders_newest_hidden_first() -> None:
    views = partition_hidden(
        _poll_data(),
        {
            "example-org/dev-tools#1": NOW - timedelta(days=1),
            "example-org/other#2": NOW,
        },
    )
    # The one you just dismissed sits at the top, ready to be put back.
    assert [i.key for i in views["hidden"]] == [
        "example-org/other#2",
        "example-org/dev-tools#1",
    ]


def test_partition_hidden_lists_an_issue_once_even_in_two_source_views() -> None:
    # Real and common: you filed it and it was assigned to you.
    item = _item(_make_issue(9), repo="example-org/backend")
    views = partition_hidden(
        {"assigned": [item], "created": [item], "mentioned": []},
        {"example-org/backend#9": NOW},
    )
    assert [i.key for i in views["hidden"]] == ["example-org/backend#9"]


def test_partition_hidden_ignores_keys_this_poll_did_not_return() -> None:
    # A closed issue (or one that aged out of the window) has nothing to show,
    # but its entry stays on the list — this must not invent a row or blow up.
    views = partition_hidden(_poll_data(), {"example-org/gone#404": NOW})
    assert views["hidden"] == []
    assert len(views["assigned"]) == 2


# --- layout state -------------------------------------------------------------


def test_layout_state_path_is_under_my_issues_not_my_prs(monkeypatch, tmp_path) -> None:
    """ADR-constrained, same reason as the hide list's path test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = layout.state_path()
    assert path == tmp_path / "my-issues" / "layout.json"
    assert "my-prs" not in str(path)


def test_layout_from_dict_defaults_bad_values() -> None:
    assert layout.from_dict(None) == layout.Layout()
    assert layout.from_dict({}) == layout.Layout()
    assert layout.from_dict({"detail_mode": "sideways", "split": "wide"}) == layout.Layout()
    # Booleans are ints in Python; they must not sneak in as a split.
    assert layout.from_dict({"split": True}).split == layout.SPLIT_DEFAULT
    # Out-of-range splits are clamped, not rejected.
    assert layout.from_dict({"split": 5}).split == layout.SPLIT_MIN
    assert layout.from_dict({"split": 95}).split == layout.SPLIT_MAX


def test_layout_validates_view_against_the_four_issue_views() -> None:
    assert layout.Layout().view == "assigned"
    for view in VIEWS:
        assert layout.from_dict({"view": view}).view == view
    # my-prs' view names are not my-issues' — they fall back to the default.
    assert layout.from_dict({"view": "mine"}).view == "assigned"
    assert layout.from_dict({"view": "review"}).view == "assigned"
    assert layout.from_dict({"view": "everything"}).view == "assigned"


def test_layout_save_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "sub" / "layout.json"  # parent dir is created on save
    saved = layout.Layout(detail_mode="below", split=35, view="mentioned")
    layout.save(saved, path)
    assert layout.load(path) == saved


def test_layout_load_missing_or_corrupt_file(tmp_path) -> None:
    assert layout.load(tmp_path / "nope.json") == layout.Layout()
    corrupt = tmp_path / "layout.json"
    corrupt.write_text("{not json")
    assert layout.load(corrupt) == layout.Layout()


# --- list rendering ---------------------------------------------------------


def test_no_view_has_an_attention_column() -> None:
    """ADR-constrained: `docs/adrs/2026-08-11-issues-get-no-attention-dot.md`.

    my-prs opens on a `!` dot column. my-issues has none, in any view.
    """
    for view in VIEWS:
        columns = ui.list_columns(view)
        assert "!" not in columns, f"{view} grew an attention column"
        assert "⚠" not in columns


def test_people_columns_follow_the_might_not_be_you_rule() -> None:
    # Author where the filer might not be you…
    for view in ("assigned", "mentioned", "hidden"):
        assert "Author" in ui.list_columns(view)
    assert "Author" not in ui.list_columns("created")  # they're all yours
    # …Assignees where the assignee might not be you.
    for view in ("created", "mentioned", "hidden"):
        assert "Assignees" in ui.list_columns(view)
    assert "Assignees" not in ui.list_columns("assigned")  # it's you, every row


def test_labels_cell_colors_each_label_from_its_own_hex() -> None:
    item = _item(
        _make_issue(labels=[Label("bug", "d73a4a"), Label("docs", "0075ca")])
    )
    cell = ui.labels_cell(item)
    assert cell.plain == "bug, docs"
    styles = [span.style for span in cell.spans]
    assert ui.label_style("d73a4a") in styles
    assert ui.label_style("0075ca") in styles


def test_labels_cell_truncates_past_three_and_says_how_many() -> None:
    labels = [Label(f"l{i}", "ededed") for i in range(5)]
    cell = ui.labels_cell(_item(_make_issue(labels=labels)))
    assert cell.plain == "l0, l1, l2 +2"
    assert ui.labels_cell(_item(_make_issue(labels=[]))).plain == "—"


def test_label_style_ignores_a_color_it_cannot_use() -> None:
    from rich.style import Style

    assert ui.label_style("a2eeef") == Style(color="#a2eeef")
    assert ui.label_style("") == Style()
    assert ui.label_style("#a2eeef") == Style()  # already-hashed is not what GitHub sends
    assert ui.label_style("nope") == Style()


def test_assignees_cell_marks_an_unassigned_issue() -> None:
    assert ui.assignees_cell(_item(_make_issue(assignees=[]))).plain == "—"
    assert (
        ui.assignees_cell(_item(_make_issue(assignees=["alice", "bob"]))).plain
        == "alice, bob"
    )


def test_comments_cell_counts_without_judging() -> None:
    assert ui.comments_cell(_item(_make_issue(comment_count=0))).plain == "—"
    assert ui.comments_cell(_item(_make_issue(comment_count=12))).plain == "12"
    # Nothing red/alarming: a busy thread is not a verdict.
    assert "red" not in str(ui.comments_cell(_item(_make_issue(comment_count=12))).style)


def test_title_cell_truncates_and_marks_a_reopened_issue() -> None:
    long_title = "x" * 60
    assert ui._title_cell(_item(_make_issue(title=long_title))).plain.endswith("…")
    assert len(ui._title_cell(_item(_make_issue(title=long_title))).plain) == 44
    reopened = ui._title_cell(_item(_make_issue(title="Back again", state_reason="REOPENED")))
    assert reopened.plain == "Back again ↻"


def _rich_item() -> IssueItem:
    return _item(
        _make_issue(
            7,
            title="Flaky test",
            author="alice",
            created_at=NOW - timedelta(days=5),
            updated_at=NOW - timedelta(hours=3),
            comment_count=4,
            labels=[Label("bug", "d73a4a")],
            assignees=["bob"],
        )
    )


def test_list_row_assigned_view_cells() -> None:
    columns = ui.list_columns("assigned")
    row = ui.list_row(_rich_item(), NOW, "assigned")
    assert len(row) == len(columns)
    assert row[columns.index("Repo")].plain == "dev-tools"
    assert row[columns.index("#")].plain == "#7"
    assert row[columns.index("Author")].plain == "alice"
    assert row[columns.index("Title")].plain == "Flaky test"
    assert row[columns.index("Labels")].plain == "bug"
    assert row[columns.index("💬")].plain == "4"
    assert row[columns.index("Age")].plain == "5d ago"
    assert row[columns.index("Updated")].plain == "3h ago"


def test_list_row_created_view_swaps_author_for_assignees() -> None:
    columns = ui.list_columns("created")
    row = ui.list_row(_rich_item(), NOW, "created")
    assert len(row) == len(columns)
    assert row[columns.index("Assignees")].plain == "bob"
    assert row[columns.index("Title")].plain == "Flaky test"
    assert row[columns.index("Age")].plain == "5d ago"


def test_list_row_mentioned_view_shows_both_people_columns() -> None:
    columns = ui.list_columns("mentioned")
    row = ui.list_row(_rich_item(), NOW, "mentioned")
    assert len(row) == len(columns)
    assert row[columns.index("Author")].plain == "alice"
    assert row[columns.index("Assignees")].plain == "bob"
    assert row[columns.index("Updated")].plain == "3h ago"


def test_list_row_hidden_view_adds_when_you_hid_it() -> None:
    columns = ui.list_columns("hidden")
    row = ui.list_row(_rich_item(), NOW, "hidden", NOW - timedelta(hours=1))
    assert len(row) == len(columns)
    assert row[columns.index("Author")].plain == "alice"
    assert row[columns.index("Assignees")].plain == "bob"
    assert row[columns.index("Hidden")].plain == "1h ago"


# --- summary / status / overlays ----------------------------------------------


def test_render_summary_counts_facts_only() -> None:
    items = [
        _item(_make_issue(1, labels=[Label("bug", "d73a4a")], assignees=["alice"])),
        _item(_make_issue(2, comment_count=3, assignees=[])),
        _item(_make_issue(3, assignees=[])),
    ]
    text = ui.render_summary(items, None, "created").plain
    assert "3 you filed" in text
    assert "🏷 1 labeled" in text
    assert "💬 1 with comments" in text
    assert "◯ 2 unassigned" in text
    # No verdicts anywhere in the bar.
    for verdict in ("need", "ready", "failing", "awaiting"):
        assert verdict not in text.lower()


def test_render_summary_omits_unassigned_on_the_assigned_view() -> None:
    # Vacuous there: every row has you on it.
    text = ui.render_summary([_item(_make_issue(1))], None, "assigned").plain
    assert "1 assigned to you" in text
    assert "unassigned" not in text


def test_render_summary_shows_every_view_tab() -> None:
    text = ui.render_summary([], None).plain
    for label in ("Assigned to me", "I filed", "Mentions me", "Hidden"):
        assert label in text
    assert "0 assigned to you" in text
    assert "1 mentioning you" in ui.render_summary(
        [_item(_make_issue(1))], None, "mentioned"
    ).plain


def test_render_summary_error_and_loading() -> None:
    assert "boom" in ui.render_summary(None, "boom").plain
    assert "Contacting GitHub" in ui.render_summary(None, None).plain


def test_render_summary_notes_the_hide_list_on_the_visible_views() -> None:
    text = ui.render_summary([_item(_make_issue(1))], None, "assigned", hidden_total=3).plain
    assert "⊘ 3 hidden" in text
    # Nothing hidden, nothing said.
    assert "⊘" not in ui.render_summary([_item(_make_issue(1))], None).plain


def test_render_summary_hidden_view_counts_what_it_cannot_show() -> None:
    text = ui.render_summary([_item(_make_issue(1))], None, "hidden", hidden_total=2).plain
    assert "1 hidden" in text
    assert "⊘ 1 not in this window" in text


def test_render_poll_dots_one_colored_dot_per_request() -> None:
    dots = ui.render_poll_dots(["ok", "error", "running"])
    assert dots.plain == "●●●"
    assert [span.style for span in dots.spans] == [
        "bold green",
        "bold red",
        "bold blue",
    ]


def test_render_status_bar_shows_dots_and_a_help_pointer() -> None:
    bar = ui.render_status_bar(NOW, 42, 60, ["ok", "error", "running"], refreshing=True)
    assert "●●●" in bar.plain
    assert "refreshing…" in bar.plain
    assert "? help" in bar.plain


def test_render_status_bar_countdown_and_no_dots_before_first_request() -> None:
    bar = ui.render_status_bar(NOW, 42, 60, [])
    assert "refresh in 42s" in bar.plain
    assert "(every 60s)" in bar.plain
    assert "●" not in bar.plain


def test_render_log_empty_and_with_entries() -> None:
    from tools.my_issues.models import LogEntry

    empty = Console(width=80)
    with empty.capture() as cap:
        empty.print(ui.render_log([]))
    assert "No activity yet" in cap.get()

    entries = [
        LogEntry(time=NOW, level="info", message="Refreshed — 2 assigned · 1 created"),
        LogEntry(time=NOW, level="warn", message="rate limit — backing off"),
    ]
    console = Console(width=80)
    with console.capture() as cap:
        console.print(ui.render_log(entries))
    out = cap.get()
    assert "Refreshed" in out
    assert "rate limit" in out
    assert "Activity log" in out


def test_render_help_explains_the_recency_sort_and_no_dot() -> None:
    console = Console(width=100)
    with console.capture() as cap:
        console.print(ui.render_help())
    out = cap.get()
    assert "Cycle view" in out
    assert "no attention dot" in out


def test_render_detail_placeholder_per_view_empty_states() -> None:
    def text(view: str) -> str:
        console = Console(width=80)
        with console.capture() as cap:
            console.print(ui.render_detail_placeholder([], None, view=view))
        return cap.get()

    assert "No open issues assigned to you" in text("assigned")
    assert "you filed" in text("created")
    assert "mentioning you" in text("mentioned")
    assert "press h" in text("hidden")


def test_render_once_lists_every_issue() -> None:
    items = [
        _item(_make_issue(1, title="First bug")),
        _item(_make_issue(2, title="Second bug", comment_count=2)),
    ]
    console = Console(width=160)
    with console.capture() as capture:
        console.print(ui.render_once(items, NOW))
    output = capture.get()
    assert "First bug" in output
    assert "Second bug" in output
    assert "2 assigned to you" in output


# --- the detail pane ----------------------------------------------------------


def _detail_text(item: IssueItem, width: int = 100) -> str:
    console = Console(width=width)
    with console.capture() as cap:
        console.print(ui.render_body(item, NOW))
    return cap.get()


def test_render_body_shows_the_issue_and_its_facts() -> None:
    item = _item(
        _make_issue(
            7,
            title="Flaky test on CI",
            author="alice",
            body="It fails about one run in five.",
            milestone="v1",
            labels=[Label("bug", "d73a4a")],
            assignees=["bob"],
            comment_count=1,
            reactions=2,
            comments=[
                IssueComment(author="bob", body="Reproduced.", created_at=NOW - timedelta(hours=2))
            ],
        ),
        repo="example-org/dev-tools",
    )
    out = _detail_text(item)
    assert "example-org/dev-tools" in out
    assert "#7" in out
    assert "Flaky test on CI" in out
    assert "@alice" in out
    assert "bug" in out
    assert "bob" in out
    assert "v1" in out
    assert "one run in five" in out
    assert "Reproduced." in out
    assert "2h ago" in out


def test_render_body_notes_the_comments_it_is_not_showing() -> None:
    item = _item(
        _make_issue(
            comment_count=9,
            comments=[
                IssueComment(author="alice", body=f"note {i}", created_at=NOW)
                for i in range(3)
            ],
        )
    )
    out = _detail_text(item)
    assert "+6 earlier" in out  # never implies it has the whole thread
    assert "Comments (9)" in out


def test_render_body_falls_back_for_an_empty_body_and_no_comments() -> None:
    out = _detail_text(_item(_make_issue(body="", comment_count=0, comments=[])))
    assert "No description." in out
    assert "No comments yet." in out


def test_render_body_marks_a_reopened_issue() -> None:
    out = _detail_text(_item(_make_issue(state_reason="REOPENED")))
    assert "reopened" in out


# --- cli -------------------------------------------------------------------


def test_cli_defaults() -> None:
    args = _parse_args([])
    assert args.days == 14
    assert args.interval == 60
    assert args.limit == 50
    assert args.user == "@me"
    assert args.once is False
    assert args.view is None  # None: fall back to the saved layout's view


def test_cli_view_and_user_args() -> None:
    assert _parse_args(["--view", "mentioned"]).view == "mentioned"
    assert _parse_args(["--user", "alice"]).user == "alice"
    assert _parse_args(["--once", "-d", "30", "--limit", "100"]).days == 30


# --- the app ----------------------------------------------------------------


def _plain(widget: Static) -> str:
    """The plain text a Static widget was last updated with."""
    console = Console(width=120)
    with console.capture() as capture:
        console.print(widget.content)
    return capture.get()


def _assigned_fleet() -> list[IssueItem]:
    return sort_items(
        [
            _item(
                _make_issue(
                    1,
                    title="Quiet issue",
                    updated_at=NOW - timedelta(days=1),
                    assignees=["me"],
                )
            ),
            _item(
                _make_issue(
                    2,
                    title="Busy issue",
                    updated_at=NOW,
                    comment_count=4,
                    assignees=["me"],
                ),
                repo="example-org/other",
            ),
        ]
    )


def _created_fleet() -> list[IssueItem]:
    return [_item(_make_issue(9, title="My idea"), repo="example-org/backend")]


def _mentioned_fleet() -> list[IssueItem]:
    return [_item(_make_issue(12, title="Design chat"), repo="example-org/frontend")]


def _data(
    assigned: list[IssueItem] | None = None,
    created: list[IssueItem] | None = None,
    mentioned: list[IssueItem] | None = None,
) -> dict[str, list[IssueItem]]:
    """A poll payload: all three searched views, defaulting to the fixtures."""
    return {
        "assigned": _assigned_fleet() if assigned is None else assigned,
        "created": _created_fleet() if created is None else created,
        "mentioned": _mentioned_fleet() if mentioned is None else mentioned,
    }


async def test_app_lists_issues_and_shows_detail() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        assert table.row_count == 2
        # Most recently updated is first — and selected.
        assert app._selected_key == "example-org/other#2"
        assert "Busy issue" in _plain(app.query_one("#detail", Static))

        # Moving the cursor swaps the detail pane.
        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"
        assert "Quiet issue" in _plain(app.query_one("#detail", Static))


async def test_app_keeps_selection_across_refresh() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
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
    polls: list[tuple[dict[str, list[IssueItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="GitHub exploded")),
    ]
    app = MyIssuesApp(poll=lambda: polls.pop(0), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The stale list stays usable; the error lands in the summary bar.
        assert app.query_one(DataTable).row_count == 2
        assert "GitHub exploded" in _plain(app.query_one("#summary", Static))


async def test_app_backs_off_then_recovers_on_rate_limit() -> None:
    polls: list[tuple[dict[str, list[IssueItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="rate limited", rate_limited=True)),
        (None, PollError(message="rate limited", rate_limited=True)),
        (_data(), None),
    ]
    app = MyIssuesApp(poll=lambda: polls.pop(0), interval=60)
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
    app = MyIssuesApp(
        poll=lambda: (None, PollError("slow down", rate_limited=True, retry_after=99999)),
        interval=60,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # retry_after is honored but capped at the 15-minute ceiling.
        assert app._current_delay == 900


async def test_app_logs_each_poll_with_per_view_counts() -> None:
    polls: list[tuple[dict[str, list[IssueItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="rate limited", rate_limited=True)),
    ]
    app = MyIssuesApp(poll=lambda: polls.pop(0), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(app.activity_log) == 1
        message = app.activity_log[0].message
        assert app.activity_log[0].level == "info"
        for expected in ("2 assigned", "1 created", "1 mentioned", "0 hidden"):
            assert expected in message

        app.action_poll_now()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.activity_log[-1].level == "warn"
        assert "rate limited" in app.activity_log[-1].message


async def test_status_bar_tracks_github_request_history() -> None:
    polls: list[tuple[dict[str, list[IssueItem]] | None, PollError | None]] = [
        (_data(), None),
        (None, PollError(message="boom")),
    ]
    app = MyIssuesApp(poll=lambda: polls.pop(0), interval=60)
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


async def test_poll_history_keeps_only_the_last_ten() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
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
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
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
    app = MyIssuesApp(
        poll=lambda: (_data(assigned=[], created=[], mentioned=[]), None), interval=60
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0
        assert "No open issues assigned to you" in _plain(app.query_one("#detail", Static))


async def test_help_overlay_opens_and_closes() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        help_text = _plain(app.screen.query_one("#help", Static))
        assert "Quit" in help_text
        assert "Cycle the detail pane" in help_text

        # `q` closes the overlay without quitting the app.
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        assert app.is_running

        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_open_issue_uses_selected_url(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("tools.my_issues.app.webbrowser.open", opened.append)
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("o")
        assert opened == ["https://github.com/example-org/dev-tools/issues/2"]


# --- view switching -----------------------------------------------------------


async def test_v_cycles_all_four_views_and_each_remembers_its_cursor() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one(DataTable)
        assert app._view == "assigned"
        assert table.row_count == 2
        # Park the cursor off the top row so we can check it's remembered.
        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"

        await pilot.press("v")
        await pilot.pause()
        assert app._view == "created"
        assert table.row_count == 1
        assert app._selected_key == "example-org/backend#9"
        assert "My idea" in _plain(app.query_one("#detail", Static))
        assert "1 you filed" in _plain(app.query_one("#summary", Static))
        assert len(table.columns) == len(ui.list_columns("created"))

        await pilot.press("v")
        await pilot.pause()
        assert app._view == "mentioned"
        assert table.row_count == 1
        assert app._selected_key == "example-org/frontend#12"
        assert len(table.columns) == len(ui.list_columns("mentioned"))

        await pilot.press("v")
        await pilot.pause()
        assert app._view == "hidden"
        assert table.row_count == 0
        assert len(table.columns) == len(ui.list_columns("hidden"))

        # Fourth press comes back around and restores the first view's cursor.
        await pilot.press("v")
        await pilot.pause()
        assert app._view == "assigned"
        assert app._selected_key == "example-org/dev-tools#1"
        assert table.cursor_row == 1
        assert len(table.columns) == len(ui.list_columns("assigned"))


async def test_mentioned_view_empty_state() -> None:
    app = MyIssuesApp(poll=lambda: (_data(mentioned=[]), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v", "v")  # assigned → created → mentioned
        await pilot.pause()
        assert "mentioning you" in _plain(app.query_one("#detail", Static))


async def test_view_persisted_and_restored(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
    assert layout.load(path).view == "created"

    reopened = MyIssuesApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with reopened.run_test(size=(140, 40)) as pilot:
        await reopened.workers.wait_for_complete()
        await pilot.pause()
        assert reopened._view == "created"
        assert reopened.query_one(DataTable).row_count == 1


async def test_initial_view_overrides_saved(tmp_path) -> None:
    path = tmp_path / "layout.json"
    layout.save(layout.Layout(view="mentioned"), path)
    app = MyIssuesApp(
        poll=lambda: (_data(), None),
        interval=60,
        layout_path=path,
        initial_view="assigned",
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._view == "assigned"


# --- hiding issues -------------------------------------------------------------


async def test_h_hides_the_selected_issue_and_the_hidden_view_holds_it() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one(DataTable)
        assert app._selected_key == "example-org/other#2"

        await pilot.press("h")
        await pilot.pause()
        # It's gone from "assigned", and the cursor moved to what was below it.
        assert table.row_count == 1
        assert app._selected_key == "example-org/dev-tools#1"
        assert "⊘ 1 hidden" in _plain(app.query_one("#summary", Static))

        # …and it's waiting in the hidden view, already under the cursor.
        await pilot.press("v", "v", "v")  # assigned → created → mentioned → hidden
        await pilot.pause()
        assert app._view == "hidden"
        assert table.row_count == 1
        assert app._selected_key == "example-org/other#2"
        assert "Busy issue" in _plain(app.query_one("#detail", Static))


async def test_h_in_the_hidden_view_restores_every_source_view_it_came_from() -> None:
    # An issue you filed *and* were assigned turns up in two source views; when
    # unhidden it must come back to both, and be selected in both.
    shared = _item(_make_issue(5, title="Both views"), repo="example-org/shared")
    app = MyIssuesApp(
        poll=lambda: (_data(assigned=[shared], created=[shared], mentioned=[]), None),
        interval=60,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._selected_key == "example-org/shared#5"

        await pilot.press("h")
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 0

        await pilot.press("v", "v", "v")
        await pilot.pause()
        assert app._view == "hidden"
        assert app.query_one(DataTable).row_count == 1  # listed once, not twice

        await pilot.press("h")
        await pilot.pause()
        assert app._hidden == {}
        assert app.query_one(DataTable).row_count == 0
        assert app._selected["assigned"] == "example-org/shared#5"
        assert app._selected["created"] == "example-org/shared#5"


async def test_hiding_survives_a_refresh() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()

        app.action_poll_now()  # the same issue comes back from GitHub…
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 1  # …and stays hidden
        assert "1 assigned" in app.activity_log[-1].message  # counts are what's shown
        assert "1 hidden" in app.activity_log[-1].message


async def test_hide_list_is_persisted_and_restored(tmp_path) -> None:
    path = tmp_path / "hidden.json"
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60, hidden_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
    assert list(hidden.load(path)) == ["example-org/other#2"]

    reopened = MyIssuesApp(poll=lambda: (_data(), None), interval=60, hidden_path=path)
    async with reopened.run_test(size=(140, 40)) as pilot:
        await reopened.workers.wait_for_complete()
        await pilot.pause()
        assert reopened.query_one(DataTable).row_count == 1
        assert reopened._selected_key == "example-org/dev-tools#1"


async def test_hiding_the_last_row_falls_back_to_the_one_above() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")  # the bottom row
        await pilot.pause()
        assert app._selected_key == "example-org/dev-tools#1"

        await pilot.press("h")
        await pilot.pause()
        assert app._selected_key == "example-org/other#2"


async def test_h_on_an_empty_list_does_nothing() -> None:
    app = MyIssuesApp(
        poll=lambda: (_data(assigned=[], created=[], mentioned=[]), None), interval=60
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert app._hidden == {}
        assert app.is_running


async def test_hidden_view_empty_state_points_at_the_key() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60, initial_view="hidden")
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "press h" in _plain(app.query_one("#detail", Static))


# --- goblin-watcher hand-off ---------------------------------------------------


def test_gw_new_issue_command() -> None:
    url = "https://github.com/example-org/dev-tools/issues/5"
    assert gw.new_issue_command(url) == ["gw", "new", "--issue", url]
    assert gw.new_issue_command(url, rm=True) == ["gw", "new", "--issue", url, "--rm"]


def test_gw_parse_exists_matches_collision_error() -> None:
    stderr = (
        "Error: Task 'issue-5' already exists in project 'dev-tools'.\n"
        "Hint: Use `gw run issue-5` to resume it. Pass --rm to remove it."
    )
    assert gw.parse_exists(stderr) == gw.ExistingTask(
        task_id="issue-5", project="dev-tools"
    )


def test_gw_parse_exists_survives_rich_line_wrapping() -> None:
    # gw prints errors through a rich Console, which wraps at 80 columns when
    # stderr is piped — a long task id splits the message across lines.
    task_id = "issue-" + "x" * 55
    stderr = (
        f"Error: Task '{task_id}' already\n"
        "exists in project 'dev-tools'.\n"
        "Hint: Pass --rm to remove it and start over.\n"
    )
    assert gw.parse_exists(stderr) == gw.ExistingTask(
        task_id=task_id, project="dev-tools"
    )
    # error_line joins the wrapped message back together and drops the hint.
    assert gw.error_line(stderr) == (
        f"Task '{task_id}' already exists in project 'dev-tools'."
    )


def test_gw_parse_exists_ignores_other_errors() -> None:
    # --rm's own refusal on a dirty worktree must NOT read as a collision,
    # or the app would loop offering --rm forever.
    dirty = "Error: Existing task 'issue-5' has uncommitted changes in /x.\n"
    assert gw.parse_exists(dirty) is None
    assert gw.parse_exists("Error: No registered project matches the repo.") is None


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

    monkeypatch.setattr("tools.my_issues.gw.run_new", fake_run_new)
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert calls == [("https://github.com/example-org/dev-tools/issues/2", False)]
        assert app.activity_log[-1].message.startswith("gw: created task")
        assert "example-org/other#2" in app.activity_log[-1].message


async def test_open_in_gw_existing_task_confirms_then_retries_with_rm(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    exists = gw.ExistingTask(task_id="issue-2", project="dev-tools")

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        calls.append((url, rm))
        if rm:
            return gw.GwLaunch(ok=True)
        return gw.GwLaunch(ok=False, exists=exists, error="Task exists")

    monkeypatch.setattr("tools.my_issues.gw.run_new", fake_run_new)
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, GwRmScreen)
        content = _plain(app.screen.query_one("#gw-exists", Static))
        assert "issue-2" in content
        assert "dev-tools" in content
        assert "gw new --issue --rm" in content

        await pilot.press("y")
        await pilot.pause()
        assert not isinstance(app.screen, GwRmScreen)
        url = "https://github.com/example-org/dev-tools/issues/2"
        assert calls == [(url, False), (url, True)]
        assert app.activity_log[-1].message.startswith("gw: created task")


async def test_open_in_gw_existing_task_declined_keeps_it(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    exists = gw.ExistingTask(task_id="issue-2", project="dev-tools")

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        calls.append((url, rm))
        return gw.GwLaunch(ok=False, exists=exists, error="Task exists")

    monkeypatch.setattr("tools.my_issues.gw.run_new", fake_run_new)
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("g")
        await pilot.pause()
        assert isinstance(app.screen, GwRmScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, GwRmScreen)
        assert len(calls) == 1
        assert "kept existing task" in app.activity_log[-1].message


async def test_open_in_gw_rm_failure_does_not_reprompt(monkeypatch) -> None:
    # A collision surviving --rm (e.g. gw refused a dirty worktree) must land
    # in the log as an error, never reopen the confirm dialog.
    exists = gw.ExistingTask(task_id="issue-2", project="dev-tools")

    def fake_run_new(url: str, *, rm: bool = False) -> gw.GwLaunch:
        if rm:
            return gw.GwLaunch(ok=False, error="has uncommitted changes")
        return gw.GwLaunch(ok=False, exists=exists, error="Task exists")

    monkeypatch.setattr("tools.my_issues.gw.run_new", fake_run_new)
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
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
        return gw.GwLaunch(ok=False, error="No registered project matches the repo.")

    monkeypatch.setattr("tools.my_issues.gw.run_new", fake_run_new)
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
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
        "tools.my_issues.gw.run_new",
        lambda url, *, rm=False: calls.append(url) or gw.GwLaunch(ok=True),
    )
    app = MyIssuesApp(
        poll=lambda: (_data(assigned=[], created=[], mentioned=[]), None), interval=60
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        assert calls == []


# --- window layout: resizing + persistence ------------------------------------


async def test_cycle_detail_moves_then_hides_pane() -> None:
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        body = app.query_one("#body")
        detail_scroll = app.query_one("#detail-scroll")
        table = app.query_one(DataTable)
        assert not body.has_class("detail-below")
        assert not body.has_class("detail-hidden")

        await pilot.press("d")
        await pilot.pause()
        assert body.has_class("detail-below")
        assert detail_scroll.display
        assert table.size.height < body.size.height

        await pilot.press("d")
        await pilot.pause()
        assert body.has_class("detail-hidden")
        assert not detail_scroll.display
        assert table.size.width == app.size.width

        await pilot.press("d")
        await pilot.pause()
        assert not body.has_class("detail-below")
        assert not body.has_class("detail-hidden")
        assert table.size.width < app.size.width


async def test_resize_moves_divider_and_saves(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
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
        assert layout.load(path) == layout.Layout(
            detail_mode="right", split=layout.SPLIT_MIN
        )


async def test_resize_is_noop_when_detail_hidden(tmp_path) -> None:
    path = tmp_path / "layout.json"
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
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

    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60, layout_path=path)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        body = app.query_one("#body")
        assert body.has_class("detail-below")
        assert app._split == 30
        table = app.query_one(DataTable)
        assert table.size.height < body.size.height // 2


async def test_app_without_state_paths_never_persists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = MyIssuesApp(poll=lambda: (_data(), None), interval=60)
    async with app.run_test(size=(140, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("d", "right_square_bracket", "h")
        await pilot.pause()
    # Neither its own directory nor — critically — my-prs'.
    assert not (tmp_path / "my-issues").exists()
    assert not (tmp_path / "my-prs").exists()
