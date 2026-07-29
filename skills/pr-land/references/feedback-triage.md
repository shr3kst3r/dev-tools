# Adjudicating review feedback

The rubric `/pr-land` and the `pr-feedback-judge` subagent both work from. The
premise: **a review comment is a claim, not an instruction.** Bots produce
confident, well-formatted, wrong findings at a steady rate, and doing what they
say without checking makes the PR worse while looking productive.

## The four verdicts

| Verdict | Means | Action |
|---|---|---|
| `ACCEPT` | The claim is true and in scope for this PR. | Fix it. Reply `fixed in <sha>`. Resolve the thread. |
| `DECLINE` | The claim is false, unreachable, already handled, or out of scope. | Do not touch the code. Reply with the evidence. Resolve **bot** threads; leave **human** threads open and tell the user. |
| `DEFER` | Real, but genuinely belongs in separate work. | Do not fix. Reply saying so and what should happen instead. Leave open. |
| `ASK` | Turns on intent, product behavior, or a trade-off you cannot settle from the repo. | Do nothing on the PR. Surface it to the user with the specific question. |

Every verdict needs **cited evidence**: a `file:line` you actually read, a command
you actually ran with its output, or a repo convention you actually found. A
verdict whose justification is "this looks fine to me" is not a verdict — it is a
guess wearing a verdict's clothes. When you cannot get evidence either way, the
verdict is `ASK`, not `DECLINE`.

## Verify against the working tree, never the diff hunk

`pr_state.py` reports `outdated: true` when the code under a thread has changed
since the comment was written. On real PRs most bot threads are outdated within a
day, and a meaningful share of them are **already fixed** — a later commit
addressed the finding and the bot never withdrew it. So:

1. Read the current file at the current line — not the hunk quoted in the comment.
2. If the concern no longer applies, that is `DECLINE`, and the evidence is the
   current code plus the commit that changed it (`git log -L<start>,<end>:<path>`).
3. Only then evaluate the claim on its merits.

Skipping this step is the single most common way to "fix" something twice, or to
reintroduce a bug a later commit removed.

`lineFrom` in the snapshot tells you how much to trust the line number:
`thread` (GitHub's current line — trustworthy), `original` (where it *was* —
approximate), `locations` (recovered from Cursor's `LOCATIONS` block — the file is
right, the line may have drifted), `none` (find it yourself by symbol name).

## Grounds for DECLINE

Each of these needs the parenthesized evidence attached:

- **Factually wrong about the code** (quote the actual line).
- **Unreachable with the claimed inputs** (show the guard or the caller).
- **Already handled elsewhere** (cite the validation/try/early-return that handles it).
- **Already fixed** (cite the commit).
- **Pre-existing, not introduced by this PR** (`git log`/`git blame` showing it
  predates the branch). Worth a follow-up ticket, not a change here — `DEFER` if
  it is severe.
- **Hallucinated API** — the attribute, kwarg, or method the bot assumes does not
  exist (grep for it and show the miss). Common in Bugbot findings that reason
  about a class it never read.
- **Contradicts a repo convention** (cite `CLAUDE.md`, `AGENTS.md`, or an accepted
  ADR). Note that the Codex connector *cites conventions itself*, in its
  `references` field — check the citation is real and says what it claims.
- **Style preference the repo does not share** (show the prevailing pattern in
  neighboring code).

## Grounds for ACCEPT

- Reproducible defect — you ran something and saw it.
- A crash path you can trace end to end from a plausible caller.
- Correctness, security, or data-loss risk, even if the trigger is unlikely.
- A missing test the reviewer named specifically, for behavior this PR added.
- A real violation of a convention the repo actually enforces.

## Weighing the source

Source changes the prior, never the standard of evidence:

- **Humans** get deference on intent, product behavior, and taste — they know why
  the code exists. They are still wrong about mechanics sometimes. When declining
  a human, reply with the evidence and **leave the thread open**; resolving a
  person's disagreement on your own PR is not yours to do.
- **Cursor Bugbot** (`cursor`) is good at real crash paths and null-handling, and
  weak at cross-file reasoning — it confidently attributes methods to the wrong
  class. Its `**High Severity**` label is a claim, not a finding. Check the class
  it names actually has what it says.
- **Codex connector** (`chatgpt-codex-connector`) is good at "this gate passes
  when it should fail" reasoning and at citing repo conventions, and it tends to
  ask for defensive checks beyond the PR's scope — a frequent `DEFER`.
- **Duplicate findings across bots** are worth more, not less: when Bugbot and
  Codex independently describe the same defect, weight it up. On etl-service#945
  both flagged the same `FINAL_SILVER_SUFFIX` crash — that one was real.

## Do not re-litigate

A declined finding's reply **is** the durable record — that is why
`pr_state.py --unanswered` exists. If a bot re-posts the same finding on a new
commit, do not adjudicate it again; point at the existing reply. Only revisit when
new evidence appears (the code changed in the relevant way, or a human disagreed
with the decline).

## Replying and resolving

Both mutations, verified against the live schema:

```bash
# Reply on a thread. THREAD_ID is `threads[].id` from pr_state.py.
gh api graphql -f query='
  mutation($tid: ID!, $body: String!) {
    addPullRequestReviewThreadReply(
      input: {pullRequestReviewThreadId: $tid, body: $body}
    ) { comment { url } }
  }' -f tid="$THREAD_ID" -f body="$BODY"

# Resolve it.
gh api graphql -f query='
  mutation($tid: ID!) {
    resolveReviewThread(input: {threadId: $tid}) {
      thread { isResolved }
    }
  }' -f tid="$THREAD_ID"
```

### Reply text

Short, specific, and evidence-first. No apologies, no restating the finding back,
no thanking a bot.

Fixed:

```markdown
Fixed in abc1234 — `VendorFull` now defines `FINAL_SILVER_SUFFIX = "fundamentals"`,
and `_resolve_final_silver_table` covers both content sets. Added
`test_resolve_final_silver_table_ff` to pin it.
```

Declined:

```markdown
Not applying this. `money_map` cannot be empty here: `discover_money_columns`
raises `NoMoneyColumnsError` at plugins/…/vendor_currency_qa.py:118 before this
line runs, and `test_discover_requires_companions` covers it. The zero-column
case the comment describes is already a hard failure, not a silent pass.
```

Deferred:

```markdown
Real, but out of scope for this PR — this gate has never checked for missing
requested dates (predates this branch, see 8f21ac0). This PR only adds the
currency check. Filed as follow-up work rather than widening the diff.
```

Ambiguous — do **not** reply, ask the user instead. Guessing at product intent
in public on a PR is worse than a two-line question in the terminal.

## What never gets auto-changed

Route these to `ASK` regardless of how confident the comment is:

- Anything altering product behavior, API shape, or a schema/contract.
- Anything touching auth, secrets, permissions, or PII handling.
- Deleting a test, or weakening an assertion, to make a check pass.
- Migrations, backfills, or anything writing to prod data.
- Version pins and dependency bumps beyond the one the build actually needs.
- A "fix" that would contradict an accepted ADR — supersede it deliberately or
  not at all (see the `adr-format` skill).
