"""GitHub access for pr-watch, via the `gh` CLI (reuses the user's auth).

The network/subprocess boundary is intentionally thin: everything hard lives in
the pure `parse_pull_request` function, which is what the tests exercise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    Check,
    CheckState,
    PRMetrics,
    PullRequest,
    RepoContext,
    ReviewThread,
)

# One query fetches the PR, its head-commit check rollup, and review threads.
_PR_QUERY = """
query($owner: String!, $repo: String!, $branch: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequests(
      headRefName: $branch
      states: OPEN
      first: 5
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        number
        title
        url
        isDraft
        author { login }
        additions
        deletions
        changedFiles
        createdAt
        updatedAt
        reviewDecision
        mergeable
        latestOpinionatedReviews(first: 100) {
          nodes { state }
        }
        commits(last: 1) {
          totalCount
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 100) {
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      status
                      conclusion
                      detailsUrl
                      startedAt
                      completedAt
                    }
                    ... on StatusContext {
                      context
                      state
                      targetUrl
                      createdAt
                    }
                  }
                }
              }
            }
          }
        }
        reviewThreads(first: 100) {
          nodes {
            isResolved
            isOutdated
            comments(first: 50) {
              totalCount
              nodes {
                author { login }
                body
                path
                line
                originalLine
                url
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    """A user-actionable problem talking to git or gh."""


def _run(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - env dependent
        raise GitHubError(f"`{args[0]}` is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or "").strip()
        raise GitHubError(f"`{' '.join(args)}` failed: {msg}") from exc
    return proc.stdout


def require_gh() -> None:
    """Fail fast with a clear message if gh is missing."""
    if shutil.which("gh") is None:
        raise GitHubError(
            "The GitHub CLI (`gh`) is required. Install it and run `gh auth login`."
        )


def get_repo_context(cwd: Path) -> RepoContext:
    """Resolve the owner/repo/branch for the repo checked out in `cwd`."""
    require_gh()
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    if not branch or branch == "HEAD":
        raise GitHubError("Not on a branch (detached HEAD?) — cannot find a PR.")

    raw = _run(["gh", "repo", "view", "--json", "owner,name"], cwd)
    data = json.loads(raw)
    owner = data.get("owner", {}).get("login")
    repo = data.get("name")
    if not owner or not repo:
        raise GitHubError("Could not determine the GitHub repo for this directory.")
    return RepoContext(owner=owner, repo=repo, branch=branch)


def fetch_pull_request(ctx: RepoContext, cwd: Path) -> PullRequest | None:
    """Return the most-recently-updated open PR for the branch, or None."""
    raw = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_PR_QUERY}",
            "-F",
            f"owner={ctx.owner}",
            "-F",
            f"repo={ctx.repo}",
            "-F",
            f"branch={ctx.branch}",
        ],
        cwd,
    )
    payload = json.loads(raw)
    if payload.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise GitHubError(f"GitHub GraphQL error: {messages}")

    nodes = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequests", {})
        .get("nodes", [])
    )
    if not nodes:
        return None
    return parse_pull_request(nodes[0])


# --- pure parsing (unit-tested) -------------------------------------------

_ROLLUP_STATE = {
    "SUCCESS": CheckState.SUCCESS,
    "FAILURE": CheckState.FAILURE,
    "ERROR": CheckState.FAILURE,
    "PENDING": CheckState.PENDING,
    "EXPECTED": CheckState.PENDING,
}


def _checkrun_state(status: str | None, conclusion: str | None) -> CheckState:
    """Map a CheckRun's (status, conclusion) pair to a normalized state."""
    if status and status.upper() != "COMPLETED":
        return CheckState.PENDING
    match (conclusion or "").upper():
        case "SUCCESS":
            return CheckState.SUCCESS
        case "FAILURE" | "TIMED_OUT" | "STARTUP_FAILURE" | "ACTION_REQUIRED" | "CANCELLED":
            return CheckState.FAILURE
        case "SKIPPED" | "NEUTRAL" | "STALE":
            return CheckState.SKIPPED
        case _:
            return CheckState.UNKNOWN


def _parse_check(node: dict) -> Check:
    if node.get("__typename") == "CheckRun":
        status = node.get("status")
        conclusion = node.get("conclusion")
        return Check(
            name=node.get("name") or "(unnamed check)",
            state=_checkrun_state(status, conclusion),
            url=node.get("detailsUrl"),
            detail=conclusion or status,
            started_at=_parse_dt(node.get("startedAt")),
            completed_at=_parse_dt(node.get("completedAt")),
        )
    # StatusContext (legacy commit statuses). These carry only a createdAt, so
    # we treat that as the start; there's no completion timestamp to report.
    raw_state = (node.get("state") or "").upper()
    return Check(
        name=node.get("context") or "(status)",
        state=_ROLLUP_STATE.get(raw_state, CheckState.UNKNOWN),
        url=node.get("targetUrl"),
        detail=raw_state or None,
        started_at=_parse_dt(node.get("createdAt")),
    )


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _dedupe_latest_runs(checks: list[Check]) -> list[Check]:
    """Keep only the newest run of each check name.

    The rollup contexts hold one CheckRun per check *suite*, so a re-run
    workflow leaves its older (typically failed) runs in the list. GitHub's
    own checks UI collapses these by name to the latest run; do the same.
    Ties (or missing timestamps) go to the later entry in the list.
    """
    latest: dict[str, Check] = {}
    for check in checks:
        prior = latest.get(check.name)
        if prior is None or (check.started_at or _EPOCH) >= (prior.started_at or _EPOCH):
            latest[check.name] = check
    return list(latest.values())


def _rollup_from_checks(checks: list[Check]) -> CheckState:
    """Recompute the overall state from individual checks (GitHub's rollup
    `state` counts stale runs, so it can't be trusted after deduping)."""
    states = {c.state for c in checks}
    for decisive in (CheckState.FAILURE, CheckState.PENDING, CheckState.SUCCESS):
        if decisive in states:
            return decisive
    return CheckState.SKIPPED if CheckState.SKIPPED in states else CheckState.UNKNOWN


def _parse_thread(node: dict) -> ReviewThread | None:
    comments = node.get("comments", {}) or {}
    comment_nodes = comments.get("nodes") or []
    if not comment_nodes:
        return None
    first = comment_nodes[0]
    author = (first.get("author") or {}).get("login") or "unknown"
    line = first.get("line")
    if line is None:
        line = first.get("originalLine")
    return ReviewThread(
        author=author,
        body=(first.get("body") or "").strip(),
        path=first.get("path"),
        line=line,
        url=first.get("url"),
        comment_count=comments.get("totalCount") or len(comment_nodes),
        is_outdated=bool(node.get("isOutdated")),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_metrics(node: dict) -> PRMetrics:
    latest = (node.get("latestOpinionatedReviews") or {}).get("nodes") or []
    states = [r.get("state") for r in latest]
    return PRMetrics(
        additions=node.get("additions") or 0,
        deletions=node.get("deletions") or 0,
        changed_files=node.get("changedFiles") or 0,
        commits=(node.get("commits") or {}).get("totalCount") or 0,
        created_at=_parse_dt(node.get("createdAt")),
        updated_at=_parse_dt(node.get("updatedAt")),
        review_decision=node.get("reviewDecision"),
        mergeable=node.get("mergeable") or "UNKNOWN",
        approvals=states.count("APPROVED"),
        changes_requested=states.count("CHANGES_REQUESTED"),
    )


def parse_pull_request(node: dict) -> PullRequest:
    """Turn one GraphQL PR node into a PullRequest. No I/O."""
    commit_nodes = node.get("commits", {}).get("nodes") or []
    rollup = (
        (commit_nodes[0].get("commit", {}) if commit_nodes else {}).get(
            "statusCheckRollup"
        )
        or {}
    )
    all_runs = [_parse_check(c) for c in (rollup.get("contexts", {}).get("nodes") or [])]
    checks = _dedupe_latest_runs(all_runs)
    # GitHub's rollup state counts the stale runs we just dropped, so it only
    # stays authoritative when nothing was deduped (it also knows about
    # required checks that haven't reported yet, which we can't see here).
    if len(checks) < len(all_runs):
        rollup_state = _rollup_from_checks(checks)
    else:
        rollup_state = _ROLLUP_STATE.get(
            (rollup.get("state") or "").upper(), CheckState.UNKNOWN
        )

    threads: list[ReviewThread] = []
    for t in node.get("reviewThreads", {}).get("nodes") or []:
        if t.get("isResolved"):
            continue
        parsed = _parse_thread(t)
        if parsed is not None:
            threads.append(parsed)

    return PullRequest(
        number=node["number"],
        title=node.get("title") or "",
        url=node.get("url") or "",
        is_draft=bool(node.get("isDraft")),
        author=(node.get("author") or {}).get("login") or "unknown",
        rollup=rollup_state,
        checks=checks,
        threads=threads,
        metrics=_parse_metrics(node),
    )
