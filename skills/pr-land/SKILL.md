---
name: pr-land
description: >-
  Drive the current branch's GitHub PR to a landable state, unattended. Loops:
  snapshot the PR's checks, merge conflicts with the base branch, and review
  threads, fix failing checks (delegating Azure DevOps builds to /azdo-pr),
  adjudicate every Cursor Bugbot, Codex, and human review comment against the
  actual code before changing anything, fix what is real, reply with evidence to
  what is not, resolve the threads it settled, push, and wait for the next check
  cycle. Stops when the PR is green with no unanswered feedback, when it stops
  making progress, or when a decision needs a human — and with --merge, merges the
  PR once it is genuinely green. Never force-pushes; never merges without --merge.
  Trigger with "watch my PR", "get this PR green", "land this PR", "fix the review
  comments", "merge it when it goes green", or "babysit this PR".
argument-hint: "[PR number] [--merge[=squash|merge|rebase]] [--max-cycles N] [--no-push] [--checks-only|--comments-only] [--interval S] [--slack]"
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

# /pr-land — drive a PR to landable

A closed loop around one PR: **snapshot → decide → fix → push → wait**, repeated
until the checks are green, the branch merges cleanly into its base, and every
piece of review feedback has been either fixed or answered. It is the loop
`/azdo-pr` deliberately is not.

Three rules shape everything below:

1. **A review comment is a claim, not an instruction.** Every finding is verified
   against the current code before a line changes. Declining a wrong finding with
   evidence is a success, not a failure. See `references/feedback-triage.md`.
2. **The PR is the state.** There is no local bookkeeping file. Which findings you
   already answered is recorded in your replies on the threads, which fixes landed
   is recorded in the commits, and `scripts/pr_state.py` reads both back. The loop
   is therefore resumable: interrupt it, come back tomorrow, re-invoke it, and it
   picks up correctly with no memory of the first run.
3. **Green is a claim too — verify it before acting on it.** GitHub needs a beat
   after every push to queue the new checks and to recompute mergeability, and in
   that window it will happily report the *previous* commit's state. Always sleep
   before believing a post-push snapshot, and always confirm `pr.headSha` is the
   commit you just pushed. Merging on a stale green is the one irreversible
   mistake this loop can make.

## Arguments

| Argument | Default | Meaning |
|---|---|---|
| `[PR number]` | current branch's PR | Which PR to drive. |
| `--max-cycles N` | `6` | Hard cap on loop iterations. |
| `--interval S` | `60` | Seconds between polls while checks are in flight. Never wait less than 60s after a push, whatever this is set to. |
| `--no-push` | off | Commit locally, never push. The loop cannot converge — it does one pass and reports. |
| `--checks-only` | off | Ignore review threads; only chase failing checks. |
| `--comments-only` | off | Ignore checks; only work the review threads. |
| `--merge [method]` | off | Merge the PR once it is genuinely green (see **Merging**). Method is `squash` (default), `merge`, or `rebase`. Without this flag the loop never merges. |
| `--slack` | off | Send a `slack-me` summary when the loop ends. |

## Phase 0 — Preflight, once

Refuse to start rather than discover a problem three commits in. All of these are
stop conditions, not warnings:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/pr_state.py --json > /tmp/pr-land-snapshot.json
git status --porcelain
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD @{upstream} 2>/dev/null
gh auth status
```

| Condition | Why it stops |
|---|---|
| No PR for this branch | Nothing to drive. |
| PR is closed or merged | Nothing to drive. |
| `pr.author` is not `viewer` | Someone else's PR. Confirm explicitly with the user before pushing anything to their branch. |
| Detached HEAD, or the checked-out branch is not the PR's head ref | You would commit to the wrong place. |
| Uncommitted changes unrelated to this work | The user has WIP. Ask; do not stash it for them. |
| No upstream, or local behind/diverged from `origin` | Fetch and fast-forward if clean (`git pull --ff-only`); stop if that fails. Never rebase or reset the user's branch to make room. |
| The PR's base branch is checked out | You are about to commit to `main`. |
| `--merge` on a PR you do not own, or on a draft | Landing someone else's work, or work marked unfinished, is never yours to do. |

A PR that conflicts with its base is *not* a preflight stop — it is the first
thing the cycle works on. See **Conflicts with the base branch**.

Then state the plan before doing anything: PR, failing checks, conflict status,
open threads by source, and what the first cycle will do. `TodoWrite` one item per finding and per
failing check so the user can watch progress.

## The cycle

Run at most `--max-cycles` iterations. Each iteration:

### 1. Snapshot

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/pr_state.py --json --unanswered --exit-code
```

