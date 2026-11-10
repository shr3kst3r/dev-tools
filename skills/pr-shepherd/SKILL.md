---
name: pr-shepherd
description: >-
  Tend the current branch's GitHub PR for days, unattended, until every review
  thread is closed and a human has approved it. The long-horizon sibling of
  /pr-land: the same snapshot → decide → fix → push cycle, but it keeps going
  across review rounds, handles review bots that post from a person's account
  (round-numbered important / question / nit findings, findings listed outside
  the diff, auto-approvals that do not count), stacked PRs, and merge conflicts
  with a base that keeps moving. It closes every thread with either a fix or an
  evidence-backed explanation — human threads included — re-requests review from
  the people who asked for changes once everything is addressed, Slacks you when
  a finding needs your call, and gives up with a Slack summary after 7 days.
  Trigger with "shepherd this PR", "tend this PR until it merges", "keep this PR
  moving", "close out the review threads and re-request review", or "babysit
  this PR for a week".
argument-hint: "[PR number] [--deadline-days N] [--interval S] [--tick] [--merge[=squash|merge|rebase]] [--no-push] [--no-slack]"
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Grep
  - Glob
  - Agent
  - Skill
  - TodoWrite
---

# /pr-shepherd — tend a PR until it lands

`/pr-land` gets a PR green once. `/pr-shepherd` stays with it: through the
seventh round of a review bot that never converges, through the human reviewer
who shows up nine days later with six repros, through the base branch moving
under a stack of PRs, and through the weekend. It runs the same cycle as
`/pr-land` — **snapshot → decide → fix → push → answer → wait** — but with a
different contract:

| | `/pr-land` | `/pr-shepherd` |
|---|---|---|
| Horizon | `--max-cycles` (6) | `--deadline-days` (7), clock stored on the PR |
| Declined **human** thread | reply, leave open | reply with evidence, **resolve** |
| Deferred thread | reply, leave open | reply naming the follow-up, **resolve** |
| Review sources | Cursor, Codex, humans | + review bots posting from a person's account, + findings outside the diff |
| Stacked PRs | not aware | finds the PR below; declines findings on its code; rides the retarget when it merges |
| Conflicts | mechanical only, else stop | mechanical and semantic-when-clear; Slack when not |
| After everything is addressed | stop | **re-request review** from whoever asked for changes, then keep waiting |
| After a human approves | stop | keep going — until the PR is **merged** or the deadline passes |
| Telling you | `--slack` at the end | Slack on every decision you have to make, on settled, on approved, on merged, on giving up |

Five rules shape everything below. The first three are `/pr-land`'s; read them
there in full.

1. **A review comment is a claim, not an instruction.** Verify every finding
   against the current code before changing a line
   (`../pr-land/references/feedback-triage.md`).
2. **The PR is the state.** Replies, commits, resolved threads, the hidden start
   marker in the description, and pending review requests are the only memory. Kill the session, come back
   in two days, re-invoke: it resumes correctly.
3. **Green is a claim too.** Sleep after every push before believing a snapshot;
   confirm `pr.headSha` is your commit; `merge.unknown` is waiting, not green.
4. **Every thread ends closed.** A thread is closed when the code is fixed, or
   when a comment explains with evidence why it is not an issue. Humans included:
   a person who disagrees with the explanation will say so, and that reply
   reopens the question for exactly one more exchange before it goes to the user.
   See `references/closing-threads.md`.
5. **When you cannot settle something, say so to a person, not to the PR.** No
   guessing at intent in public. An `ASK` goes to Slack, the rest of the cycle
   carries on, and the thread waits for the answer.
6. **Only a merge or the deadline ends the run.** An approval is a state, not
   a finish line: an approved PR can go stale behind its base, conflict, lose
   its approval to a push, or pick up another review round before anyone
   presses merge. The shepherd keeps polling through all of it.

## Arguments

| Argument | Default | Meaning |
|---|---|---|
| `[PR number]` | current branch's PR | Which PR to tend. |
| `--deadline-days N` | `7` | Run this many days after the start marker, or until the PR is merged, whichever comes first. The marker's own deadline wins on a restart, so a run started with one horizon is not shortened by a restart that used the default. |
| `--interval S` | `1200` | Seconds between polls while nothing is happening. Never below 60 after a push. |
| `--tick` | off | Run exactly one cycle and exit. For driving the loop from outside (`/loop 20m /pr-shepherd 635 --tick`) or for checking what it would do. |
| `--merge [method]` | off | Merge once a **human** has approved and everything is settled, under `/pr-land`'s gates. Machine approvals never satisfy this. |
| `--no-push` | off | Commit locally, never push. One pass, then report. |
| `--no-slack` | off | Report in the terminal only. Default is to Slack, because the point of a week-long run is that you are not watching it. |

