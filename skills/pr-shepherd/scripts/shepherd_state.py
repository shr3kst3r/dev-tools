#!/usr/bin/env python3
"""The /pr-shepherd snapshot: pr_state.py's view of a PR, plus the long-run view.

`pr-land` answers "what is wrong with this PR right now?". A shepherd that is
going to sit on a PR for a week needs four more answers, and this script gives
them from the same payload:

1. **How long have I been at this, and when do I give up?** The start time is a
   marker the shepherd leaves in a PR comment on its first cycle
   (`<!-- pr-shepherd: started <iso> deadline <iso> -->`), so the clock survives
   a restarted session, a summarized context, or a different machine. The PR is
   the state; there is no local file.
2. **Is this PR stacked?** A base that is not the repo's default branch means
   there is a PR below this one. Findings on code that PR introduced belong
   there, and when it merges GitHub retargets this one and the branch is
   suddenly "behind".
3. **What feedback is there that is not a thread?** A review skill that finds
   something on a line outside the diff cannot comment there, so it lists it in
   the review body. Nothing to resolve — but it still has to be answered, in a
   PR comment, or the reviewer sees it ignored.
4. **Who should be re-requested, and is that already done?** Once every thread
   is closed and the checks are green, the humans whose last word was
   CHANGES_REQUESTED get asked again. A pending request shows up in
   `reviewRequests` until they act, which is what keeps this idempotent.

Everything here is pure given the payload plus three facts the I/O boundary
supplies (`now`, the default branch, the PR open against the base branch), so
`--parse-file` reproduces any decision offline.

Usage:
    shepherd_state.py                       # current branch's PR, human-readable
    shepherd_state.py --json                # as JSON
    shepherd_state.py --pr 635 --repo owner/name
    shepherd_state.py --ensure-started      # post the start marker if missing
    shepherd_state.py --deadline-days 7     # how long before it gives up
    shepherd_state.py --exit-code           # 0 settled · 1 actionable · 2 waiting
                                            # 3 error · 4 deadline passed
    shepherd_state.py --parse-file p.json --now 2026-11-03T00:00:00Z \\
        --default-branch main --base-pr '{"number": 635, "state": "OPEN"}'
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# The shepherd is pr-land's loop with a longer horizon, and it reuses pr-land's
# snapshot rather than carrying a second copy of the GraphQL parsing. Resolve
# through the real file so the import works whether this runs from the repo,
# from ~/.claude/skills/pr-shepherd (a symlink into it), or from ~/.agents.
_PR_LAND_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "pr-land" / "scripts"
if str(_PR_LAND_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PR_LAND_SCRIPTS))

import pr_state as ps  # noqa: E402  # ty: ignore[unresolved-import]

EXIT_SETTLED = 0
EXIT_ACTIONABLE = 1
EXIT_WAITING = 2
EXIT_ERROR = 3
EXIT_EXPIRED = 4

DEFAULT_DEADLINE_DAYS = 7

MARKER_RE = re.compile(
    r"<!--\s*pr-shepherd:\s*started\s+(?P<started>\S+?)(?:\s+deadline\s+(?P<deadline>\S+?))?\s*-->"
)
_ROUND_MENTION = re.compile(r"\bround\s+(\d+)\b", re.IGNORECASE)

# States a review can be in that still say something about the reviewer's
# position. A DISMISSED review has been withdrawn (by them or by a push) and
# says nothing.
_LIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}


# --- time ---------------------------------------------------------------------


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def marker_body(started: datetime, deadline: datetime) -> str:
    """The PR comment the shepherd leaves on its first cycle.

    Visible on purpose — a reviewer should know the PR is being tended and that
    every thread will get a fix or an answer — but the only thing read back is
    the HTML comment on the last line.
    """
    return (
        f"Shepherding this PR until {deadline.strftime('%Y-%m-%d')}: every review thread "
        "gets a fix or an answer, then gets resolved; conflicts with the base get merged "
        "forward; review is re-requested once everything is addressed.\n\n"
        f"<!-- pr-shepherd: started {iso(started)} deadline {iso(deadline)} -->"
    )


# --- pure views ---------------------------------------------------------------


def find_marker(comments: list[dict[str, Any]], viewer: str | None) -> dict[str, Any] | None:
    """The earliest start marker the viewer left on this PR, if any."""
    viewer_lower = (viewer or "").lower()
    found: list[dict[str, Any]] = []
    for comment in comments:
        if (comment.get("author") or "").lower() != viewer_lower:
            continue
        match = MARKER_RE.search(comment.get("body") or "")
        if not match:
            continue
        started = parse_iso(match.group("started"))
        if started is None:
            continue
        found.append(
            {
                "startedAt": started,
                "deadline": parse_iso(match.group("deadline")),
                "createdAt": parse_iso(comment.get("createdAt")),
            }
        )
    if not found:
        return None
    return min(found, key=lambda m: m["startedAt"])


def deadline_view(
    comments: list[dict[str, Any]],
    viewer: str | None,
    *,
    now: datetime,
    deadline_days: float = DEFAULT_DEADLINE_DAYS,
) -> dict[str, Any]:
    """When the shepherd started, when it gives up, and whether that has passed.

    The marker's own deadline wins over `--deadline-days`, so a run started with
    one horizon is not silently shortened by a restart that used the default.
    """
    marker = find_marker(comments, viewer)
    if marker is None:
        return {
            "started": False,
            "startedAt": None,
            "deadline": None,
            "remainingHours": None,
            "expired": False,
        }
    started = marker["startedAt"]
    deadline = marker["deadline"] or (started + timedelta(days=deadline_days))
    remaining = (deadline - now).total_seconds() / 3600
    return {
        "started": True,
        "startedAt": iso(started),
        "deadline": iso(deadline),
        "remainingHours": round(remaining, 1),
        "expired": remaining <= 0,
    }


def stack_view(
    pr: dict[str, Any],
    *,
    default_branch: str | None,
    base_pr: dict[str, Any] | None,
) -> dict[str, Any]:
    """Is there a PR underneath this one?

    `base_pr` is whatever `gh pr list --head <base>` found: the open PR whose
    head is this PR's base. When it is None and the base is not the default
    branch, the base is a plain branch (or its PR already merged and GitHub has
    not retargeted yet — the next snapshot will say `behind`).
    """
    base = pr.get("base") or ""
    is_default = bool(default_branch) and base == default_branch
    return {
        "base": base,
        "defaultBranch": default_branch,
        "baseIsDefault": is_default,
        "stacked": not is_default,
        "basePr": base_pr,
    }


def outside_diff_view(
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    viewer: str | None,
) -> list[dict[str, Any]]:
    """Every machine review that listed findings outside the diff, and whether a
    PR comment by the viewer has answered it.

    "Answered" is a comment by the viewer, posted after the review, that names
    the round (`round 6`). That is the one convention the shepherd's replies
    follow so the next cycle — or a restarted one — can see its own work.
    """
    viewer_lower = (viewer or "").lower()
    out: list[dict[str, Any]] = []
    for review in reviews:
        findings = review.get("outsideDiff") or []
        if not findings:
            continue
        submitted = parse_iso(review.get("submittedAt"))
        round_no = review.get("round")
        answered = False
        for comment in comments:
            if (comment.get("author") or "").lower() != viewer_lower:
                continue
            created = parse_iso(comment.get("createdAt"))
            if submitted and created and created < submitted:
                continue
            mentioned = {int(n) for n in _ROUND_MENTION.findall(comment.get("body") or "")}
            if round_no is not None and round_no in mentioned:
                answered = True
                break
        out.append(
            {
                "author": review.get("author"),
                "round": round_no,
                "reviewedCommit": review.get("reviewedCommit"),
                "submittedAt": review.get("submittedAt"),
                "findings": findings,
                "answered": answered,
            }
        )
    return out


def reviewer_view(
    reviews: list[dict[str, Any]],
    requested: list[str],
    viewer: str | None,
    *,
    settled: bool,
) -> dict[str, Any]:
    """Where each human reviewer stands, and who to re-request once settled.

    Machine reviews (round markers, `pr-gate: auto-approve`) are kept out of the
    human columns entirely: an auto-approval is not the sign-off a PR needs, and
    a bot's CHANGES_REQUESTED is not a person waiting on an answer.
    """
    viewer_lower = (viewer or "").lower()
    latest: dict[str, dict[str, Any]] = {}
    machine_approvals: list[str] = []
    for review in reviews:
        author = review.get("author") or ""
        if not author or author.lower() == viewer_lower:
            continue
        if review.get("machine"):
            if review.get("autoApprove") and review.get("state") == "APPROVED":
                machine_approvals.append(author)
            continue
        state = review.get("state")
        if state not in _LIVE_REVIEW_STATES:
            continue
        current = latest.get(author)
        if current is None or (review.get("submittedAt") or "") >= (current["submittedAt"] or ""):
            latest[author] = {"state": state, "submittedAt": review.get("submittedAt")}

    approved = sorted(a for a, r in latest.items() if r["state"] == "APPROVED")
    changes = sorted(a for a, r in latest.items() if r["state"] == "CHANGES_REQUESTED")
    commented = sorted(a for a, r in latest.items() if r["state"] == "COMMENTED")
    pending = sorted(requested)

    targets: list[str] = []
    if settled:
        targets = [a for a in changes if a not in pending]
        if not targets and not approved:
            targets = [a for a in commented if a not in pending]
    return {
        "requested": pending,
        "humanApprovals": approved,
        "changesRequestedBy": changes,
        "commentedBy": commented,
        "machineApprovals": sorted(set(machine_approvals)),
        "reRequest": targets,
        "needsReRequest": bool(targets),
        # Settled, nobody human has approved, nobody is asked, nobody to ask:
        # the shepherd cannot conjure a reviewer. That one goes to the user.
        "noReviewer": settled and not approved and not pending and not targets,
    }


def shepherd_view(
    state: dict[str, Any],
    *,
    now: datetime,
    deadline_days: float = DEFAULT_DEADLINE_DAYS,
    default_branch: str | None = None,
    base_pr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the long-run view to a pr_state snapshot. Pure.

    `state` must be the *unfiltered* snapshot — resolved threads out, answered
    ones in — because a thread the viewer already answered but never resolved is
    exactly the work this loop exists to finish.
    """
    viewer = state.get("viewer")
    pr = state["pr"]
    checks = state["checks"]
    merge = state["merge"]
    threads = state["threads"]
    comments = state.get("comments") or []

    to_answer = [t for t in threads if not t["viewerHasLastWord"]]
    to_close = [t for t in threads if t["viewerHasLastWord"]]
    reopened = [t for t in to_answer if t["answeredByViewer"]]
    outside = outside_diff_view(state.get("reviews") or [], comments, viewer)
    outside_open = [r for r in outside if not r["answered"]]

    failing = checks["failing"]
    pending = checks["pending"]
    open_feedback = bool(threads) or bool(outside_open)
    settled = (
        not failing
        and not pending
        and not open_feedback
        and not merge["conflicted"]
        and not merge["unknown"]
        and not merge["behind"]
    )
    reviewers = reviewer_view(
        state.get("reviews") or [], pr.get("requestedReviewers") or [], viewer, settled=settled
    )
    deadline = deadline_view(comments, viewer, now=now, deadline_days=deadline_days)
    stack = stack_view(pr, default_branch=default_branch, base_pr=base_pr)

    closed = pr.get("state") in ("MERGED", "CLOSED")
    done = closed or (
        settled
        and bool(reviewers["humanApprovals"])
        and pr.get("reviewDecision") != "CHANGES_REQUESTED"
    )
    actionable = (
        bool(failing)
        or open_feedback
        or merge["conflicted"]
        or merge["behind"]
        or reviewers["needsReRequest"]
    )
    waiting = not actionable and (bool(pending) or merge["unknown"])
    if closed:
        status = "closed"
    elif deadline["expired"] and not done:
        status = "expired"
    elif done:
        status = "done"
    elif actionable:
        status = "actionable"
    elif waiting:
        status = "waiting"
    else:
        status = "settled"

    view = dict(state)
    view["shepherd"] = {
        "now": iso(now),
        "deadline": deadline,
        "stack": stack,
        "outsideDiff": outside,
        "reviewers": reviewers,
        "work": {
            "threadsToAnswer": to_answer,
            "threadsToClose": to_close,
            "reopened": reopened,
            "outsideDiffToAnswer": outside_open,
            "failingChecks": failing,
            "conflicted": merge["conflicted"],
            "behind": merge["behind"],
            "reRequest": reviewers["reRequest"],
        },
        "summary": {
            "status": status,
            "settled": settled,
            "done": done,
            "actionable": actionable,
            "waiting": waiting,
            "expired": deadline["expired"],
            "threadsToAnswer": len(to_answer),
            "threadsToClose": len(to_close),
            "reopened": len(reopened),
            "outsideDiffToAnswer": sum(len(r["findings"]) for r in outside_open),
            "noReviewer": reviewers["noReviewer"],
        },
    }
    return view


