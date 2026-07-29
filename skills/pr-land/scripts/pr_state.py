#!/usr/bin/env python3
"""One-shot snapshot of a GitHub PR's checks and review threads, normalized.

Stdlib only, deliberately. This ships with the `pr-land` skill and runs inside
whatever repository the skill is pointed at — etl-service, billing-service,
web-app — never inside this project's venv. The only external dependency
is the `gh` CLI, which supplies auth.

No model is involved in anything this file does. Every field it emits is a
function of the GraphQL payload, which is the point: the loop that consumes it
needs a stable, diffable view of "what is still wrong with this PR" that cannot
drift between iterations depending on how a model felt about reading the page.

Three things here are load-bearing and easy to get wrong by hand:

1. **Stale duplicate check runs.** A re-run leaves the *old* CheckRun in the
   rollup under the same name. `statusCheckRollup.state` counts both, so a PR
   whose failing check was re-run green still reports FAILURE. `latest_checks`
   dedupes by name keeping the newest, then recomputes the rollup.
2. **Azure DevOps build ids.** azdo results arrive as CheckRuns whose
   `detailsUrl` embeds `buildId=<n>`. Parsing that is strictly better than
   hunting for the run through `az pipelines runs list`, which has to guess at
   refs and race the queue.
3. **Bot finding bodies.** Cursor Bugbot and the Codex connector wrap their real
   content in HTML badges, tracking comments, and "Fix in Cursor" images. The
   parsers pull out title/severity/locations and drop the chrome.

Usage:
    pr_state.py                        # current branch's PR, human-readable
    pr_state.py --json                 # same, as JSON
    pr_state.py --pr 943               # explicit PR number
    pr_state.py --repo owner/name --pr 943
    pr_state.py --unanswered           # only threads we have not replied to
    pr_state.py --include-resolved     # keep resolved threads in the output
    pr_state.py --exit-code            # 0 green · 1 actionable · 2 waiting · 3 error
    pr_state.py --parse-file payload.json   # parse a saved payload, no network
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

# --- exit codes ---------------------------------------------------------------

EXIT_GREEN = 0
EXIT_ACTIONABLE = 1
EXIT_WAITING = 2
EXIT_ERROR = 3

# --- bot identities -----------------------------------------------------------
#
# GraphQL reports `author.login` for App accounts *without* the `[bot]` suffix
# the REST API uses ("cursor", not "cursor[bot]"), so both spellings are matched.

CURSOR_LOGINS = frozenset({"cursor", "cursor[bot]", "cursorai", "cursor-com"})
CODEX_LOGINS = frozenset({"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"})
OTHER_BOT_LOGINS = frozenset(
    {
        "coderabbitai",
        "copilot-pull-request-reviewer",
        "github-actions",
        "github-advanced-security",
        "greptile-apps",
        "linear",
        "sonarcloud",
    }
)

SOURCE_CURSOR = "cursor"
SOURCE_CODEX = "codex"
SOURCE_BOT = "bot"
SOURCE_HUMAN = "human"

# --- the query ----------------------------------------------------------------

_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  viewer { login }
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      number
      title
      url
      isDraft
      state
      baseRefName
      headRefName
      headRefOid
      updatedAt
      mergeable
      mergeStateStatus
      reviewDecision
      author { login }
      commits(last: 1) {
        nodes {
          commit {
            oid
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
      reviews(first: 50) {
        nodes { author { login } state submittedAt body }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          isCollapsed
          path
          line
          originalLine
          comments(first: 20) {
            nodes {
              databaseId
              author { login }
              createdAt
              body
            }
          }
        }
      }
    }
  }
}
"""


# --- pure parse layer ---------------------------------------------------------


@dataclass
class Location:
    """A file span a bot finding points at."""

    path: str
    start: int | None = None
    end: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "start": self.start, "end": self.end}


@dataclass
class Finding:
    """One unresolved conversation on the PR, normalized across sources."""

    thread_id: str
    source: str
    author: str
    path: str | None
    line: int | None
    line_from: str
    title: str
    body: str
    severity: str | None = None
    resolved: bool = False
    outdated: bool = False
    locations: list[Location] = field(default_factory=list)
    comment_count: int = 0
    last_author: str | None = None
    answered_by_viewer: bool = False
    reviewed_commit: str | None = None
    bug_id: str | None = None
    references: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.thread_id,
            "source": self.source,
            "author": self.author,
            "path": self.path,
            "line": self.line,
            "lineFrom": self.line_from,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "resolved": self.resolved,
            "outdated": self.outdated,
            "locations": [loc.as_dict() for loc in self.locations],
            "commentCount": self.comment_count,
            "lastAuthor": self.last_author,
            "answeredByViewer": self.answered_by_viewer,
            "reviewedCommit": self.reviewed_commit,
            "bugId": self.bug_id,
            "references": self.references,
        }


