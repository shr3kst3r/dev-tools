"""Data model for the my-issues dashboard.

One `IssueItem` per issue in the list, wrapping an `Issue` with the repo it
lives in. Unlike my-prs there is nothing to borrow from pr-watch here: an issue
has no head branch, no checks, no review decision and no mergeability, so
`PullRequest` and friends are simply the wrong shape (see
`docs/adrs/2026-08-11-my-issues-copies-the-my-prs-shell.md`).

**There is deliberately no attention/verdict property on `IssueItem`.** GitHub
tells us facts about an issue — labels, assignees, comments, timestamps — and
none of them answer "does this need you", so the dashboard reports the facts and
sorts by recency alone. That is a recorded decision, not an omission: see
`docs/adrs/2026-08-11-issues-get-no-attention-dot.md` before adding one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

# The views GitHub is actually searched for (see github.fetch_all_views).
SOURCE_VIEWS = ("assigned", "created", "mentioned")

# The dashboard's views, in the order `v` cycles through: issues assigned to
# you, issues you filed, issues that mention you, and the ones you've hidden.
# "hidden" is not a search — it's whatever the source views turned up that's on
# the hide list (see hidden.py), which is also why hiding takes effect without
# a poll.
VIEWS = ("assigned", "created", "mentioned", "hidden")
VIEW_LABELS = {
    "assigned": "Assigned to me",
    "created": "I filed",
    "mentioned": "Mentions me",
    "hidden": "Hidden",
}


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One line in the activity log: what a background poll did and when.

    `level` is one of "info" (a normal refresh), "warn" (rate-limit backoff),
    or "error" (a failed poll). The dashboard's `l` overlay renders these.
    """

    time: datetime
    level: str
    message: str


@dataclass(frozen=True, slots=True)
class Label:
    """One of an issue's labels. `color` is GitHub's bare 6-digit hex, with no
    leading `#` — the renderer adds it."""

    name: str
    color: str = ""


@dataclass(frozen=True, slots=True)
class IssueComment:
    """One comment from the tail of an issue's thread."""

    author: str
    body: str
    created_at: datetime | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Issue:
    """A GitHub issue, as much of it as one search result carries.

    `comments` is only the *tail* of the thread (the last few); `comment_count`
    is the real total, so the detail pane can say how much it isn't showing.
    """

    number: int
    title: str = ""
    url: str = ""
    author: str = "unknown"
    state: str = "OPEN"
    state_reason: str | None = None
    body: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    milestone: str | None = None
    comment_count: int = 0
    reactions: int = 0
    labels: list[Label] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    comments: list[IssueComment] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IssueItem:
    """One issue in the dashboard list.

    Carries no `needs_attention`, `ready`, or other verdict — see the module
    docstring and the no-attention-dot ADR.
    """

    repo: str  # "owner/name"
    issue: Issue

    @property
    def key(self) -> str:
        """Stable identity across refreshes (keeps the selection in place), and
        the hide list's key. Same shape as my-prs' — which is exactly why the
        two tools keep their state in separate directories."""
        return f"{self.repo}#{self.issue.number}"

    @property
    def repo_name(self) -> str:
        return self.repo.partition("/")[2] or self.repo

    @property
    def reopened(self) -> bool:
        """Closed once and opened again. A fact, not a verdict — worth marking
        because it usually means the first fix didn't hold."""
        return self.issue.state_reason == "REOPENED"

    @property
    def unassigned(self) -> bool:
        """Nobody is holding it. Reported as a count in the summary bar; it is
        a triage fact about the repo, and never sorts the list."""
        return not self.issue.assignees


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def sort_items(items: list[IssueItem]) -> list[IssueItem]:
    """Most recently updated first, and nothing else.

    No attention term, no popularity term: an issue with 40 comments does not
    outrank the quiet one someone just assigned to you. Issues with no
    timestamp at all sort last. Recency-only is ADR-constrained — see
    `docs/adrs/2026-08-11-issues-get-no-attention-dot.md`.
    """

    def key(item: IssueItem) -> float:
        return -(item.issue.updated_at or _EPOCH).timestamp()

    return sorted(items, key=key)


def partition_hidden(
    data: Mapping[str, list[IssueItem]], hidden: Mapping[str, datetime]
) -> dict[str, list[IssueItem]]:
    """Split a poll's source views into what to show and what's hidden.

    Returns every view in `VIEWS`: each source view with its hidden issues
    taken out, plus a "hidden" view holding the ones that were taken out
    (deduped — an issue you filed and were assigned turns up in two source
    views). Pure, so the dashboard can re-derive its lists the instant you
    press `h`, with no poll.

    Hidden issues are ordered by when you hid them, newest first, so the one
    you just dismissed is at the top if you want it back. Hide-list entries for
    issues this poll didn't return (closed, or simply outside the day window)
    have nothing to show and are silently absent — they stay on the list, and
    reappear here if the issue does.
    """
    shown = {
        view: [item for item in items if item.key not in hidden]
        for view, items in data.items()
    }
    seen: set[str] = set()
    hidden_items: list[IssueItem] = []
    for items in data.values():
        for item in items:
            if item.key in hidden and item.key not in seen:
                seen.add(item.key)
                hidden_items.append(item)
    hidden_items.sort(
        key=lambda item: (
            -hidden[item.key].timestamp(),
            -(item.issue.updated_at or _EPOCH).timestamp(),
        )
    )
    return {**shown, "hidden": hidden_items}