def exit_code_for(view: dict[str, Any]) -> int:
    summary = view["shepherd"]["summary"]
    if summary["status"] == "expired":
        return EXIT_EXPIRED
    if summary["actionable"]:
        return EXIT_ACTIONABLE
    if summary["waiting"]:
        return EXIT_WAITING
    return EXIT_SETTLED


# --- rendering ----------------------------------------------------------------


def render(view: dict[str, Any]) -> str:
    shep = view["shepherd"]
    lines = [ps.render(view), ""]
    dl = shep["deadline"]
    if dl["started"]:
        when = "PASSED" if dl["expired"] else f"{dl['remainingHours']}h left"
        lines.append(f"Shepherd: started {dl['startedAt']} · deadline {dl['deadline']} ({when})")
    else:
        lines.append("Shepherd: not started — run with --ensure-started to post the marker")
    stack = shep["stack"]
    if stack["stacked"]:
        below = stack["basePr"]
        where = f"PR #{below['number']} ({below.get('state')})" if below else "no open PR"
        lines.append(f"  stacked on {stack['base']} — {where}")
    rv = shep["reviewers"]
    lines.append(
        f"  reviewers: approved={rv['humanApprovals'] or '-'}"
        f" changes-requested={rv['changesRequestedBy'] or '-'}"
        f" requested={rv['requested'] or '-'}"
        f" machine-approvals={rv['machineApprovals'] or '-'}"
    )
    work = shep["work"]
    summary = shep["summary"]
    lines.append(
        f"  work: answer {summary['threadsToAnswer']} thread(s)"
        f" ({summary['reopened']} reopened) · close {summary['threadsToClose']}"
        f" · outside-diff findings {summary['outsideDiffToAnswer']}"
        f" · failing checks {len(work['failingChecks'])}"
        + (" · CONFLICTED" if work["conflicted"] else "")
        + (" · behind base" if work["behind"] else "")
        + (f" · re-request {work['reRequest']}" if work["reRequest"] else "")
    )
    for review in work["outsideDiffToAnswer"]:
        lines.append(f"  round {review['round']} by {review['author']} — outside the diff, unanswered:")
        for finding in review["findings"]:
            lines.append(f"    • [{finding['severity']}] {finding['path']}:{finding['line']} {finding['title']}")
    if summary["noReviewer"]:
        lines.append("  ! settled, but no human has approved and nobody is left to re-request")
    lines.append(f"  status: {summary['status'].upper()}")
    return "\n".join(lines)


