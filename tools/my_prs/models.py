"""Data model for the my-prs dashboard.

One `PrItem` per PR in the list. The PR payload itself reuses pr-watch's
`PullRequest` model (same GraphQL shape, same pure parser) — this module only
adds what a *cross-repo list* needs: which repo/branch each PR lives in, and
the "does this need me?" flags the dashboard sorts and colors by.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from tools.pr_watch.models import CheckState, PullRequest, RepoContext


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
    def needs_attention(self) -> bool:
        return self.failing or self.open_threads > 0 or self.review_gap

    @property
    def ready(self) -> bool:
        """All clear: review satisfied, no open comments, checks done and not
        failing, no merge conflicts, and not a draft. The green-dot state."""
        return (
            not self.needs_attention
            and not self.pr.is_draft
            and self.pr.rollup is not CheckState.PENDING
            and self.pr.metrics.mergeable != "CONFLICTING"
        )


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def sort_items(items: list[PrItem]) -> list[PrItem]:
    """Attention-needed PRs first; most recently updated first within each group."""

    def key(item: PrItem) -> tuple[bool, float]:
        updated = item.pr.metrics.updated_at or _EPOCH
        return (not item.needs_attention, -updated.timestamp())

    return sorted(items, key=key)