## Phase 0 — Preflight, once per invocation

Every `/pr-land` preflight stop applies: no PR, closed PR, not your PR, detached
HEAD, unrelated uncommitted changes, diverged from `origin`, base branch checked
out, `--merge` on a draft. Plus:

```bash
# slack-me is published to ~/bin, which is often not on an agent shell's PATH.
SLACK_ME="$(command -v slack-me || true)"
[ -n "$SLACK_ME" ] || [ ! -x "$HOME/bin/slack-me" ] || SLACK_ME="$HOME/bin/slack-me"
[ -n "$SLACK_ME" ] || echo "NO SLACK"                  # a stop, unless --no-slack
python3 ${CLAUDE_SKILL_DIR}/scripts/shepherd_state.py --json --exit-code --ensure-started \
  > /tmp/pr-shepherd-snapshot.json; echo "exit=$?"
```

- **No `slack-me` and no `--no-slack`** → stop. A shepherd that cannot reach you
  will sit on an `ASK` for a week. Look in `~/bin/slack-me` before concluding it
  is missing — that is where `spg install` puts it — and only then tell the user
  to `spg install` dev-tools or pass `--no-slack` deliberately. Use `$SLACK_ME`
  for every message below.
- **`--ensure-started`** appends the hidden start marker
  (`<!-- pr-shepherd: started … deadline … -->`) to the PR *description* if there
  is none. It renders as nothing, so reviewers see no change and nothing is
  posted on the PR. Every later cycle reads the clock from it. Never add a
  second one, and never put it in a comment — that was the old place, and it
  showed.
- **`shepherd.deadline.expired` already true** → this is a restart after the
  deadline. Report, do not start a new clock; the user decides whether to
  extend (`--deadline-days` only counts from the marker).
- **`shepherd.stack.stacked`** → note the PR below (`stack.basePr`) in the plan;
  it changes how findings and conflicts are read (see **Stacked PRs**).

Once preflight passes, raise the sheep (see **Status line marker**), then state
the plan: deadline, stack, failing checks, conflict state, threads to
answer / to close by source, outside-diff findings unanswered, who is requested
and who asked for changes. `TodoWrite` one item per thread, finding, and check.

## Status line marker

While the shepherd runs, the Claude Code status line shows a sheep (🐑) walking
next to the PR number. The skill can't draw on the UI itself. It keeps a marker
file current, and the status line script (owned by the dotfiles repo, linked to
`~/.claude/statusline.sh`) renders the sheep while the marker is live.

**The contract** (the status line script relies on it, so keep it stable):

- Path: `~/.claude/pr-shepherd/<session id>.running`, one file per Claude Code
  session, so the sheep appears only in the session doing the herding. Two
  sessions in the same repo don't share it.
- Content: one line, `<PR number> <expires epoch seconds>`.
- The status line shows the sheep only while `now < expires`. A run that
  crashes or gets killed loses the sheep on its own and needs no cleanup.

**Raise it / heartbeat.** Do this once preflight passes, then again at the top
of every cycle and before every wait. Use the current `--interval`, so the
marker always outlives the wait plus a cycle's work:

```bash
mkdir -p ~/.claude/pr-shepherd
find ~/.claude/pr-shepherd -name '*.running' -mtime +8 -delete 2>/dev/null   # old crashes
printf '%s %s\n' "<PR number>" "$(( $(date +%s) + <interval> + 900 ))" \
  > ~/.claude/pr-shepherd/${CLAUDE_SESSION_ID}.running
```

**Lower it** on every stop in **Stopping** except `--tick`: merged, closed,
deadline, hostile state, `--no-push`, and when the user cancels the run.

```bash
rm -f ~/.claude/pr-shepherd/${CLAUDE_SESSION_ID}.running
```

Under `--tick`, leave the marker in place unless the tick hit a terminal stop
(merged, closed, deadline). A `/loop` driving ticks then keeps the sheep up
between ticks, and a one-off tick's sheep expires on its own.

