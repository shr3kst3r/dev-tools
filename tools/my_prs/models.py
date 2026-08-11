"""Data model for the my-prs dashboard.

One `PrItem` per PR in the list. The PR payload itself reuses pr-watch's
`PullRequest` model (same GraphQL shape, same pure parser) — this module only
adds what a *cross-repo list* needs: which repo/branch each PR lives in, and
the "does this need me?" flags the dashboard sorts and colors by.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from tools.pr_watch.models import CheckState, PullRequest, RepoContext

# The views GitHub is actually searched for (see github.fetch_all_views).
SOURCE_VIEWS = ("mine", "review")

# The dashboard's views, in the order `v` cycles through: PRs you authored,
# PRs waiting on a review from you, and the ones you've hidden. "hidden" is
# not a search — it's whatever the source views turned up that's on the hide
# list (see hidden.py), which is also why hiding takes effect without a poll.
VIEWS = ("mine", "review", "hidden")
VIEW_LABELS = {
    "mine": "My PRs",
    "review": "Needs my review",
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
class PrItem:
    """One PR in the dashboard list."""

    repo: str  # "owner/name"
    branch: str
    pr: PullRequest

    @property
    def key(self) -> str:
        """Stable identity across refreshes (keeps the selection in place)."""
        return f"{self.repo}#{self.pr.number}"

    @property
    def repo_name(self) -> str:
        return self.repo.partition("/")[2] or self.repo

    @property
    def ctx(self) -> RepoContext:
        owner, _, name = self.repo.partition("/")
        return RepoContext(owner=owner, repo=name, branch=self.branch)

    @property
    def failing(self) -> bool:
        return self.pr.rollup is CheckState.FAILURE

    @property
    def open_threads(self) -> int:
        return len(self.pr.threads)

    @property
    def review_gap(self) -> bool:
        """True when the PR still needs review work: no approval yet, or a
        reviewer asked for changes. Drafts never count — they aren't up for
        review. A None decision means the repo requires no reviews, so only
        the absence of any approval flags it."""
        if self.pr.is_draft:
            return False
        decision = self.pr.metrics.review_decision
        if decision == "APPROVED":
            return False
        if decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            return True
        return self.pr.metrics.approvals == 0

    @property
    def conflicting(self) -> bool:
        """The branch no longer merges cleanly into its base. GitHub computes
        mergeability lazily, so anything other than a definite CONFLICTING —
        including the UNKNOWN it returns while computing — is not a conflict."""
        return self.pr.metrics.mergeable == "CONFLICTING"

    @property
    def needs_attention(self) -> bool:
        return (
            self.failing
            or self.open_threads > 0
            or self.review_gap
            or self.conflicting
        )

    @property
    def ready(self) -> bool:
        """All clear: review satisfied, no open comments, checks done and not
        failing, no merge conflicts (all four via `needs_attention`), and not a
        draft. The green-dot state."""
        return (
            not self.needs_attention
            and not self.pr.is_draft
            and self.pr.rollup is not CheckState.PENDING
        )


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def sort_items(items: list[PrItem]) -> list[PrItem]:
    """Attention-needed PRs first; most recently updated first within each group."""

    def key(item: PrItem) -> tuple[bool, float]:
        updated = item.pr.metrics.updated_at or _EPOCH
        return (not item.needs_attention, -updated.timestamp())

    return sorted(items, key=key)


def partition_hidden(
    data: Mapping[str, list[PrItem]], hidden: Mapping[str, datetime]
) -> dict[str, list[PrItem]]:
    """Split a poll's source views into what to show and what's hidden.

    Returns every view in `VIEWS`: each source view with its hidden PRs taken
    out, plus a "hidden" view holding the ones that were taken out (deduped —
    a PR can legitimately appear in more than one source view). Pure, so the
    dashboard can re-derive its lists the instant you press `h`, with no poll.

    Hidden PRs are ordered by when you hid them, newest first, so the one you
    just dismissed is at the top if you want it back. Hide-list entries for
    PRs this poll didn't return (merged, closed, or simply outside the day
    window) have nothing to show and are silently absent — they stay on the
    list, and reappear here if the PR does.
    """
    shown = {
        view: [item for item in items if item.key not in hidden]
        for view, items in data.items()
    }
    seen: set[str] = set()
    hidden_items: list[PrItem] = []
    for items in data.values():
        for item in items:
            if item.key in hidden and item.key not in seen:
                seen.add(item.key)
                hidden_items.append(item)
    hidden_items.sort(
        key=lambda item: (
            -hidden[item.key].timestamp(),
            -(item.pr.metrics.updated_at or _EPOCH).timestamp(),
        )
    )
    return {**shown, "hidden": hidden_items}