def classify_author(login: str | None) -> str:
    """Map a comment author to a triage source class."""
    if not login:
        return SOURCE_HUMAN
    lowered = login.lower()
    if lowered in CURSOR_LOGINS:
        return SOURCE_CURSOR
    if lowered in CODEX_LOGINS:
        return SOURCE_CODEX
    if lowered in OTHER_BOT_LOGINS or lowered.endswith("[bot]"):
        return SOURCE_BOT
    return SOURCE_HUMAN


def parse_azdo_details_url(url: str | None) -> dict[str, str] | None:
    """Pull org / project / buildId out of an Azure DevOps build results URL.

    azdo posts its pipeline results back to GitHub as CheckRuns pointing at
    `https://dev.azure.com/<org>/<project-or-guid>/_build/results?buildId=<n>`.
    That `buildId` is the run id every `az devops invoke` call needs, so reading
    it here removes the whole "find the run, dodge the queue race" dance.
    """
    if not url or "dev.azure.com" not in url:
        return None
    build = re.search(r"[?&]buildId=(\d+)", url)
    if not build:
        return None
    path = re.search(r"dev\.azure\.com/([^/]+)/([^/]+)/_build/results", url)
    out = {"buildId": build.group(1)}
    if path:
        out["org"] = path.group(1)
        out["project"] = path.group(2)
    job = re.search(r"[?&]jobId=([0-9a-fA-F-]+)", url)
    if job:
        out["jobId"] = job.group(1)
    return out


def _iso_key(value: str | None) -> str:
    """Sort key for ISO-8601 timestamps that tolerates nulls (sorted earliest)."""
    return value or ""