The marker is cosmetic. If writing it fails, carry on; never stop a run over it.

## The cycle

Loop until **Stopping** says otherwise. Each cycle:

### 1. Snapshot

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/shepherd_state.py --json --exit-code
```

Exit codes: `0` settled · `1` actionable · `2` waiting on checks or mergeability ·
`3` error · `4` deadline passed. The script wraps `pr-land`'s `pr_state.py`
(same checks dedupe, same mergeability verdict, same thread normalization) and
adds a `shepherd` block:

- **`deadline`** — `startedAt`, `deadline`, `remainingHours`, `expired`.
- **`stack`** — `stacked`, `base`, `basePr` (the open PR whose head is this
  PR's base, or `null`).
- **`work.threadsToAnswer`** — unresolved threads where the last word is not
  yours. Includes `reopened` ones: you answered, and a reviewer came back.
- **`work.threadsToClose`** — unresolved threads where the last word *is* yours.
  Answered by hand or by an earlier run and never resolved. Rule 4 says close
  them.
- **`work.outsideDiffToAnswer`** — machine reviews that listed findings on lines
  outside the diff, with no PR comment of yours naming that round.
- **`work.reRequest`** — who to re-request, non-empty only when settled.
- **`reviewers`** — human approvals, who asked for changes, who is currently
  requested, and `machineApprovals` kept separate because they are not sign-off.
- **`summary.status`** — `actionable` · `waiting` · `settled` · `approved` ·
  `expired` · `merged` · `closed`. Only the last three end the run.

Threads come **unfiltered** (resolved out, answered in). Do not pass
`--unanswered` thinking: a thread you answered and left open is work.

Print a one-line-per-item digest. Never dump the JSON.

### 2. Decide — everything, before changing anything

In this order, because each invalidates the ones after it:

1. **Conflicts and a moved base** (`merge.conflicted`, `merge.behind`) — see
   **Conflicts with the base branch**. Fixes written on a stale base conflict
   again.
2. **Failing checks** — exactly as `/pr-land`: azdo builds to `/azdo-pr`,
   GitHub Actions via `gh run view --log-failed`, Bugbot-as-a-check is not a
   failure, required-review blocks are not yours to fix.
3. **Threads to answer and findings outside the diff** — one
   `pr-feedback-judge` per finding, in parallel, each given the finding, the
   file, the PR's intent, the absolute path to
   `../pr-land/references/feedback-triage.md`, and the two facts the judge cannot
   see: whether the PR is stacked (and what the PR below changed), and any
   earlier thread on the same lines (for flip-flops). Verdicts are `ACCEPT`,
   `DECLINE`, `DEFER`, `ASK`. You own the outcome; overrule a plainly wrong
   verdict and say why in the report.
4. **Threads to close** — no judging. Read your last reply; if it is a fix or
   an explanation, it gets resolved in step 5. If it is a question *you* asked
   the reviewer, leave it and list it in the digest — the shepherd does not ask
   reviewers questions in threads (rule 5), but it will not close a question a
   person asked by hand.

**Reopened threads** get the reviewer's reply as new evidence and one fresh
verdict. If the same person pushes back a second time on the same thread, the
verdict is `ASK` regardless of what you think — two rounds of disagreement on
your own PR is a conversation for the author, not the shepherd.

**Do not re-litigate.** A finding you already declined, re-posted on a new
commit, gets a one-line reply pointing at the earlier thread and is resolved. A
finding asking you to reverse a change you made *for an earlier finding* — the
review bots do this — is declined with a link to that thread, unless the new
one brings evidence the old one lacked.

### 3. Fix

Only `ACCEPT` verdicts and in-scope check failures, under `/pr-land`'s
discipline: smallest change, a test when the finding is a behavior claim, the
repo's own gate before every commit, one commit per logical fix naming the
source and the round:

```bash
git commit -m "$(cat <<'EOF'
fix(validate): give max_trade_pct_adv its own upper bound

