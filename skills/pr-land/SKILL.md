---
name: pr-land
description: >-
  Drive the current branch's GitHub PR to a landable state, unattended. Loops:
  snapshot the PR's checks and review threads, fix failing checks (delegating
  Azure DevOps builds to /azdo-pr), adjudicate every Cursor Bugbot, Codex, and
  human review comment against the actual code before changing anything, fix what
  is real, reply with evidence to what is not, resolve the threads it settled, push,
  and wait for the next check cycle. Stops when the PR is green with no unanswered
  feedback, when it stops making progress, or when a decision needs a human. Never
  force-pushes and never merges. Trigger with "watch my PR", "get this PR green",
  "land this PR", "fix the review comments", or "babysit this PR".
argument-hint: "[PR number] [--max-cycles N] [--no-push] [--checks-only|--comments-only] [--interval S] [--slack]"
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
until the checks are green and every piece of review feedback has been either
fixed or answered. It is the loop `/azdo-pr` deliberately is not.

Two rules shape everything below:

1. **A review comment is a claim, not an instruction.** Every finding is verified
   against the current code before a line changes. Declining a wrong finding with
   evidence is a success, not a failure. See `references/feedback-triage.md`.
2. **The PR is the state.** There is no local bookkeeping file. Which findings you
   already answered is recorded in your replies on the threads, which fixes landed
   is recorded in the commits, and `scripts/pr_state.py` reads both back. The loop
   is therefore resumable: interrupt it, come back tomorrow, re-invoke it, and it
   picks up correctly with no memory of the first run.

## Arguments

| Argument | Default | Meaning |
|---|---|---|
| `[PR number]` | current branch's PR | Which PR to drive. |
| `--max-cycles N` | `6` | Hard cap on loop iterations. |
| `--interval S` | `60` | Seconds between polls while checks are in flight. |
| `--no-push` | off | Commit locally, never push. The loop cannot converge — it does one pass and reports. |
| `--checks-only` | off | Ignore review threads; only chase failing checks. |
| `--comments-only` | off | Ignore checks; only work the review threads. |
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

Then state the plan before doing anything: PR, failing checks, open threads by
source, and what the first cycle will do. `TodoWrite` one item per finding and per
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
- **`--unanswered`** hides threads you already replied to, which is what makes the
  loop idempotent instead of re-litigating settled findings every cycle.

Print a one-line-per-item digest each cycle. Never dump the raw JSON at the user.

### 2. Decide — everything, before changing anything

Do all the thinking for this cycle up front, then apply in one batch. Interleaving
"fix one thing, push, wait" wastes a full CI cycle per finding.

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
- **Never push to the base branch**, and never `git merge`/`gh pr merge`. Landing
  the PR is the user's decision, not this loop's.
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

After a push, checks take 30s–2m to even queue — azdo especially. Sleep
`--interval` and re-snapshot. Prefer `Bash run_in_background=true` for the waits so
the conversation is not blocked.

## Stopping

Stop and report on the first of these:

| Stop | Meaning |
|---|---|
| **Green** | No failing checks, no pending checks, no unanswered threads. Success. |
| **Waiting only** | Nothing actionable, checks still running, and the user asked for a bounded run. Report the in-flight set. |
| **No progress** | Same failing checks after a cycle in which you pushed a fix for them. Two consecutive no-progress cycles means your model of the failure is wrong. Stop and report — do not keep pushing. |
| **`ASK` outstanding** | A verdict needs the user. Finish everything else first, then ask all the questions at once. |
| **`--max-cycles`** | Report what is left and what you would do next. |
| **Hostile state** | Push rejected after a failed fast-forward, merge conflict, closed PR, expired auth, infra failure (`##[error]` about an agent pool or image pull). Never work around infrastructure. |

## Final report

Always, even on an abort:

```
## /pr-land — PR #945, 3 cycles

Checks:  FAILURE → SUCCESS  (11 passing)
  fixed  etl-service (Lint)   ruff format, 2 files                  a1b2c3d
  fixed  etl-service (Test)   FF silver suffix crash                e4f5g6h

Feedback: 8 threads
  fixed     3  (2 cursor, 1 human)
  declined  3  (3 codex — evidence in the thread replies)
  deferred  1  (pre-existing missing-date gate, predates this branch)
  asked     1  ← needs you: should the null-served case fail or warn?

Pushed:   3 commits to alice/feat-79-…
Left open: 1 human thread (alice disagrees with the decline)
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
- `references/feedback-triage.md` — the four verdicts, the evidence bar, the
  per-source priors, the reply/resolve mutations, and the never-auto-change list.