def normalize_check(node: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a CheckRun or StatusContext rollup node into one shape."""
    kind = node.get("__typename")
    if kind == "CheckRun":
        conclusion = node.get("conclusion")
        status = node.get("status")
        return {
            "name": node.get("name") or "(unnamed)",
            "kind": "CheckRun",
            "status": status,
            "conclusion": conclusion,
            "state": _check_state(status, conclusion),
            "url": node.get("detailsUrl"),
            "startedAt": node.get("startedAt"),
            "completedAt": node.get("completedAt"),
            "azdo": parse_azdo_details_url(node.get("detailsUrl")),
        }
    if kind == "StatusContext":
        state = (node.get("state") or "").upper()
        return {
            "name": node.get("context") or "(unnamed)",
            "kind": "StatusContext",
            "status": "COMPLETED" if state not in ("PENDING", "EXPECTED") else "IN_PROGRESS",
            "conclusion": state,
            "state": _status_context_state(state),
            "url": node.get("targetUrl"),
            "startedAt": node.get("createdAt"),
            "completedAt": node.get("createdAt"),
            "azdo": parse_azdo_details_url(node.get("targetUrl")),
        }
    return None


def _check_state(status: str | None, conclusion: str | None) -> str:
    """Reduce (status, conclusion) to pending / passing / failing / neutral."""
    if status and status.upper() not in ("COMPLETED",):
        return "pending"
    verdict = (conclusion or "").upper()
    if verdict in ("SUCCESS",):
        return "passing"
    if verdict in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"):
        return "failing"
    if verdict in ("CANCELLED", "STALE", "SKIPPED", "NEUTRAL"):
        return "neutral"
    if not verdict:
        return "pending"
    return "neutral"


def _status_context_state(state: str) -> str:
    if state in ("PENDING", "EXPECTED"):
        return "pending"
    if state == "SUCCESS":
        return "passing"
    if state in ("FAILURE", "ERROR"):
        return "failing"
    return "neutral"


def latest_checks(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe rollup entries by name, keeping the most recent attempt.

    A re-run does not replace the old CheckRun in the rollup — both are
    returned, and `statusCheckRollup.state` counts the stale failure. Keeping
    only the newest per name is what makes "is this PR actually red?" answerable.
    """
    best: dict[str, dict[str, Any]] = {}
    for node in nodes:
        check = normalize_check(node)
        if check is None:
            continue
        key = check["name"]
        current = best.get(key)
        if current is None:
            best[key] = check
            continue
        # Prefer the later attempt; an in-flight run with no completedAt still
        # supersedes a finished older one, so fall back to startedAt.
        newer = max(
            (current, check),
            key=lambda c: (_iso_key(c["completedAt"]), _iso_key(c["startedAt"])),
        )
        if check["state"] == "pending" and current["state"] != "pending":
            if _iso_key(check["startedAt"]) >= _iso_key(current["startedAt"]):
                newer = check
        best[key] = newer
    return sorted(best.values(), key=lambda c: c["name"].lower())


def effective_rollup(checks: list[dict[str, Any]]) -> str:
    """Recompute the rollup from deduped checks: FAILURE > PENDING > SUCCESS."""
    states = {c["state"] for c in checks}
    if "failing" in states:
        return "FAILURE"
    if "pending" in states:
        return "PENDING"
    if "passing" in states:
        return "SUCCESS"
    return "NEUTRAL"


def group_azdo_builds(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse azdo CheckRuns into one entry per buildId, worst state wins."""
    builds: dict[str, dict[str, Any]] = {}
    for check in checks:
        azdo = check.get("azdo")
        if not azdo:
            continue
        build_id = azdo["buildId"]
        entry = builds.setdefault(
            build_id,
            {
                "buildId": build_id,
                "org": azdo.get("org"),
                "project": azdo.get("project"),
                "state": "passing",
                "checks": [],
                "failingChecks": [],
                "url": f"https://dev.azure.com/{azdo.get('org', '')}/{azdo.get('project', '')}"
                f"/_build/results?buildId={build_id}",
            },
        )
        entry["checks"].append(check["name"])
        if check["state"] == "failing":
            entry["failingChecks"].append(check["name"])
        entry["state"] = _worse(entry["state"], check["state"])
    for entry in builds.values():
        entry["checks"].sort()
        entry["failingChecks"].sort()
    return sorted(builds.values(), key=lambda b: int(b["buildId"]))


_STATE_RANK = {"passing": 0, "neutral": 1, "pending": 2, "failing": 3}


def _worse(a: str, b: str) -> str:
    return a if _STATE_RANK.get(a, 0) >= _STATE_RANK.get(b, 0) else b


# --- bot body parsers ---------------------------------------------------------

_CURSOR_DESC = re.compile(
    r"<!--\s*DESCRIPTION START\s*-->(.*?)<!--\s*DESCRIPTION END\s*-->", re.DOTALL
)
_CURSOR_LOCATIONS = re.compile(
    r"<!--\s*LOCATIONS START\s*(.*?)\s*LOCATIONS END\s*-->", re.DOTALL
)
_CURSOR_BUG_ID = re.compile(r"<!--\s*BUGBOT_BUG_ID:\s*([0-9a-fA-F-]+)\s*-->")
_CURSOR_SEVERITY = re.compile(r"\*\*(\w+)\s+Severity\*\*", re.IGNORECASE)
_CURSOR_COMMIT = re.compile(r"for commit ([0-9a-f]{7,40})")
_LOCATION_LINE = re.compile(r"^(?P<path>[^\s#]+)(?:#L(?P<start>\d+)(?:-L(?P<end>\d+))?)?$")

_CODEX_TITLE = re.compile(
    r"\*\*(?:<sub>)*(?:!\[(?P<badge>[^\]]*)\]\([^)]*\))?(?:</sub>)*\s*(?P<title>[^*]+?)\*\*"
)
_CODEX_BADGE_SEVERITY = re.compile(r"^(P\d)\b", re.IGNORECASE)
_CODEX_FOOTER = re.compile(r"Useful\?\s*React with.*$", re.DOTALL)
# Reviewer bots cite the repo's own convention docs to justify a finding, as
# markdown link *text*: `[designs/AGENTS.md:L10-L12](https://github.com/…)`.
# Match the text only — matching anywhere would also hit the URL's copy of it.
_CONVENTION_REF = re.compile(
    r"\[([^\]\s]*(?:AGENTS|CLAUDE)\.md(?::L\d+(?:-L\d+)?)?)\]\("
)

_HTML_BLOCK = re.compile(r"<(div|details|picture|sup|a|img|source)\b.*?(?:</\1>|>)", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_chrome(body: str) -> str:
    """Remove HTML badges, tracking comments, and CTA images from a bot body."""
    text = _HTML_COMMENT.sub("", body)
    text = re.sub(r"<div>.*?</div>", "", text, flags=re.DOTALL)
    text = re.sub(r"<details>.*?</details>", "", text, flags=re.DOTALL)
    text = re.sub(r"<sup>.*?</sup>", "", text, flags=re.DOTALL)
    text = _HTML_BLOCK.sub("", text)
    text = re.sub(r"</?(sub|sup|b|i|br)\s*/?>", "", text)
    text = _CODEX_FOOTER.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_locations(block: str) -> list[Location]:
    """Parse a cursor LOCATIONS block: one `path#L<start>-L<end>` per line."""
    out: list[Location] = []
    for raw in block.splitlines():
        line = raw.strip().strip("-").strip().strip("`")
        if not line:
            continue
        match = _LOCATION_LINE.match(line)
        if not match:
            continue
        start = match.group("start")
        end = match.group("end")
        out.append(
            Location(
                path=match.group("path"),
                start=int(start) if start else None,
                end=int(end) if end else (int(start) if start else None),
            )
        )
    return out


def parse_cursor_body(body: str) -> dict[str, Any]:
    """Extract title / severity / description / locations from a Bugbot comment."""
    title = ""
    for line in body.splitlines():
        if line.startswith("###"):
            title = line.lstrip("#").strip()
            break
    severity_match = _CURSOR_SEVERITY.search(body)
    desc_match = _CURSOR_DESC.search(body)
    loc_match = _CURSOR_LOCATIONS.search(body)
    commit_match = _CURSOR_COMMIT.search(body)
    bug_match = _CURSOR_BUG_ID.search(body)
    description = desc_match.group(1).strip() if desc_match else strip_chrome(body)
    return {
        "title": title or _first_sentence(description),
        "severity": severity_match.group(1).lower() if severity_match else None,
        "body": description,
        "locations": parse_locations(loc_match.group(1)) if loc_match else [],
        "reviewed_commit": commit_match.group(1) if commit_match else None,
        "bug_id": bug_match.group(1) if bug_match else None,
        "references": [],
    }


def parse_codex_body(body: str) -> dict[str, Any]:
    """Extract title / P-severity / prose from a Codex connector comment."""
    title = ""
    severity = None
    match = _CODEX_TITLE.search(body)
    if match:
        title = match.group("title").strip()
        badge = match.group("badge") or ""
        badge_match = _CODEX_BADGE_SEVERITY.match(badge)
        if badge_match:
            severity = badge_match.group(1).upper()
    cleaned = strip_chrome(body)
    if title:
        # Drop the heading line from the prose so the body is not title-repeated.
        cleaned = "\n".join(
            line for line in cleaned.splitlines() if title not in line
        ).strip()
    return {
        "title": title or _first_sentence(cleaned),
        "severity": severity,
        "body": cleaned,
        "locations": [],
        "reviewed_commit": None,
        "bug_id": None,
        "references": sorted(set(_CONVENTION_REF.findall(body))),
    }


def parse_human_body(body: str) -> dict[str, Any]:
    cleaned = strip_chrome(body)
    return {
        "title": _first_sentence(cleaned),
        "severity": None,
        "body": cleaned,
        "locations": [],
        "reviewed_commit": None,
        "bug_id": None,
        "references": [],
    }


def _first_sentence(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    if not flat:
        return "(empty comment)"
    cut = flat.split(". ")[0]
    return cut[:limit].rstrip() + ("…" if len(cut) > limit else "")


def parse_thread(thread: dict[str, Any], viewer: str | None) -> Finding | None:
    """Normalize one reviewThread node into a Finding."""
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return None
    first = comments[0]
    author = ((first.get("author") or {}).get("login")) or ""
    source = classify_author(author)
    body = first.get("body") or ""
    if source == SOURCE_CURSOR:
        parsed = parse_cursor_body(body)
    elif source == SOURCE_CODEX:
        parsed = parse_codex_body(body)
    else:
        parsed = parse_human_body(body)

    locations: list[Location] = parsed["locations"]
    line = thread.get("line") or thread.get("originalLine")
    line_from = "thread" if thread.get("line") else ("original" if thread.get("originalLine") else "none")
    if line is None and locations and locations[0].start:
        line = locations[0].start
        line_from = "locations"

    viewer_lower = (viewer or "").lower()
    later_authors = [
        ((c.get("author") or {}).get("login") or "").lower() for c in comments[1:]
    ]
    return Finding(
        thread_id=thread.get("id") or "",
        source=source,
        author=author,
        path=thread.get("path") or (locations[0].path if locations else None),
        line=line,
        line_from=line_from,
        title=parsed["title"],
        body=parsed["body"],
        severity=parsed["severity"],
        resolved=bool(thread.get("isResolved")),
        outdated=bool(thread.get("isOutdated")),
        locations=locations,
        comment_count=len(comments),
        last_author=((comments[-1].get("author") or {}).get("login")) or None,
        answered_by_viewer=bool(viewer_lower) and viewer_lower in later_authors,
        reviewed_commit=parsed["reviewed_commit"],
        bug_id=parsed["bug_id"],
        references=parsed["references"],
    )


_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "p1": 1,
    "medium": 2,
    "p2": 2,
    "low": 3,
    "p3": 3,
    None: 4,
}


def _finding_sort_key(f: Finding) -> tuple[int, int, int, str]:
    sev = _SEVERITY_RANK.get((f.severity or "").lower() or None, 4)
    return (
        0 if f.source == SOURCE_HUMAN else 1,  # humans first — they can be asked
        sev,
        1 if f.outdated else 0,
        f.path or "",
    )


def parse_pr_state(
    payload: dict[str, Any],
    *,
    include_resolved: bool = False,
    unanswered_only: bool = False,
) -> dict[str, Any]:
    """Turn the GraphQL payload into the normalized snapshot. Pure."""
    data = payload.get("data") or payload
    viewer = ((data.get("viewer") or {}).get("login")) or None
    repo = data.get("repository") or {}
    pr = repo.get("pullRequest")
    if not pr:
        raise ValueError("payload contains no pullRequest")

    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = {}
    if commits:
        rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") or {}
    raw_contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
    checks = latest_checks(raw_contexts)

    findings: list[Finding] = []
    for thread in (pr.get("reviewThreads") or {}).get("nodes") or []:
        finding = parse_thread(thread, viewer)
        if finding is None:
            continue
        if finding.resolved and not include_resolved:
            continue
        if unanswered_only and finding.answered_by_viewer:
            continue
        findings.append(finding)
    findings.sort(key=_finding_sort_key)

    by_state: dict[str, list[dict[str, Any]]] = {
        "failing": [],
        "pending": [],
        "passing": [],
        "neutral": [],
    }
    for check in checks:
        by_state[check["state"]].append(check)

    by_source: dict[str, int] = {}
    for finding in findings:
        by_source[finding.source] = by_source.get(finding.source, 0) + 1

    reviews = [
        {
            "author": ((r.get("author") or {}).get("login")) or "",
            "state": r.get("state"),
            "submittedAt": r.get("submittedAt"),
        }
        for r in ((pr.get("reviews") or {}).get("nodes") or [])
    ]

    return {
        "viewer": viewer,
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "url": pr.get("url"),
            "state": pr.get("state"),
            "isDraft": pr.get("isDraft"),
            "author": ((pr.get("author") or {}).get("login")) or None,
            "branch": pr.get("headRefName"),
            "base": pr.get("baseRefName"),
            "headSha": pr.get("headRefOid"),
            "updatedAt": pr.get("updatedAt"),
            "mergeable": pr.get("mergeable"),
            "mergeStateStatus": pr.get("mergeStateStatus"),
            "reviewDecision": pr.get("reviewDecision"),
        },
        "checks": {
            "reportedRollup": rollup.get("state"),
            "rollup": effective_rollup(checks),
            "staleDuplicatesDropped": max(0, len(raw_contexts) - len(checks)),
            "counts": {state: len(items) for state, items in by_state.items()},
            "failing": by_state["failing"],
            "pending": by_state["pending"],
            "passing": by_state["passing"],
            "neutral": by_state["neutral"],
            "azdoBuilds": group_azdo_builds(checks),
        },
        "reviews": reviews,
        "threads": [f.as_dict() for f in findings],
        "summary": {
            "failingChecks": len(by_state["failing"]),
            "pendingChecks": len(by_state["pending"]),
            "openThreads": len(findings),
            "threadsBySource": by_source,
            "actionable": bool(by_state["failing"]) or bool(findings),
            "waiting": not by_state["failing"] and not findings and bool(by_state["pending"]),
        },
    }


# --- I/O boundary -------------------------------------------------------------


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def detect_repo() -> tuple[str, str]:
    full = _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    owner, _, name = full.partition("/")
    if not owner or not name:
        raise RuntimeError(f"could not parse repo from {full!r}")
    return owner, name


def detect_pr() -> int:
    out = _run(["gh", "pr", "view", "--json", "number", "--jq", ".number"])
    return int(out)


def fetch_payload(owner: str, repo: str, pr: int) -> dict[str, Any]:
    raw = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo}",
            "-F",
            f"pr={pr}",
        ]
    )
    return json.loads(raw)


# --- rendering ----------------------------------------------------------------

_MARK = {"failing": "✗", "pending": "…", "passing": "✓", "neutral": "-"}


def render(state: dict[str, Any]) -> str:
    pr = state["pr"]
    checks = state["checks"]
    lines: list[str] = []
    draft = " [draft]" if pr["isDraft"] else ""
    lines.append(f"PR #{pr['number']}{draft} — {pr['title']}")
    lines.append(f"  {pr['url']}")
    lines.append(
        f"  branch {pr['branch']} → {pr['base']} @ {(pr['headSha'] or '')[:7]}"
        f" · review={pr['reviewDecision'] or 'NONE'} · mergeable={pr['mergeable']}"
    )
    counts = checks["counts"]
    lines.append("")
    lines.append(
        f"Checks: {checks['rollup']}"
        f"  ({counts['failing']} failing, {counts['pending']} pending,"
        f" {counts['passing']} passing, {counts['neutral']} neutral)"
    )
    # Only worth saying when a stale attempt is what made the two disagree —
    # a PR with no checks at all has a null reported rollup and no story.
    if checks["staleDuplicatesDropped"] and checks["reportedRollup"] != checks["rollup"]:
        lines.append(
            f"  note: GitHub reports {checks['reportedRollup']} —"
            f" {checks['staleDuplicatesDropped']} stale re-run duplicate(s) ignored"
        )
    for state_name in ("failing", "pending"):
        for check in checks[state_name]:
            suffix = ""
            if check.get("azdo"):
                suffix = f"  [azdo build {check['azdo']['buildId']}]"
            lines.append(f"  {_MARK[state_name]} {check['name']}{suffix}")
    for build in checks["azdoBuilds"]:
        if build["state"] == "failing":
            lines.append("")
            lines.append(f"azdo build {build['buildId']} FAILED — {build['url']}")
            for name in build["failingChecks"]:
                lines.append(f"  ✗ {name}")

    lines.append("")
    threads = state["threads"]
    if not threads:
        lines.append("Open threads: none")
    else:
        lines.append(f"Open threads: {len(threads)}")
        for finding in threads:
            sev = f" [{finding['severity']}]" if finding["severity"] else ""
            flags = []
            if finding["outdated"]:
                flags.append("outdated")
            if finding["answeredByViewer"]:
                flags.append("answered")
            flag = f" ({', '.join(flags)})" if flags else ""
            where = finding["path"] or "(no file)"
            if finding["line"]:
                where += f":{finding['line']}"
            lines.append(f"  • {finding['source']}{sev} {where}{flag}")
            lines.append(f"    {finding['title']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, help="PR number (default: current branch's PR)")
    ap.add_argument("--repo", help="owner/name (default: detect via gh)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--include-resolved", action="store_true", help="keep resolved threads")
    ap.add_argument(
        "--unanswered",
        action="store_true",
        help="only threads you have not already replied to",
    )
    ap.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 0 green · 1 actionable · 2 waiting on in-flight checks",
    )
    ap.add_argument("--parse-file", help="parse a saved GraphQL payload instead of querying")
    args = ap.parse_args(argv)

    try:
        if args.parse_file:
            with open(args.parse_file, encoding="utf-8") as fh:
                payload = json.load(fh)
        else:
            if args.repo:
                owner, _, name = args.repo.partition("/")
            else:
                owner, name = detect_repo()
            pr = args.pr or detect_pr()
            payload = fetch_payload(owner, name, pr)
        state = parse_pr_state(
            payload,
            include_resolved=args.include_resolved,
            unanswered_only=args.unanswered,
        )
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"pr_state: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(json.dumps(state, indent=2) if args.json else render(state))

    if not args.exit_code:
        return EXIT_GREEN
    if state["summary"]["actionable"]:
        return EXIT_ACTIONABLE
    if state["summary"]["waiting"]:
        return EXIT_WAITING
    return EXIT_GREEN


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