Review round 3 (important): V1 capped every fraction at 1.0, but the
optimizer's own ADV default is 5 days, so 2.0 and 5.0 were rejected with
"did you mean v/100?".

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
EOF
)"
```

**Nits** are fixed only when the change is one line and cannot alter behavior
(a test name, a comment, an import moved to the top). Batch every accepted nit
of a round into one commit. A nit that would touch logic is a `DECLINE` with
one sentence, not a refactor. **Questions** are not requests to change code:
answer them with evidence and move on; change code only if answering one
reveals a real defect, and then say so in the reply.

The never-auto-change list is unchanged: product behavior, contracts, auth,
weakening tests, migrations, dependency bumps, anything against an accepted ADR.
Those are `ASK`.

### 4. Push

One push per cycle, after the gate passes. `/pr-land`'s hard rules: never
`--force`, never amend a pushed commit, never push to the base, `git pull
--ff-only` on rejection and stop if that fails. Under `--no-push`, stop here.

### 5. Answer and close

After the push, so `fixed in <sha>` names a commit on the remote. Reply text and
mutations are in `references/closing-threads.md`; the policy is:

| Verdict | Reply | Then |
|---|---|---|
| `ACCEPT` | `Fixed in <sha> — <what changed>, <test that pins it>` | **resolve** |
| `DECLINE` | the evidence, 1–3 sentences | **resolve** — bot *and* human |
| `DEFER` | real, out of scope, and *where it is tracked* | **resolve** |
| `ASK` | nothing on the PR | Slack the question; the thread stays open |
| to-close | nothing more | **resolve** |

Findings outside the diff get **one PR comment per round**, starting with the
round number (`Round 6, lines outside the diff:`) and one bullet per finding
with the same fixed / declined / deferred text. The round number is how the next
cycle knows it was answered.

`DEFER` needs somewhere real to point: a follow-up issue in the repo
(`gh issue create`) if the repo uses issues, otherwise a `Follow-ups` section
in the PR description. "Will do later" with no pointer leaves the reviewer
nothing to hold, and the thread would deserve to stay open.

### 6. Re-request review

When the snapshot is **settled** — no failing or pending checks, no open
threads, no unanswered outside-diff findings, not conflicted or behind — and
`work.reRequest` is non-empty:

```bash
gh api -X POST "repos/$OWNER/$REPO/pulls/$PR/requested_reviewers" \
  -f "reviewers[]=$LOGIN"
