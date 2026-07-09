"""Plain data models for the PR view.

Kept free of any I/O so they are trivial to construct in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CheckState(str, Enum):
    """Normalized status of a single check, across CheckRun and StatusContext."""

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RepoContext:
    """Which repo/branch we are watching."""

    owner: str
    repo: str
    branch: str

    @property
    def name_with_owner(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class Check:
    """A single check on the head commit (CheckRun or legacy StatusContext)."""

    name: str
    state: CheckState
    url: str | None = None
    # Raw GitHub label, e.g. "IN_PROGRESS", "FAILURE" — handy for tooltips.
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewThread:
    """An unresolved review thread. `author` is whoever started it."""

    author: str
    body: str
    path: str | None
    line: int | None
    url: str | None
    comment_count: int
    is_outdated: bool

    @property
    def location(self) -> str:
        if not self.path:
            return "(general)"
        if self.line is not None:
            return f"{self.path}:{self.line}"
        return self.path


@dataclass(frozen=True, slots=True)
class PRMetrics:
    """At-a-glance size, freshness, and review state of a PR."""

    additions: int
    deletions: int
    changed_files: int
    commits: int
    created_at: datetime | None
    updated_at: datetime | None
    # "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | None
    review_decision: str | None
    mergeable: str  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
    approvals: int
    changes_requested: int


@dataclass(frozen=True, slots=True)
class PullRequest:
    """Everything the UI needs about one open PR."""

    number: int
    title: str
    url: str
    is_draft: bool
    author: str
    rollup: CheckState
    metrics: PRMetrics
    checks: list[Check] = field(default_factory=list)
    threads: list[ReviewThread] = field(default_factory=list)

    def counts(self) -> dict[CheckState, int]:
        out: dict[CheckState, int] = {s: 0 for s in CheckState}
        for c in self.checks:
            out[c.state] += 1
        return out
