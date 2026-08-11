"""GitHub access for my-prs, via the `gh` CLI (reuses the user's auth).

A single GraphQL request fetches *both* views at once — the PRs authored by the
user ("mine") and those awaiting the user's review ("review") — as two aliased
`search` fields sharing one PR-fields fragment. One request per poll (instead
of one per view) keeps the tool well under GitHub's rate limits. Those two are
the only *searched* views; the dashboard's "hidden" view is derived from them
locally, so it costs no request. The hard
parsing is delegated to pr-watch's pure `parse_pull_request`; this module only
wraps each node with its repo/branch and turns `gh` failures into concise,
actionable errors the dashboard can act on.

Request *count* was never the binding constraint, though — GitHub's GraphQL
budget is scored in points, not calls, and this is by some distance the most
expensive query in the repo. See the note above `_PR_FIELDS` for what drives
that number and why `reviewThreads` is capped where it is; the short version
is that one poll costs ~54 points out of 5000/hour, so the cadence in cli.py
and the cap in the fragment are what keep the tool inside its budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `_run` is pr-watch's thin subprocess wrapper; shared on purpose so both
# tools report git/gh failures identically.
from tools.pr_watch.github import GitHubError, _run, parse_pull_request, require_gh

from .models import SOURCE_VIEWS, PrItem

__all__ = [
    "GitHubError",
    "PollError",
    "require_gh",
    "build_search_query",
    "classify_github_error",
    "fetch_all_views",
    "parse_search",
]

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
# view only shows who opened the thread and the total count. Kept as a shared
# fragment so both the single-view and combined-views queries select the same
# fields without duplicating ~60 lines.
#
# `reviewThreads` is the one expensive selection here, and it is worth
# understanding before anyone raises it back. GitHub scores a GraphQL call on
# the nodes it *might* return, and a connection nested inside another
# connection multiplies: this query's cost is roughly
# `2 searches × limit × reviewThreads-first / 100`. Measured against the real
# API at the default `--limit 50`, that is:
#
#     reviewThreads(first: 100) -> 104 points    reviewThreads(first: 50) -> 54
#     reviewThreads(first: 30)  ->  34 points    reviewThreads(first: 25) -> 29
#
# The GraphQL budget is 5000 points/hour, so the old `first: 100` cost 104
# points a poll — 6240/hour on the old 60s cadence, past the limit on its own
# and before my-issues or any pr-watch got a look in. Nothing else in the
# fragment moves the number: dropping the nested `comments` selection takes
# `first: 100` from 104 points to 4, and `latestOpinionatedReviews`/`contexts`
# cost nothing measurable because they have no connection under them.
#
# 50 is the compromise: half the cost, and a PR would need more than 50 review
# threads (resolved ones included — GitHub can't filter them server-side)
# before the unresolved count shown in the list starts to undercount.
_PR_FIELDS = """
fragment PrFields on PullRequest {
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
  reviewThreads(first: 50) {
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
"""

# Both source views in one request: two aliased searches sharing the fragment. Halving
# the per-poll request count is the main defense against GitHub's rate limits.
_MULTI_SEARCH_QUERY = (
    _PR_FIELDS
    + """
query($mine: String!, $review: String!, $limit: Int!) {
  mine: search(query: $mine, type: ISSUE, first: $limit) {
    nodes { ...PrFields }
  }
  review: search(query: $review, type: ISSUE, first: $limit) {
    nodes { ...PrFields }
  }
}
"""
)


@dataclass(frozen=True, slots=True)
class PollError:
    """A classified failure from a poll, ready for the UI to show and act on.

    `message` is a concise, user-facing line (never the raw `gh` command).
    `rate_limited` marks the case worth backing off for, and `retry_after` is
    the server's requested wait in seconds when it gave one.
    """

    message: str
    rate_limited: bool = False
    retry_after: int | None = None


def _extract_retry_after(text: str) -> int | None:
    """Pull a `Retry-After: N` hint out of a gh error, if present."""
    marker = "retry-after:"
    lowered = text.lower()
    if marker not in lowered:
        return None
    tail = text[lowered.index(marker) + len(marker) :].strip()
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def classify_github_error(exc: GitHubError) -> PollError:
    """Turn a raw `gh`/GraphQL failure into a concise, actionable PollError.

    The raw text may be a truncated `<command> failed: <gh message>` or a
    `GitHub GraphQL error: <messages>`; we key off the gh message tail so the
    command noise never reaches the dashboard.
    """
    raw = str(exc)
    tail = raw.split("failed:", 1)[1].strip() if "failed:" in raw else raw
    low = tail.lower()
    if "rate limit" in low or "secondary rate" in low or "abuse detection" in low:
        return PollError(
            message="GitHub API rate limit hit — backing off before retrying.",
            rate_limited=True,
            retry_after=_extract_retry_after(tail),
        )
    if (
        "bad credentials" in low
        or "gh auth" in low
        or "authentication" in low
        or "401" in low
        or "requires authentication" in low
    ):
        return PollError(
            message="GitHub authentication failed — run `gh auth login`."
        )
    return PollError(message=tail or raw)


def build_search_query(
    days: int, now: datetime, author: str = "@me", view: str = "mine"
) -> str:
    """The GitHub search string: the view's open PRs updated in the window."""
    since = (now - timedelta(days=days)).date().isoformat()
    who = _VIEW_QUALIFIERS[view].format(author=author)
    return f"is:pr is:open {who} updated:>={since} sort:updated-desc"


def _graphql(args: list[str], cwd: Path | None) -> dict:
    """Run a `gh api graphql` call and return its parsed `data`, raising a
    GitHubError for any GraphQL-level errors in the response."""
    raw = _run(["gh", "api", "graphql", *args], cwd or Path.cwd())
    payload = json.loads(raw)
    if payload.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise GitHubError(f"GitHub GraphQL error: {messages}")
    return payload.get("data") or {}


def fetch_all_views(
    days: int,
    limit: int,
    author: str = "@me",
    cwd: Path | None = None,
) -> dict[str, list[PrItem]]:
    """Fetch every view's PRs in a single GraphQL request.

    Returns `{view: items}` for each view in `VIEWS`. One request per poll
    (rather than one per view) keeps the tool comfortably under GitHub's rate
    limits, which is what makes the dashboard's fast refresh sustainable.
    """
    now = datetime.now(timezone.utc)
    data = _graphql(
        [
            "-f",
            f"query={_MULTI_SEARCH_QUERY}",
            "-f",
            f"mine={build_search_query(days, now, author, 'mine')}",
            "-f",
            f"review={build_search_query(days, now, author, 'review')}",
            "-F",
            f"limit={limit}",
        ],
        cwd,
    )
    return {
        view: parse_search((data.get(view) or {}).get("nodes") or [])
        for view in SOURCE_VIEWS
    }


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