# --- I/O boundary -------------------------------------------------------------


def _gh_json(args: list[str]) -> Any:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def detect_default_branch(owner: str, repo: str) -> str | None:
    data = _gh_json(["repo", "view", f"{owner}/{repo}", "--json", "defaultBranchRef"])
    return ((data or {}).get("defaultBranchRef") or {}).get("name")


def find_base_pr(owner: str, repo: str, base: str) -> dict[str, Any] | None:
    rows = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--head",
            base,
            "--state",
            "all",
            "--json",
            "number,state,url,baseRefName",
            "--limit",
            "5",
        ]
    )
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("state") != "OPEN", -int(r.get("number") or 0)))
    return rows[0]


def post_marker(owner: str, repo: str, pr: int, now: datetime, deadline_days: float) -> str:
    body = marker_body(now, now + timedelta(days=deadline_days))
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--repo", f"{owner}/{repo}", "--body", body],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr comment failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, help="PR number (default: current branch's PR)")
    ap.add_argument("--repo", help="owner/name (default: detect via gh)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--exit-code", action="store_true", help="0 settled · 1 actionable · 2 waiting · 4 deadline passed")
    ap.add_argument(
        "--deadline-days",
        type=float,
        default=DEFAULT_DEADLINE_DAYS,
        help=f"give up this many days after the start marker (default {DEFAULT_DEADLINE_DAYS})",
    )
    ap.add_argument(
        "--ensure-started",
        action="store_true",
        help="post the start marker on the PR if there is none (one comment, once)",
    )
    ap.add_argument("--parse-file", help="parse a saved GraphQL payload instead of querying")
    ap.add_argument("--now", help="ISO-8601 'now' for offline runs (default: the clock)")
    ap.add_argument("--default-branch", help="repo default branch for offline runs")
    ap.add_argument("--base-pr", help="JSON for the PR open against the base branch, for offline runs")
    args = ap.parse_args(argv)

    now = parse_iso(args.now) if args.now else datetime.now(UTC)
    if now is None:
        print(f"shepherd_state: cannot parse --now {args.now!r}", file=sys.stderr)
        return EXIT_ERROR

    try:
        if args.parse_file:
            with open(args.parse_file, encoding="utf-8") as fh:
                payload = json.load(fh)
            default_branch = args.default_branch
            base_pr = json.loads(args.base_pr) if args.base_pr else None
            if args.ensure_started:
                raise RuntimeError("--ensure-started needs a live PR, not --parse-file")
        else:
            if args.repo:
                owner, _, name = args.repo.partition("/")
            else:
                owner, name = ps.detect_repo()
            pr = args.pr or ps.detect_pr()
            payload = ps.fetch_payload(owner, name, pr)
            state = ps.parse_pr_state(payload)
            if args.ensure_started and find_marker(state.get("comments") or [], state.get("viewer")) is None:
                post_marker(owner, name, pr, now, args.deadline_days)
                payload = ps.fetch_payload(owner, name, pr)
            default_branch = args.default_branch or detect_default_branch(owner, name)
            base_pr = json.loads(args.base_pr) if args.base_pr else None
            base = ((payload.get("data") or payload)["repository"]["pullRequest"].get("baseRefName")) or ""
            if base_pr is None and base and base != default_branch:
                base_pr = find_base_pr(owner, name, base)
        state = ps.parse_pr_state(payload)
        view = shepherd_view(
            state,
            now=now,
            deadline_days=args.deadline_days,
            default_branch=default_branch,
            base_pr=base_pr,
        )
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"shepherd_state: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(json.dumps(view, indent=2) if args.json else render(view))
    return exit_code_for(view) if args.exit_code else EXIT_SETTLED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
