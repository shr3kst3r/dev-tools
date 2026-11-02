"""Tests for the pr-shepherd skill's snapshot script.

`shepherd_state.py` wraps pr-land's `pr_state.py` (imported through the real
path, the way the published symlinks resolve) and adds the long-run view: the
start marker and deadline, the stack, findings outside the diff, and who to
re-request. Every decision is reproduced here offline from a saved payload plus
an explicit `now`.

Fixtures are anonymised; the review-bot markers are the real ones a review
skill run from a reviewer's account leaves in its comments.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "pr-shepherd" / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ss = _load("shepherd_state")
ps = ss.ps

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
VIEWER = "alice"

REVIEW_BOT_THREAD = """**important:** `_levers` uses `spec[k] not in (None, False, [])`, and \
because 0 == False a lever set to 0 counts as unset.

_TRACED · sweep_
<!-- deliver-review -->
"""

REVIEW_BOT_ROUND_6 = """2 posted, 1 next to an existing comment.

On lines outside the diff:
- **important:** `pkg/spec.py:379` The finite check does not stop the computed bound from overflowing to infinity.

How it fails: value 1e308 passes validation and the sum is inf.
- **question:** `pkg/translate.py:628` Weighted targets send `weight: 0.0` as a plain zero.

<!-- deliver-review-build: skill 0.8.0; engine model-x; sweep model-x; triage model-x; triage_error False; sweep_error False; repeats_seen 1 -->

<!-- deliver-review: round 6, df0b8be5a7e7aa0b102c7525f8521012a05804e4 -->
"""

AUTO_APPROVE = """Approving from my side: nothing here needs my call on architecture, the \
database or security, and an automated review found no critical issues. This is a \
light pass, not a deep review, so please get another engineer to review it properly.