Exit codes: `0` green · `1` actionable work exists · `2` waiting on in-flight
checks · `3` error. The script is stdlib-only and shells out to `gh`, so it runs
in any repo without a venv.

What it gives you that hand-querying does not:

- **`checks.rollup` recomputed after dropping stale re-runs.** GitHub's own
  `statusCheckRollup.state` counts superseded attempts, so a PR whose failing
  check was re-run green still reports `FAILURE`. Real case:
  etl-service#943 reported red on two dead attempts of a check that passed.
  When `reportedRollup` and `rollup` disagree, `rollup` is right.
- **`checks.azdoBuilds[]`** — Azure DevOps build ids parsed out of the check
  details URLs, already grouped so seven job checks are one build.
- **`threads[]`** normalized across Cursor Bugbot, the Codex connector, and
  humans: `source`, `severity`, `title`, chrome-stripped `body`, `locations`
  recovered from Cursor's `LOCATIONS` block, `outdated`, and `answeredByViewer`.
- **`merge`** — GitHub's two mergeability fields folded into one verdict:
  `conflicted`, `behind`, `blocked`, `clean`, `unknown`. A conflicted PR is
  `actionable` even when every check is green, and an `unknown` one is `waiting`,
  not green — GitHub computes mergeability in the background, so it reports
  `UNKNOWN` for the first seconds after every push. `--merge-only` prints just
  this block.
- **`--unanswered`** hides threads you already replied to, which is what makes the
  loop idempotent instead of re-litigating settled findings every cycle.

Print a one-line-per-item digest each cycle. Never dump the raw JSON at the user.

### 2. Decide — everything, before changing anything

Do all the thinking for this cycle up front, then apply in one batch. Interleaving
"fix one thing, push, wait" wastes a full CI cycle per finding.

**Conflicts with the base branch come first.** They invalidate everything else:
fixes written against a stale base can conflict again, and no amount of green
makes a conflicted PR landable. Handle `merge.conflicted` or `merge.behind`
before touching a check or a thread — see the section below.

**Failing checks.** Group by kind:

- azdo builds (`checks.azdoBuilds[]`) → invoke `/azdo-pr`, which finds the build
  from the same check URLs, pulls the failed tasks' logs, diagnoses, and commits
  locally. It never pushes; this loop does.
- GitHub Actions → `gh run view <id> --log-failed` from the check's `url`.
- `Cursor Bugbot` as a *check* is not a failure to fix — its findings arrive as
  review threads, handled below.
- Required-review or merge-state blocks are not fixable by you. Report them.

**Review threads.** For each unanswered thread, get a verdict of `ACCEPT`,
`DECLINE`, `DEFER`, or `ASK` per `references/feedback-triage.md`. Verify against
the **current working tree**, not the diff hunk quoted in the comment — most bot
threads on an active PR are `outdated`, and a real share of them are already fixed
by a later commit.

Delegate the verdicts to the **`pr-feedback-judge`** subagent, one invocation per
finding, in parallel. This is not ceremony: you wrote (or just read) the code under
review, and a judge with no stake in it and a fresh context is measurably less
likely to rationalize a bot's confident-sounding claim into a real defect. Give
each judge the finding, the file, and the PR's intent; it returns a verdict with
cited evidence. If the subagent is unavailable, apply the same rubric yourself and
say in the summary that the verdicts were unreviewed.

Then apply the judges' verdicts — you own the outcome, so overrule one that is
plainly wrong and say why in the summary.

### 3. Fix

Only `ACCEPT` verdicts, plus in-scope check failures. Standard discipline:

- Smallest change that resolves the finding. A bot finding is not license to
  refactor the neighborhood.
- Add or adjust a test when the finding is a behavior claim — a fix with no test
  invites the same comment on the next commit.
- Verify locally before committing: the repo's own gate (`just check`, `just lint`,
  `pytest <the one test>`, `npm test`). Discover it from the `justfile` /
  `package.json` / `pyproject.toml` rather than assuming.
- One commit per logical fix, message naming the source:

```bash
git commit -m "$(cat <<'EOF'
fix(currency): resolve FF silver suffix explicitly

Cursor Bugbot: VendorFull had no FINAL_SILVER_SUFFIX, so validation crashed
for VENDOR-FUNDAMENTALS.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

Never in-scope for automatic change, no matter how confident the comment (route to
`ASK`): product behavior, API or schema contracts, auth/secrets/permissions,
deleting a test or weakening an assertion to get green, migrations or prod-data
writes, dependency bumps beyond what the build needs, anything contradicting an
accepted ADR.

### 4. Push

One push per cycle, after local verification passes.

```bash
git push
```

Hard rules, no exceptions:

- **Never `--force`, never `--force-with-lease`, never amend a pushed commit.** If
  the push is rejected, the remote moved: `git pull --ff-only` and re-verify. If
  that fails, stop and hand it to the user — a divergence means someone else is
  working here.
- **Never push to the base branch.** Merging the base *into* this branch to clear
  a conflict is fine — it is an ordinary commit on your own branch. Merging this
  branch into the base is `gh pr merge`, and it happens only under `--merge`,
  under the conditions in **Merging**.
- Under `--no-push`, stop here with the commits local, report, and exit — the loop
  cannot converge without pushing and pretending otherwise wastes cycles.

### 5. Answer the threads

After the push, so `fixed in <sha>` names a commit that actually exists on the
remote. Reply and resolve per `references/feedback-triage.md`:

- `ACCEPT` → reply `fixed in <sha>` with what changed → resolve.
- `DECLINE` → reply with the evidence → resolve **bot** threads; leave **human**
  threads open and surface them to the user. Resolving a person's disagreement on
  your own PR is not yours to do.
- `DEFER` → reply with what should happen instead → leave open.
- `ASK` → no reply. Bring the question to the user.

### 6. Wait, then loop

**Always sleep at least 60 seconds after a push before you trust a snapshot.**
Checks take 30s–2m to even queue — azdo especially — and mergeability is
recomputed in the background, so a snapshot taken immediately after a push
describes the *previous* commit: the new checks do not exist yet, the old ones
still read green, and `merge.mergeable` comes back `UNKNOWN`. Acting on that
snapshot is how the loop concludes "green, nothing to do" one second after
pushing a change that breaks the build.

```bash
sleep 60
python3 ${CLAUDE_SKILL_DIR}/scripts/pr_state.py --json --unanswered --exit-code
```

Two guards on the snapshot that follows a push:

- `pr.headSha` must equal your local `git rev-parse HEAD`. If it does not,
  GitHub has not caught up (or someone else pushed) — sleep `--interval` again.
- `merge.unknown` means mergeability is still being computed. It counts as
  waiting, never as green. Sleep and re-snapshot.

Then sleep `--interval` between polls while checks are in flight. Prefer
`Bash run_in_background=true` for the waits so the conversation is not blocked.

## Conflicts with the base branch

A PR can be entirely green and still unlandable. `merge.conflicted` (GitHub's
`mergeable: CONFLICTING` or `mergeStateStatus: DIRTY`) is a work item, not a
footnote, and the snapshot marks it actionable for that reason.

List what actually conflicts without touching the working tree:

```bash
git fetch origin "$BASE"
git merge-tree --write-tree --name-only "origin/$BASE" HEAD   # exit 1 ⇒ conflicts
```

Then, by case:

- **`merge.behind`, no conflicts** — the base moved and nothing collides. Merge it
  forward, verify with the repo's gate, and push as an ordinary cycle:

  ```bash
  git merge --no-edit "origin/$BASE"
  ```

  Never rebase to do this. A rebase rewrites pushed commits, and this loop does
  not force-push.
- **Conflicts confined to files this loop changed during this run, resolvable
  mechanically** — resolve them, run the repo's gate, and commit the merge with a
  message that says which side won and why. "Mechanically" means both sides are
  additive and independent (an import list, a test file gaining separate cases),
  not two different implementations of the same function.
- **Anything else** — `git merge --abort`, then stop and report with the
  conflicted paths and what each side changed. Resolving a semantic conflict is a
  decision about the user's code, and a merge resolved wrong is far more expensive
  than one left for a human.

Under `--merge`, a conflict is a hard stop before the merge, never something to
paper over.

## Merging

Only with `--merge`, and only once. Merging is the one action in this skill that
cannot be undone from here, so it is gated on a snapshot you have re-taken, not
on the one that ended the last cycle.

**Confirm green twice, a minute apart.** The checks on a PR you just pushed to
take time to appear; a PR can read green purely because nothing has started yet.

```bash
sleep 60
python3 ${CLAUDE_SKILL_DIR}/scripts/pr_state.py --json --unanswered --exit-code
```

Merge only when *every* one of these holds on that fresh snapshot:

| Gate | Field |
|---|---|
| The snapshot is green | exit code `0` — no failing checks, no pending checks, no unanswered threads |
| It describes your commit | `pr.headSha` == local `git rev-parse HEAD`, and nothing is unpushed |
| It merges cleanly | `merge.conflicted` false, `merge.unknown` false |
| Review is satisfied | `pr.reviewDecision` is not `CHANGES_REQUESTED`, and no human thread was left open |
| It is yours and it is ready | `pr.author` == viewer, `isDraft` false |
| Nothing was punted | no `ASK` or `DEFER` verdict outstanding from any cycle |

If a gate fails, do not merge. Report which gate failed and stop — "green except
for one open human thread" is precisely the case a human should decide.

```bash
gh pr merge "$PR" --squash          # or --merge / --rebase per the flag
```

Then re-read the PR to confirm it actually merged (a merge can be rejected by a
protection rule the snapshot cannot see) and say so in the report. Leave the
branch alone: deleting it is the user's cleanup tool's job, not this loop's.

If `--merge` was asked for but the loop stops for any other reason, say
explicitly in the report that **the PR was not merged** and what blocked it.

## Stopping

Stop and report on the first of these:

| Stop | Meaning |
|---|---|
| **Green** | No failing checks, no pending checks, no unanswered threads, and no conflict with the base. Success — and with `--merge`, the trigger for **Merging**. |
| **Conflict needing judgment** | The branch conflicts with its base in code that is not mechanically resolvable. Report the conflicted paths; do not guess a resolution. |
| **Waiting only** | Nothing actionable, checks still running, and the user asked for a bounded run. Report the in-flight set. |
| **No progress** | Same failing checks after a cycle in which you pushed a fix for them. Two consecutive no-progress cycles means your model of the failure is wrong. Stop and report — do not keep pushing. |
| **`ASK` outstanding** | A verdict needs the user. Finish everything else first, then ask all the questions at once. |
| **`--max-cycles`** | Report what is left and what you would do next. |
| **Hostile state** | Push rejected after a failed fast-forward, closed PR, expired auth, infra failure (`##[error]` about an agent pool or image pull). Never work around infrastructure. |