```

Then one PR comment summarizing what the round did (counts of fixed / explained /
deferred, the conflict merge if there was one) and naming who was re-requested.
The targets are the humans whose last review was `CHANGES_REQUESTED`; if nobody
asked for changes and nobody has approved, the humans who commented. Someone
already in `requestedReviewers` is never re-requested — a pending request shows
there until they act, which is what makes this idempotent across cycles.

`summary.noReviewer` — settled, no human approval, nobody requested, nobody to
ask — is an `ASK`: Slack it with the PR link and the one-line question "who
should review this?".

### 7. Wait, then loop

**At least 60 seconds after a push**, then `--interval` between polls, in the
background so the conversation is free. Refresh the status line marker before
each wait:

```bash
sleep 60 && python3 ${CLAUDE_SKILL_DIR}/scripts/shepherd_state.py --json --exit-code
```

Guards on the post-push snapshot: `pr.headSha` equals local `HEAD`, and
`merge.unknown` is false. Otherwise sleep again.

While `settled` with a review request pending, there is nothing to do but wait
for a person: poll at `--interval`, and say nothing until something changes. A
new review, a new reply, a new push to the base, or the deadline is what ends
the wait.

**After a human approves (`approved`), keep polling the same way.** The run ends
when the PR is merged (by you under `--merge`, or by anyone) or when the deadline
passes — not at the approval. What can still happen, and what to do:

- **A new review round or reply** — an ordinary cycle; the threads get answered
  and closed as before, and whoever asked for changes is re-requested.
- **`merge.behind`** — GitHub reports this only when branch protection requires
  the branch to be up to date, so merge the base forward. Expect a push to
  dismiss stale approvals in repos that have that rule: `approved` flips back
  and the re-request logic asks the approver again. Do **not** merge the base
  forward on an approved PR that is merely *not* behind — that throws away an
  approval for nothing.
- **`merge.conflicted`** — a **Conflicts** cycle, approval or not.
- **Checks re-run red** (a flaky or a scheduled job) — fix or re-run as usual.
- **Nothing** — poll at `--interval` and stay quiet. Day after day of quiet is
  the expected shape of this phase.

Under `--tick`, stop after step 6 and print the report instead of waiting.

## Review sources

The `/pr-land` priors for Cursor Bugbot, the Codex connector, and humans hold.
The shepherd adds one source and one place feedback hides.

**Review bots that post from a person's account** (`source: review-bot`). A
reviewer's own login runs a review skill against every push, so the author
looks human and only the body says otherwise. The snapshot classifies by the
body (`<!-- deliver-review -->`, the `**important:**` / `**question:**` /
`**nit:**` label) and keeps the same person's hand-written comments as `human`.
What to expect:

- **It runs a round per push and does not converge.** Seven rounds on one PR
  is normal; round seven still finds things. Each round is answered completely
  in one cycle — fix, decline, or defer, every item — so the *next* round has
  only the new diff to look at, not a backlog.
- **`important` / `question` / `nit`** map to the snapshot's `severity`. Four
  in ten findings are nits. Treat the label as the bot's belief, not a fact.
- **It flip-flops.** Round 2 asks you to drop a line; round 3 asks why it is
  gone. Decline the second with a link to the first.
- **It reviews the stack twice.** On a stacked PR, some findings point at code
  the PR below introduced. Decline those with the PR number: the fix belongs
  there, and GitHub's diff on this PR will drop the lines once the base merges.
- **It cannot see other repos.** Findings that call a cross-repo claim
  "unverified" are yours to verify, not to concede: clone the companion repo
  read-only into the scratchpad, cite `repo/path:line`, and if there is a
  companion PR, link it.
- **Its approval is not an approval.** `pr-gate: auto-approve` reviews say
  "please get another engineer to review it properly". The snapshot files them
  under `machineApprovals`; `approved` and `--merge` ignore them.
- **Its round summary can carry findings.** Lines outside the diff cannot take
  a review comment, so they are listed in the review body under *On lines
  outside the diff*. The snapshot surfaces them as `outsideDiff`; answer them in
  a PR comment (step 5).

## Stacked PRs

`shepherd.stack.stacked` means the base is not the default branch, and
`stack.basePr` is the open PR whose head it is. Three consequences:

- **Scope.** `git diff --name-only origin/$BASE...HEAD` is what this PR changed.
  A finding on anything else — a line from the PR below, or from `main` — is
  declined with the pointer, unless this PR's change is what makes it wrong.
- **The retarget.** When the PR below merges and its branch is deleted, GitHub
  retargets this PR to *its* base and the snapshot flips to `merge.behind` or
  `merge.conflicted`. That is an ordinary **Conflicts** cycle: merge the new
  base forward. Expect a review-bot round on the retargeted diff.
- **Order.** The shepherd never merges a stacked PR into a base that is itself
  an open PR, even under `--merge`. It reports "settled, waiting on #<base>".

## Conflicts with the base branch

Handled first in every cycle, and the one place the shepherd is bolder than
`/pr-land`, because it owns the branch for a week and a conflict left for a
human on day two is a PR nobody can review until day nine.

```bash
git fetch origin "$BASE"
git merge-tree --write-tree --name-only "origin/$BASE" HEAD   # exit 1 ⇒ conflicts
```

- **`merge.behind`, no conflicts** — `git merge --no-edit "origin/$BASE"`, run
  the gate, push. Never rebase; this loop does not force-push.
- **Mechanical conflicts** — both sides additive and independent (imports, a
  test file gaining separate cases, a changelog). Resolve, gate, commit.
- **Semantic conflicts where the intent of both sides is clear** — read both
  changes in full (`git log -p origin/$BASE -- <path>` for theirs, this PR's
  commits for yours), and resolve only when you can state in one sentence what
  each side was doing and why the merged result does both. The merge commit
  message lists every conflicted file with which side won and why; the gate
  must pass; and the next Slack digest says a semantic merge happened so the
  user can look. If the repo's gate does not cover the conflicted code, treat
  it as the next case.
- **Competing implementations of the same thing, or anything you cannot
  explain in a sentence** — `git merge --abort`, stop touching the merge, Slack
  the conflicted paths with what each side changed, and carry on with the rest
  of the cycle's work. The conflict stays `actionable`; re-check it every cycle
  in case the user resolves it by hand.

Under `--merge`, a conflict is a hard stop before the merge, never something to
paper over.

## Telling you

Slack, through `$SLACK_ME` as resolved in preflight (`slack-me` on `$PATH`, else
`~/bin/slack-me`), in mrkdwn (`*bold*`, `<url|text>`; see the `slack-me` skill).
One message per event, never a heartbeat:

| Event | Message carries |
|---|---|
| `ASK` | the PR, the thread link, the single question, what you will do meanwhile |
| conflict needing judgment | the conflicted paths and what each side changed |
| settled and re-requested | counts, who was re-requested, whether a semantic merge happened |
| approved | a human approved (by whom); that the run continues until merge |
| merged | who merged it, or that `--merge` did |
| hostile state | push rejected, auth expired, infra failure — and that you stopped |
| **gave up** | the deadline, the state of the PR, every open thread and unanswered question, what you would do next |

Batch: several `ASK`s in one cycle are one message. Never put an ARN, account
id, registry host, or token in a Slack message.

## Merging

Only with `--merge`, under every `/pr-land` gate, plus two: `reviewers.humanApprovals`
is non-empty, and `stack.stacked` is false. Confirm green twice a minute apart,
merge, re-read the PR to confirm it merged, leave the branch alone.

## Stopping

| Stop | Meaning |
|---|---|
| **Merged** | The PR is merged — by `--merge`, or by anyone. Slack it. Success. |
| **Closed** | Someone closed the PR without merging. Report and Slack; nothing to tend. |
| **Deadline** | `summary.status == expired`. Final report, the give-up Slack, stop. Do not start a new clock. |
| **Hostile state** | Push rejected after a failed fast-forward, expired auth, infra failure. Slack, stop. |
| **`--tick`** | One cycle done. Report what happened and what the next cycle would do. |
| **`--no-push`** | Commits local. Report and stop. |

Every stop except `--tick` lowers the status line marker (see **Status line
marker**).

An `ASK` is **not** a stop. The thread stays open, the question is in Slack, and
the loop keeps working everything else and polling until the answer arrives (as
a reply on the thread from you, or an instruction in the terminal) or the
deadline passes.

An **approval** is not a stop either, and neither is `settled`. With no `--merge`
the shepherd cannot finish the PR itself; it tends it until someone does, or
until the deadline, and the give-up report then says "approved by X on <date>,
never merged" — which is the fact the user needs.

## Final report

Always, and the give-up Slack is this in mrkdwn:

```
## /pr-shepherd — PR #649, day 3 of 7, 14 cycles

