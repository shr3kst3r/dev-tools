"""GitHub access for my-prs, via the `gh` CLI (reuses the user's auth).

One GraphQL *search* query per view fetches the recent PRs — authored by the
user ("mine") or awaiting the user's review ("review") — with the same per-PR
fields pr-watch uses. The hard parsing is delegated to pr-watch's pure
`parse_pull_request`; this module only wraps each node with its repo/branch.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `_run` is pr-watch's thin subprocess wrapper; shared on purpose so both
# tools report git/gh failures identically.
from tools.pr_watch.github import GitHubError, _run, parse_pull_request, require_gh

from .models import PrItem

__all__ = ["GitHubError", "require_gh", "build_search_query", "fetch_prs", "parse_search"]

# How each view narrows the search: whose PRs, relative to `author`.
# `review-requested` clears once you submit a review, so the review view
# naturally lists only PRs still waiting on you.
_VIEW_QUALIFIERS = {
    "mine": "author:{author}",
    "review": "review-requested:{author}",
}

# The per-PR selection mirrors pr-watch's _PR_QUERY (parse_pull_request reads
# this exact shape), plus repository/headRefName so the list can say where
# each PR lives. Comments are trimmed to the first one per thread — the list
# view only shows who opened the thread and the total count.
_SEARCH_QUERY = """
query($q: String!, $limit: Int!) {
  search(query: $q, type: ISSUE, first: $limit) {
    issueCount
    nodes {
      ... on PullRequest {
        repository { nameWithOwner }
        headRefName
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
            comments(first: 1) {
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


def build_search_query(
    days: int, now: datetime, author: str = "@me", view: str = "mine"
) -> str:
    """The GitHub search string: the view's open PRs updated in the window."""
    since = (now - timedelta(days=days)).date().isoformat()
    who = _VIEW_QUALIFIERS[view].format(author=author)
    return f"is:pr is:open {who} updated:>={since} sort:updated-desc"


def fetch_prs(
    view: str,
    days: int,
    limit: int,
    author: str = "@me",
    cwd: Path | None = None,
) -> list[PrItem]:
    """Return the view's open PRs updated within the last `days` days."""
    query = build_search_query(
        days, now=datetime.now(timezone.utc), author=author, view=view
    )
    raw = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_SEARCH_QUERY}",
            "-f",
            f"q={query}",
            "-F",
            f"limit={limit}",
        ],
        cwd or Path.cwd(),
    )
    payload = json.loads(raw)
    if payload.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise GitHubError(f"GitHub GraphQL error: {messages}")

    nodes = payload.get("data", {}).get("search", {}).get("nodes") or []
    return parse_search(nodes)


def parse_search(nodes: list[dict]) -> list[PrItem]:
    """Turn search result nodes into PrItems. No I/O.

    Search can return empty objects for hits that aren't PRs (the inline
    fragment doesn't match); those are skipped.
    """
    items: list[PrItem] = []
    for node in nodes:
        if not node or "number" not in node:
            continue
        repo = (node.get("repository") or {}).get("nameWithOwner") or "?"
        items.append(
            PrItem(
                repo=repo,
                branch=node.get("headRefName") or "",
                pr=parse_pull_request(node),
            )
        )
    return items