## Final report

Always, even on an abort:

```
## /pr-land — PR #945, 3 cycles

Checks:  FAILURE → SUCCESS  (11 passing)
  fixed  etl-service (Lint)   ruff format, 2 files                   a1b2c3d
  fixed  etl-service (Test)   FF silver suffix crash                 e4f5g6h

Merge:   CONFLICTING → CLEAN  (merged origin/main forward, 1 file)   b7c8d9e

Feedback: 8 threads
  fixed     3  (2 cursor, 1 human)
  declined  3  (3 codex — evidence in the thread replies)
  deferred  1  (pre-existing missing-date gate, predates this branch)
  asked     1  ← needs you: should the null-served case fail or warn?

Pushed:   4 commits to alice/feat-79-…
Merged:   no — --merge was set, but 1 human thread is still open
Left open: 1 human thread (a reviewer disagrees with the decline)
```

With `--slack`, send the same thing through `slack-me` — Slack mrkdwn, not
Markdown (`*bold*`, `<url|text>`); see the `slack-me` skill.

## Related skills

- **`/azdo-pr`** — one-shot azdo build triage. This loop delegates every azdo
  failure to it. It commits locally; the push is this loop's.
- **`/pr-notebook`**, **`/azdo-then-notebook`** — verify a data-pipeline PR by
  running a notebook against its freshly-built image.
- **`pr-watch`** (a tool, not a skill) — live read-only TUI of one PR's checks and
  threads. Use it to watch what this loop is doing.
- **`/review-pr`**, **`/addr-pr`** — read-only deep review, and the plan-first
  variant of addressing comments. `/pr-land` is the unattended version.

## Bundled files

- `scripts/pr_state.py` — the snapshot. Stdlib-only, `gh` for auth, no venv, so it
  runs in whatever repo you point it at. Pure parse layer, tested in
  `tests/test_pr_land_scripts.py` against real captured bot payloads.
  `--merge-only` prints just the mergeability verdict, which is the cheap way to
  ask "has the conflict cleared yet?" between cycles.
- `references/feedback-triage.md` — the four verdicts, the evidence bar, the
  per-source priors, the reply/resolve mutations, and the never-auto-change list.
