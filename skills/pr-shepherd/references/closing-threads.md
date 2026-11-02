# Closing every thread

The contract `/pr-shepherd` adds on top of `/pr-land`'s verdict rubric
(`../../pr-land/references/feedback-triage.md`): **a thread is closed when the
code is fixed or a comment explains, with evidence, why it is not an issue — and
then it is resolved, whoever opened it.** The rubric decides *what is true*; this
file decides *what happens on the PR*.

## Why humans get resolved too

`/pr-land` leaves a declined human thread open because resolving a person's
disagreement on your own PR looks like dismissing them. The shepherd resolves it
anyway, for three reasons that hold over a week and not over an afternoon:

1. An open thread is a blocker in GitHub's UI and in most "ready to merge"
   checks. Twenty open threads a human has to re-read is why reviewers take nine
   days to come back.
2. Resolving is reversible and visible. A reviewer who disagrees clicks
   *Unresolve* or replies, and the snapshot shows the thread as `reopened` on
   the next cycle. Nothing is hidden; the reply is still there.
3. The evidence is in the reply. "Not applying this: `discover_money_columns`
   raises at line 118 before this runs, covered by `test_…`" is the same answer
   whether the thread is open or closed. What the reviewer needs is the answer,
   not the open state.

The counterweight is the **two-strikes rule**: a reopened thread gets one fresh
verdict with the reviewer's reply as new evidence; the same person pushing back a
second time makes it `ASK`, no matter how sure you are. At that point the
disagreement is between two people and the shepherd steps out.

## Policy by verdict

| Verdict | Reply | Resolve? | Bot thread | Human thread |
|---|---|---|---|---|
| `ACCEPT` | `Fixed in <sha> — …` | yes | yes | yes |
| `DECLINE` | evidence, 1–3 sentences | yes | yes | yes |
| `DEFER` | real, out of scope, tracked at `<pointer>` | yes | yes | yes |
| `ASK` | nothing | no | Slack the question | Slack the question |
| already answered by you, never resolved | nothing | yes | yes | yes, unless your last reply is a question |

`DEFER` requires a pointer. Create one if there is none:

```bash
gh issue create --title "<finding, as a task>" \
  --body "From review of #<PR> (<thread url>): <what and why it is separate work>"
```

If the repo does not use issues, add a `## Follow-ups` section to the PR body
(`gh pr edit --body-file`) and cite it. A deferral with nothing to hold is a
decline wearing a nicer word.

## Reply text

Evidence first, short, no thanks to a bot, no apology to a person. Cite a
`file:line` you read or a command you ran. These extend the rubric's templates.

**Review-bot `important`, fixed:**

```markdown
Fixed in 195c058. V1 gives `max_trade_pct_adv` its own upper bound of 10.0; 2.0
and 5.0 (the optimizer's default) now validate, and 50 is still rejected.
`test_V1_adv_cap_allows_multi_day` pins it.
```

**Review-bot `question`:** answer the question; do not change code because a
question was asked.

```markdown
Targets are active in every mode, so no behaviour change. op-timize measures
exposure targets on active exposure (`e = E @ a`, op-timize
`problem/optimize.py:214-217`), and the objective base is
`custom.minimizeRisk.base` when set — which is what `translate.py:322` sends.
```

**Review-bot `nit`, declined:**

```markdown
Leaving this. `_frac` is used three times in this module and the name is the
one `report.py` already uses for the same thing; a longer name here would be the
odd one out.
```

**Flip-flop:**

```markdown
This is the change round 2 asked for (<thread url>): the risk line was dropped
for weighted targets because the solver trades risk against the target there.
Keeping it out unless there is a case where its absence misleads.
```

**Stack pointer:**

```markdown
This line comes from #647 (`git log -L429,431:op_mcp/optimize_portfolio_tools.py`
shows it landing in a1b2c3d, on that branch). Addressed there — see
<thread url on #647>. This PR does not touch it.
```

**Cross-repo claim, verified:**

```markdown
Verified against op-hops: `python_eggs/op_hops/adv.py:88-104` applies the
override before the market-impact call, and op-hops#1173 (merged) added it.
The "unverified" is the reviewer's view, not the code's.
```

**Already answered, re-posted on a new commit:**

```markdown
Same finding as <earlier thread url>; the answer there stands. Resolving.
```

**Lines outside the diff** — one PR comment per round, round number first:

```markdown
Round 6, lines outside the diff:

- **important** `op_llm_tools/optimization/spec.py:379` — fixed in 4d5e6f7: V37
  now rejects a bound that is not finite after the band is applied; test
  `test_V37_rejects_overflowing_band`.
- **question** `op_llm_tools/optimization/translate.py:628` — plain zeros are
  what the GraphQL schema types as `Float!`; op-timize treats `weight: 0.0` as
  "hold", see `problem/optimize.py:240`. No change.
```

The literal `Round N` is what the next cycle's snapshot matches to mark the
round answered. Keep it.

## Mutations

Reply and resolve are the rubric's two GraphQL calls; `THREAD_ID` is
`threads[].id` from the snapshot.

```bash
gh api graphql -f query='
  mutation($tid: ID!, $body: String!) {
    addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $tid, body: $body}) {
      comment { url }
    }
  }' -f tid="$THREAD_ID" -f body="$BODY"

gh api graphql -f query='
  mutation($tid: ID!) {
    resolveReviewThread(input: {threadId: $tid}) { thread { isResolved } }
  }' -f tid="$THREAD_ID"
```

A PR comment (outside-diff rounds, the round summary):

```bash
gh pr comment "$PR" --body-file reply.md
```

Re-request a reviewer. This works for someone who has already reviewed; GitHub
shows it as a re-request and the login appears in `reviewRequests` until they
act:

```bash
gh api -X POST "repos/$OWNER/$REPO/pulls/$PR/requested_reviewers" \
  -f "reviewers[]=$LOGIN"
```

Never dismiss a review (`dismissReview`). A `CHANGES_REQUESTED` that the
reviewer has not withdrawn is their position; re-requesting asks them to
revisit it, dismissing it overrules them.

## Slack templates

All in mrkdwn. `<url|text>` for links, `*bold*`, no headings, no tables.

**Ask:**

```
*pr-shepherd needs you* — <https://github.com/o/r/pull/649|op-llm-tools#649>
• <https://github.com/o/r/pull/649#discussion_r1|translate.py:255> (slegare): should `weight: 0` mean "ignore the target" or "hold it at today's exposure"? The schema says Float, the docs say nothing, and both readings pass the tests.
Carrying on with the other 6 threads; this one stays open until you answer (reply on the thread, or tell me here).
```

**Settled and re-requested:**

```
*#649 settled* — 0 open threads, checks green, clean on s/ai-283. Round 7: 3 fixed, 4 explained, 1 deferred (→ issue #71). Re-requested slegare. Semantic merge of translate.py from the base (ours kept) — worth a look.
```

**Gave up:**

```
*pr-shepherd gave up on #649* after 7 days (started 2026-11-02).
State: checks green · clean · 0 open threads · re-requested slegare 2026-11-04, no reply since.
Open for you: 1 question (translate.py:255, asked 2026-11-05).
Next: a human approval is all that is left; KarenOP and aprrrrr are also requested. Re-run `/pr-shepherd 649` to start a new clock.
```