<!-- pr-gate: auto-approve -->
"""


def _thread(tid, author, body, *, resolved=False, replies=()):
    comments = [{"databaseId": 1, "author": {"login": author}, "createdAt": "2026-11-01T00:00:00Z", "body": body}]
    for i, (who, text) in enumerate(replies, start=2):
        comments.append(
            {"databaseId": i, "author": {"login": who}, "createdAt": f"2026-11-01T0{i}:00:00Z", "body": text}
        )
    return {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": False,
        "isCollapsed": False,
        "path": "pkg/a.py",
        "line": 10,
        "originalLine": None,
        "comments": {"nodes": comments},
    }


def _review(author, state, body="", at="2026-11-02T00:00:00Z"):
    return {"author": {"login": author}, "state": state, "submittedAt": at, "body": body}


def _comment(author, body, at="2026-11-02T12:00:00Z"):
    """A comment as pr_state emits it — the shape the pure views consume."""
    return {"author": author, "createdAt": at, "body": body}


def _raw_comment(comment):
    """The same comment as the GraphQL payload carries it."""
    return {"author": {"login": comment["author"]}, "createdAt": comment["createdAt"], "body": comment["body"]}


def _check(name, conclusion, status="COMPLETED"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": None,
        "startedAt": "2026-11-02T00:00:00Z",
        "completedAt": "2026-11-02T00:05:00Z",
    }


def _payload(
    *,
    threads=(),
    reviews=(),
    comments=(),
    checks=None,
    requested=(),
    base="main",
    mergeable="MERGEABLE",
    merge_state_status="CLEAN",
    review_decision=None,
    state="OPEN",
):
    if checks is None:
        checks = [_check("Test", "SUCCESS")]
    return {
        "data": {
            "viewer": {"login": VIEWER},
            "repository": {
                "pullRequest": {
                    "number": 649,
                    "title": "factor targets",
                    "url": "https://github.com/o/r/pull/649",
                    "isDraft": False,
                    "state": state,
                    "baseRefName": base,
                    "headRefName": "feat-649",
                    "headRefOid": "b249da6" + "0" * 33,
                    "updatedAt": "2026-11-02T18:00:00Z",
                    "mergeable": mergeable,
                    "mergeStateStatus": merge_state_status,
                    "reviewDecision": review_decision,
                    "author": {"login": VIEWER},
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": "b249da6" + "0" * 33,
                                    "statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": checks}},
                                }
                            }
                        ]
                    },
                    "reviewRequests": {
                        "nodes": [{"requestedReviewer": {"__typename": "User", "login": r}} for r in requested]
                    },
                    "reviews": {"nodes": list(reviews)},
                    "comments": {"nodes": [_raw_comment(c) for c in comments]},
                    "reviewThreads": {"nodes": list(threads)},
                }
            },
        }
    }


def _view(payload, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("default_branch", "main")
    return ss.shepherd_view(ps.parse_pr_state(payload), **kw)


# --- time and the marker ------------------------------------------------------


def test_parse_iso_accepts_z_and_offsets_and_rejects_junk():
    assert ss.parse_iso("2026-11-03T12:00:00Z") == NOW
    assert ss.parse_iso("2026-11-03T14:00:00+02:00") == NOW
    assert ss.parse_iso("2026-11-03T12:00:00") == NOW
    assert ss.parse_iso("yesterday") is None
    assert ss.parse_iso(None) is None


def test_marker_body_round_trips_through_the_marker_regex():
    started = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    deadline = datetime(2026, 11, 9, 9, 30, tzinfo=UTC)
    body = ss.marker_body(started, deadline)
    match = ss.MARKER_RE.search(body)
    assert match is not None
    assert ss.parse_iso(match.group("started")) == started
    assert ss.parse_iso(match.group("deadline")) == deadline
    assert "2026-11-09" in body.splitlines()[0]  # the visible line says when it gives up


def test_deadline_view_without_a_marker_is_not_started():
    view = ss.deadline_view([], VIEWER, now=NOW)
    assert view == {
        "started": False,
        "startedAt": None,
        "deadline": None,
        "remainingHours": None,
        "expired": False,
    }


def test_deadline_view_counts_days_from_the_marker_when_it_has_no_deadline():
    comments = [_comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T12:00:00Z -->")]
    view = ss.deadline_view(comments, VIEWER, now=NOW, deadline_days=7)
    assert view["startedAt"] == "2026-11-02T12:00:00Z"
    assert view["deadline"] == "2026-11-09T12:00:00Z"
    assert view["remainingHours"] == 144.0
    assert view["expired"] is False


def test_deadline_view_prefers_the_markers_own_deadline_over_the_flag():
    """A restart with the default horizon must not shorten a run started with a longer one."""
    comments = [
        _comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T12:00:00Z deadline 2026-11-16T12:00:00Z -->")
    ]
    view = ss.deadline_view(comments, VIEWER, now=NOW, deadline_days=1)
    assert view["deadline"] == "2026-11-16T12:00:00Z"
    assert view["expired"] is False


def test_deadline_view_expires_and_ignores_other_peoples_markers():
    comments = [
        _comment("bob", "<!-- pr-shepherd: started 2026-11-03T00:00:00Z -->"),
        _comment(VIEWER, "<!-- pr-shepherd: started 2026-10-20T00:00:00Z deadline 2026-10-27T00:00:00Z -->"),
    ]
    view = ss.deadline_view(comments, VIEWER, now=NOW)
    assert view["startedAt"] == "2026-10-20T00:00:00Z"
    assert view["expired"] is True
    assert view["remainingHours"] < 0


def test_find_marker_takes_the_earliest_of_several():
    comments = [
        _comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T00:00:00Z -->"),
        _comment(VIEWER, "<!-- pr-shepherd: started 2026-11-01T00:00:00Z -->"),
    ]
    assert ss.find_marker(comments, VIEWER)["startedAt"] == datetime(2026, 11, 1, tzinfo=UTC)


# --- stack --------------------------------------------------------------------


def test_stack_view_tells_a_default_base_from_a_stacked_one():
    plain = ss.stack_view({"base": "main"}, default_branch="main", base_pr=None)
    assert (plain["stacked"], plain["baseIsDefault"]) == (False, True)
    below = {"number": 647, "state": "OPEN"}
    stacked = ss.stack_view({"base": "s/ai-283"}, default_branch="main", base_pr=below)
    assert (stacked["stacked"], stacked["basePr"]) == (True, below)


def test_stack_view_with_an_unknown_default_branch_assumes_stacked():
    """Better to read a stack that is not there than to miss one that is."""
    assert ss.stack_view({"base": "main"}, default_branch=None, base_pr=None)["stacked"] is True


# --- findings outside the diff ------------------------------------------------


def test_outside_diff_is_unanswered_until_a_viewer_comment_names_the_round():
    reviews = [ps.parse_review(_review("carol", "COMMENTED", REVIEW_BOT_ROUND_6, at="2026-11-02T00:00:00Z"))]
    assert reviews[0]["machine"] and reviews[0]["round"] == 6
    assert [f["severity"] for f in reviews[0]["outsideDiff"]] == ["important", "question"]
    assert "How it fails" in reviews[0]["outsideDiff"][0]["body"]

    nothing = ss.outside_diff_view(reviews, [], VIEWER)
    assert nothing[0]["answered"] is False

    too_early = [_comment(VIEWER, "Round 6, lines outside the diff: …", at="2026-11-01T00:00:00Z")]
    assert ss.outside_diff_view(reviews, too_early, VIEWER)[0]["answered"] is False

    someone_else = [_comment("bob", "Round 6 answered", at="2026-11-02T12:00:00Z")]
    assert ss.outside_diff_view(reviews, someone_else, VIEWER)[0]["answered"] is False

    wrong_round = [_comment(VIEWER, "Round 5, lines outside the diff: …", at="2026-11-02T12:00:00Z")]
    assert ss.outside_diff_view(reviews, wrong_round, VIEWER)[0]["answered"] is False

    answered = [_comment(VIEWER, "Round 6, lines outside the diff:\n- fixed", at="2026-11-02T12:00:00Z")]
    assert ss.outside_diff_view(reviews, answered, VIEWER)[0]["answered"] is True


def test_outside_diff_view_skips_reviews_with_nothing_outside_the_diff():
    reviews = [ps.parse_review(_review("carol", "COMMENTED", "3 posted.\n\n<!-- deliver-review: round 2, abc1234 -->"))]
    assert ss.outside_diff_view(reviews, [], VIEWER) == []


# --- reviewers ----------------------------------------------------------------


def _reviews(*nodes):
    return [ps.parse_review(n) for n in nodes]


def test_reviewer_view_keeps_machine_approvals_out_of_the_human_columns():
    reviews = _reviews(
        _review("carol", "APPROVED", AUTO_APPROVE),
        _review("carol", "COMMENTED", "5 posted.\n<!-- deliver-review: round 1, abc1234 -->"),
    )
    view = ss.reviewer_view(reviews, [], VIEWER, settled=True)
    assert view["machineApprovals"] == ["carol"]
    assert view["humanApprovals"] == []
    assert view["reRequest"] == []
    assert view["noReviewer"] is True


def test_reviewer_view_re_requests_whoever_asked_for_changes_once_settled():
    reviews = _reviews(
        _review("carol", "CHANGES_REQUESTED", "Repros inline.", at="2026-11-01T00:00:00Z"),
        _review("dave", "APPROVED", at="2026-11-01T00:00:00Z"),
    )
    not_yet = ss.reviewer_view(reviews, [], VIEWER, settled=False)
    assert not_yet["changesRequestedBy"] == ["carol"]
    assert not_yet["reRequest"] == []

    settled = ss.reviewer_view(reviews, [], VIEWER, settled=True)
    assert settled["reRequest"] == ["carol"]
    assert settled["needsReRequest"] is True
    assert settled["humanApprovals"] == ["dave"]


def test_reviewer_view_never_re_requests_someone_already_requested():
    reviews = _reviews(_review("carol", "CHANGES_REQUESTED", "Repros inline."))
    view = ss.reviewer_view(reviews, ["carol", "erin"], VIEWER, settled=True)
    assert view["requested"] == ["carol", "erin"]
    assert view["reRequest"] == []
    assert view["noReviewer"] is False


def test_reviewer_view_falls_back_to_commenters_when_nobody_approved_or_blocked():
    reviews = _reviews(_review("carol", "COMMENTED", "Looks mostly fine, two questions inline."))
    assert ss.reviewer_view(reviews, [], VIEWER, settled=True)["reRequest"] == ["carol"]
    # …but not when someone has already approved: the PR is not waiting on a commenter.
    reviews.append(ps.parse_review(_review("dave", "APPROVED")))
    assert ss.reviewer_view(reviews, [], VIEWER, settled=True)["reRequest"] == []


def test_reviewer_view_uses_each_persons_latest_live_review():
    reviews = _reviews(
        _review("carol", "CHANGES_REQUESTED", "no", at="2026-11-01T00:00:00Z"),
        _review("carol", "APPROVED", "ok now", at="2026-11-02T00:00:00Z"),
        _review("dave", "DISMISSED", "", at="2026-11-01T00:00:00Z"),
        _review(VIEWER, "COMMENTED", "replying to myself", at="2026-11-02T00:00:00Z"),
    )
    view = ss.reviewer_view(reviews, [], VIEWER, settled=True)
    assert view["humanApprovals"] == ["carol"]
    assert view["changesRequestedBy"] == []
    assert "dave" not in view["humanApprovals"]  # dismissed says nothing


# --- the whole view -----------------------------------------------------------


def test_work_splits_threads_by_who_has_the_last_word():
    payload = _payload(
        threads=[
            _thread("T1", "carol", REVIEW_BOT_THREAD),  # never answered
            _thread("T2", "carol", "Can this be None?", replies=[(VIEWER, "No: guarded at line 4.")]),
            _thread("T3", "carol", "Rename?", replies=[(VIEWER, "Done in abc."), ("carol", "Still reads wrong.")]),
            _thread("T4", "carol", REVIEW_BOT_THREAD, resolved=True),
        ]
    )
    work = _view(payload)["shepherd"]["work"]
    assert [t["id"] for t in work["threadsToAnswer"]] == ["T3", "T1"]  # humans sort first
    assert [t["id"] for t in work["threadsToClose"]] == ["T2"]
    assert [t["id"] for t in work["reopened"]] == ["T3"]
    assert work["threadsToAnswer"][1]["source"] == "review-bot"
    assert work["threadsToAnswer"][1]["severity"] == "important"


def test_settled_requires_everything_quiet_and_then_names_who_to_re_request():
    reviews = [_review("carol", "CHANGES_REQUESTED", "Repros inline.")]
    marker = _comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T12:00:00Z -->")

    open_thread = _view(_payload(threads=[_thread("T1", "carol", "hm")], reviews=reviews, comments=[marker]))
    assert open_thread["shepherd"]["summary"]["settled"] is False
    assert open_thread["shepherd"]["work"]["reRequest"] == []

    unanswered_round = _view(
        _payload(reviews=reviews + [_review("carol", "COMMENTED", REVIEW_BOT_ROUND_6)], comments=[marker])
    )
    assert unanswered_round["shepherd"]["summary"]["settled"] is False
    assert unanswered_round["shepherd"]["summary"]["outsideDiffToAnswer"] == 2

    behind = _view(_payload(reviews=reviews, comments=[marker], merge_state_status="BEHIND"))
    assert behind["shepherd"]["summary"]["settled"] is False
    assert behind["shepherd"]["summary"]["actionable"] is True

    pending = _view(_payload(reviews=reviews, comments=[marker], checks=[_check("Test", None, status="IN_PROGRESS")]))
    assert (pending["shepherd"]["summary"]["settled"], pending["shepherd"]["summary"]["status"]) == (False, "waiting")

    settled = _view(_payload(reviews=reviews, comments=[marker]))
    summary = settled["shepherd"]["summary"]
    assert summary["settled"] is True
    assert summary["actionable"] is True  # the re-request is the remaining action
    assert settled["shepherd"]["work"]["reRequest"] == ["carol"]
    assert summary["status"] == "actionable"

    requested = _view(_payload(reviews=reviews, comments=[marker], requested=["carol"]))
    assert requested["shepherd"]["summary"]["status"] == "settled"
    assert requested["shepherd"]["summary"]["done"] is False


def test_done_needs_a_human_approval_not_a_machine_one():
    marker = _comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T12:00:00Z -->")
    machine = _view(_payload(reviews=[_review("carol", "APPROVED", AUTO_APPROVE)], comments=[marker]))
    assert machine["shepherd"]["summary"]["done"] is False
    assert machine["shepherd"]["summary"]["noReviewer"] is True

    human = _view(_payload(reviews=[_review("dave", "APPROVED")], comments=[marker], review_decision="APPROVED"))
    assert human["shepherd"]["summary"]["done"] is True
    assert human["shepherd"]["summary"]["status"] == "done"

    overruled = _view(
        _payload(
            reviews=[_review("dave", "APPROVED"), _review("carol", "CHANGES_REQUESTED", "no")],
            comments=[marker],
            review_decision="CHANGES_REQUESTED",
        )
    )
    assert overruled["shepherd"]["summary"]["done"] is False


def test_status_expired_beats_actionable_but_not_done_or_closed():
    old = _comment(VIEWER, "<!-- pr-shepherd: started 2026-10-20T00:00:00Z deadline 2026-10-27T00:00:00Z -->")
    expired = _view(_payload(threads=[_thread("T1", "carol", "hm")], comments=[old]))
    assert expired["shepherd"]["summary"]["status"] == "expired"
    assert ss.exit_code_for(expired) == ss.EXIT_EXPIRED

    done_late = _view(_payload(reviews=[_review("dave", "APPROVED")], comments=[old], review_decision="APPROVED"))
    assert done_late["shepherd"]["summary"]["status"] == "done"

    closed = _view(_payload(threads=[_thread("T1", "carol", "hm")], comments=[old], state="MERGED"))
    assert closed["shepherd"]["summary"]["status"] == "closed"
    assert closed["shepherd"]["summary"]["done"] is True


def test_view_carries_the_stack_and_leaves_pr_state_fields_intact():
    payload = _payload(base="s/ai-283")
    view = _view(payload, base_pr={"number": 647, "state": "OPEN"})
    assert view["shepherd"]["stack"] == {
        "base": "s/ai-283",
        "defaultBranch": "main",
        "baseIsDefault": False,
        "stacked": True,
        "basePr": {"number": 647, "state": "OPEN"},
    }
    assert view["pr"]["number"] == 649
    assert view["checks"]["rollup"] == "SUCCESS"


# --- CLI ----------------------------------------------------------------------


def test_main_reproduces_a_decision_offline(tmp_path, capsys):
    marker = _comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T12:00:00Z -->")
    payload = tmp_path / "p.json"
    payload.write_text(
        json.dumps(_payload(reviews=[_review("carol", "CHANGES_REQUESTED", "no")], comments=[marker])),
        encoding="utf-8",
    )
    code = ss.main(
        [
            "--parse-file",
            str(payload),
            "--now",
            "2026-11-03T12:00:00Z",
            "--default-branch",
            "main",
            "--json",
            "--exit-code",
        ]
    )
    assert code == ss.EXIT_ACTIONABLE
    out = json.loads(capsys.readouterr().out)
    assert out["shepherd"]["work"]["reRequest"] == ["carol"]
    assert out["shepherd"]["deadline"]["remainingHours"] == 144.0


def test_main_exit_codes_for_expired_settled_and_waiting(tmp_path, capsys):
    old = _comment(VIEWER, "<!-- pr-shepherd: started 2026-10-20T00:00:00Z -->")
    expired = tmp_path / "expired.json"
    expired.write_text(json.dumps(_payload(threads=[_thread("T1", "carol", "hm")], comments=[old])), encoding="utf-8")
    assert ss.main(["--parse-file", str(expired), "--now", "2026-11-03T12:00:00Z", "--exit-code"]) == ss.EXIT_EXPIRED
    assert "PASSED" in capsys.readouterr().out

    settled = tmp_path / "settled.json"
    settled.write_text(json.dumps(_payload(requested=["carol"])), encoding="utf-8")
    assert ss.main(["--parse-file", str(settled), "--exit-code", "--default-branch", "main"]) == ss.EXIT_SETTLED
    assert "not started" in capsys.readouterr().out

    waiting = tmp_path / "waiting.json"
    waiting.write_text(json.dumps(_payload(checks=[_check("Test", None, status="QUEUED")])), encoding="utf-8")
    assert ss.main(["--parse-file", str(waiting), "--exit-code"]) == ss.EXIT_WAITING


def test_main_refuses_to_post_a_marker_offline_and_reports_bad_input(tmp_path, capsys):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps(_payload()), encoding="utf-8")
    assert ss.main(["--parse-file", str(payload), "--ensure-started"]) == ss.EXIT_ERROR
    assert "needs a live PR" in capsys.readouterr().err
    assert ss.main(["--parse-file", "/nonexistent.json"]) == ss.EXIT_ERROR
    assert ss.main(["--parse-file", str(payload), "--now", "noon"]) == ss.EXIT_ERROR


def test_render_names_the_work_and_the_stack():
    payload = _payload(
        threads=[_thread("T1", "carol", REVIEW_BOT_THREAD)],
        reviews=[_review("carol", "COMMENTED", REVIEW_BOT_ROUND_6)],
        comments=[_comment(VIEWER, "<!-- pr-shepherd: started 2026-11-02T12:00:00Z -->")],
        base="s/ai-283",
        requested=["erin"],
    )
    text = ss.render(_view(payload, base_pr={"number": 647, "state": "OPEN"}))
    assert "stacked on s/ai-283 — PR #647 (OPEN)" in text
    assert "answer 1 thread(s)" in text
    assert "outside-diff findings 2" in text
    assert "round 6 by carol — outside the diff, unanswered:" in text
    assert "[important] pkg/spec.py:379" in text
    assert "144.0h left" in text
    assert "status: ACTIONABLE" in text