Checks:   SUCCESS (14 passing)
Merge:    CLEAN — merged origin/s/ai-283… forward twice (1 mechanical, 1 semantic: translate.py, ours)
Stack:    on #647 (OPEN) — 3 findings declined as #647's code

Threads:  31 seen → 0 open
  fixed     17  (3 important, 2 question, 12 nit — 4 commits)
  declined  9   (5 review-bot, 3 human, 1 cursor — evidence in the replies)
  deferred  3   (→ issue #71)
  closed    2   (answered by hand earlier, never resolved)
Outside the diff: rounds 1, 6 answered (comment per round)

Review:   re-requested alice on 2026-11-04 after round 7 · waiting 31h
Asked you: 1 — should a zero weight mean "ignore" or "hold"? (thread on translate.py:255)
Pushed:   6 commits · Merged: no (--merge not set)
```

## Related skills

- **`/pr-land`** — the bounded version: one run to green, human declines left
  open, no re-request. Use it when you will be at the keyboard.
- **`/azdo-pr`** — the azdo failure triage this loop delegates to.
- **`/review-requested`**, **`/request-pr-review`** — whether and where review
  was asked for, by hand; the shepherd re-requests on GitHub only.
- **`pr-watch`** — read-only TUI to watch what the shepherd is doing.

## Bundled files

- `scripts/shepherd_state.py` — the snapshot. Imports `pr-land`'s `pr_state.py`
  through the real path, so it works from the repo or from the published
  symlinks; stdlib-only, `gh` for auth. Pure given the payload plus `--now`,
  `--default-branch`, `--base-pr`, which is how `tests/test_pr_shepherd_scripts.py`
  reproduces every decision offline. `--ensure-started` is its one mutation.
- `references/closing-threads.md` — the closing policy per verdict and source,
  the reply templates (including nits, questions, outside-diff rounds, and
  stack pointers), the re-request and resolve mutations, and the Slack
  templates.
- `../pr-land/references/feedback-triage.md` — the verdict rubric. Not copied;
  the judge reads it from there.
