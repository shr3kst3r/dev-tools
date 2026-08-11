"""GitHub access for my-issues, via the `gh` CLI (reuses the user's auth).

A single GraphQL request fetches *all three* searched views at once — the issues
assigned to you, the ones you filed, and the ones that mention you — as three
aliased `search` fields sharing one issue-fields fragment. One request per poll
(instead of one per view) keeps the tool well under GitHub's rate limits, and is
why a third view was affordable at all. Those three are the only *searched*
views; the dashboard's "hidden" view is derived from them locally, so it costs
no request.

Parsing lives in the pure `parse_issue` / `parse_search` pair, which is what the
tests exercise; only `_graphql` touches the network. `gh` failures are turned
into concise, actionable errors the dashboard can act on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `_run` is pr-watch's thin subprocess wrapper; shared on purpose so every tool
# in the repo reports git/gh failures identically. That — plus `GitHubError` and
# `require_gh` — is the whole of what my-issues borrows from another tool.
from tools.pr_watch.github import GitHubError, _run, require_gh

from .models import SOURCE_VIEWS, Issue, IssueComment, IssueItem, Label

__all__ = [
    "GitHubError",
    "PollError",
    "require_gh",
    "build_search_query",
    "classify_github_error",
    "fetch_all_views",
    "parse_issue",
    "parse_search",
]

# How each view narrows the search: whose issue it is, relative to `user`.
_VIEW_QUALIFIERS = {
    "assigned": "assignee:{user}",
    "created": "author:{user}",
    "mentioned": "mentions:{user}",
}

# How many comments of an issue's thread the poll fetches. The detail pane shows
# this tail and says how many earlier ones it isn't showing; the list shows only
# the total. Kept small on purpose — a long thread would dwarf the response.
COMMENT_TAIL = 3

# The per-issue selection. `labels.nodes[].color` is a bare 6-hex-digit string
# with no leading "#"; `stateReason` is null on an ordinary open issue;
# `milestone` is nullable; `body` is "" (not null) when empty; and `author` is
# null for a ghosted account — parse_issue reads all of those defensively.
# Kept as a shared fragment so all three aliased searches select the same fields.
_ISSUE_FIELDS = f"""
fragment IssueFields on Issue {{
  repository {{ nameWithOwner }}
  number
  title
  url
  state
  stateReason
  body
  author {{ login }}
  createdAt
  updatedAt
  milestone {{ title }}
  reactions {{ totalCount }}
  labels(first: 10) {{
    nodes {{ name color }}
  }}
  assignees(first: 10) {{
    nodes {{ login }}
  }}
  comments(last: {COMMENT_TAIL}) {{
    totalCount
    nodes {{
      author {{ login }}
      body
      createdAt
      url
    }}
  }}
}}
"""

# All three source views in one request: three aliased searches sharing the
# fragment. Keeping the per-poll request count at one — not three — is the main
# defense against GitHub's rate limits, so don't split this up.
_MULTI_SEARCH_QUERY = (
    _ISSUE_FIELDS
    + """
query($assigned: String!, $created: String!, $mentioned: String!, $limit: Int!) {
  assigned: search(query: $assigned, type: ISSUE, first: $limit) {
    nodes { ...IssueFields }
  }
  created: search(query: $created, type: ISSUE, first: $limit) {
    nodes { ...IssueFields }
  }
  mentioned: search(query: $mentioned, type: ISSUE, first: $limit) {
    nodes { ...IssueFields }
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
    days: int, now: datetime, user: str = "@me", view: str = "assigned"
) -> str:
    """The GitHub search string: the view's open issues updated in the window."""
    since = (now - timedelta(days=days)).date().isoformat()
    who = _VIEW_QUALIFIERS[view].format(user=user)
    return f"is:issue is:open {who} updated:>={since} sort:updated-desc"


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
    user: str = "@me",
    cwd: Path | None = None,
) -> dict[str, list[IssueItem]]:
    """Fetch every searched view's issues in a single GraphQL request.

    Returns `{view: items}` for each view in `SOURCE_VIEWS`. One request per
    poll (rather than one per view) keeps the tool comfortably under GitHub's
    rate limits, which is what makes the dashboard's fast refresh sustainable.
    """
    now = datetime.now(timezone.utc)
    args = ["-f", f"query={_MULTI_SEARCH_QUERY}"]
    for view in SOURCE_VIEWS:
        args += ["-f", f"{view}={build_search_query(days, now, user, view)}"]
    args += ["-F", f"limit={limit}"]
    data = _graphql(args, cwd)
    return {
        view: parse_search((data.get(view) or {}).get("nodes") or [])
        for view in SOURCE_VIEWS
    }


# --- pure parsing (unit-tested) -------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_labels(node: dict) -> list[Label]:
    labels = (node.get("labels") or {}).get("nodes") or []
    out: list[Label] = []
    for label in labels:
        name = (label or {}).get("name")
        if name:
            out.append(Label(name=name, color=(label.get("color") or "")))
    return out


def _parse_assignees(node: dict) -> list[str]:
    assignees = (node.get("assignees") or {}).get("nodes") or []
    return [login for a in assignees if (login := (a or {}).get("login"))]


def _parse_comments(node: dict) -> list[IssueComment]:
    comments = (node.get("comments") or {}).get("nodes") or []
    return [
        IssueComment(
            author=((c or {}).get("author") or {}).get("login") or "unknown",
            body=((c or {}).get("body") or "").strip(),
            created_at=_parse_dt((c or {}).get("createdAt")),
            url=(c or {}).get("url"),
        )
        for c in comments
    ]


def parse_issue(node: dict) -> Issue:
    """Turn one GraphQL issue node into an Issue. No I/O."""
    comments = node.get("comments") or {}
    comment_nodes = _parse_comments(node)
    return Issue(
        number=node["number"],
        title=node.get("title") or "",
        url=node.get("url") or "",
        # `author` is null for a deleted (ghosted) account.
        author=(node.get("author") or {}).get("login") or "unknown",
        state=node.get("state") or "OPEN",
        state_reason=node.get("stateReason"),
        body=node.get("body") or "",
        created_at=_parse_dt(node.get("createdAt")),
        updated_at=_parse_dt(node.get("updatedAt")),
        milestone=(node.get("milestone") or {}).get("title"),
        comment_count=comments.get("totalCount") or len(comment_nodes),
        reactions=(node.get("reactions") or {}).get("totalCount") or 0,
        labels=_parse_labels(node),
        assignees=_parse_assignees(node),
        comments=comment_nodes,
    )


def parse_search(nodes: list[dict]) -> list[IssueItem]:
    """Turn search result nodes into IssueItems. No I/O.

    `search(type: ISSUE)` spans issues *and* pull requests, so a PR hit comes
    back as an empty object (the `... on Issue` fragment doesn't match); those
    are skipped.
    """
    items: list[IssueItem] = []
    for node in nodes:
        if not node or "number" not in node:
            continue
        repo = (node.get("repository") or {}).get("nameWithOwner") or "?"
        items.append(IssueItem(repo=repo, issue=parse_issue(node)))
    return items
