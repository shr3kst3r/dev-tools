---
id: 2026-08-11-issues-get-no-attention-dot
status: Accepted
supersedes: null
superseded-by: null
components: [my-issues]
ticket: "#5"
date: 2026-08-11
---
# Sort `my-issues` by recency alone and ship no attention dot

## Context

`my-prs` opens on a red-or-green dot in its first column, and sorts every list
attention-first. That works because a pull request exposes four crisp,
machine-checkable facts that mean "this needs you": a check is failing, a review
thread is unresolved, the review is missing or changes-requested, or the branch
no longer merges cleanly. Each is a definite answer from GitHub, each is
repo-independent, and each is actionable the moment you read it. The dot is the
tool's whole value proposition — it is why you glance at `my-prs` instead of
opening ten browser tabs.

An issue exposes nothing of the kind. What GitHub gives us is `state`,
`stateReason`, labels, assignees, milestone, comment count and bodies, the issue
body, timestamps, and reactions. None of that answers "does this need me". The
substitutes we considered are all soft, and the softness is not a detail:

- *Assigned but untouched by you* requires attributing the last comment and still
  misreads an issue you are deliberately waiting on.
- *Has an unanswered comment* has no notion of "answered" — the last commenter
  being someone else is normal on a healthy thread.
- *No assignee* is a triage signal about the repo, not about you.
- *Stale* is already what the `updated:` window and the sort convey.
- *A label convention* (`bug`, `priority:high`, `needs-triage`) is the only sharp
  option, and it is not portable — the tool searches every repo you touch, and
  those repos do not agree on label vocabularies. Hardcoding one repo's labels
  into a cross-repo tool makes the dot wrong everywhere else.

A dot that is wrong, or right only sometimes, is worse than no dot. Its entire
function is to be trusted at a glance; once you learn to second-guess it, it
costs a column and buys nothing. This is the risk the decision turns on.

## Decision

`my-issues` has no attention column and no `needs_attention`/`ready` concept. It
sorts every view by `updatedAt` descending, newest first, and nothing else. Its
columns and summary bar report **facts** — labels, assignees, comment count,
age, when you hid it — and never a judgment about whether an issue wants your
attention.

## Consequences

The tool is honest: everything on screen is something GitHub actually told us,
so there is no rule for a reader to learn, distrust, or work around. Recency is
also the right default for the thing an issue dashboard is actually for —
noticing movement across repos you are not watching.

What becomes harder is the glance. `my-issues` cannot be triaged as fast as
`my-prs`; you read rows rather than scanning a dot column, and a long list is
genuinely more work. A stale issue that has quietly become urgent sinks to the
bottom, because staleness and unimportance look identical to a recency sort. We
are accepting that: the alternative is inventing urgency the data does not
support.

The visible asymmetry between the two dashboards is deliberate. A `my-issues`
sitting next to a `my-prs` with a `!` column reads as unfinished, and this record
exists mainly so that nobody "completes" it by inventing an attention heuristic.

Constraints this imposes on future work:

- **Do not add an attention dot, a `needs_attention` property, or an
  attention-first sort to `my-issues` without superseding this ADR.** Reaching for
  parity with `my-prs` is not a reason; a newly available sharp signal is.
- **Do not hardcode label names into `my-issues`' sorting, coloring, or
  filtering.** Labels are rendered as the repo defines them and given no
  semantics. A user-configurable priority-label list would be a new decision, not
  an implementation detail of this one.
- **The summary bar counts facts, not verdicts.** "12 unassigned" is in scope;
  "3 need you" is not.

## Alternatives considered

- **Port the dot with issue-flavored rules** (assigned-and-untouched, unanswered
  comment, no assignee). Rejected: every rule needs a judgment GitHub does not
  make, so the dot would be wrong often enough to stop being glanceable, which
  is the only property that made it worth having in `my-prs`.
- **Drive the dot from a label convention.** The only sharp option, and rejected
  for portability: the tool spans every repo you touch and those repos do not
  share a label vocabulary, so one repo's `priority:high` would leave the dot
  meaningless in the rest.
- **Make the dot user-configurable** (a priority-label list in a config file).
  Defensible, and possibly the right successor to this ADR — but it is a config
  surface, a config file, and a documentation burden bolted onto a tool that does
  not exist yet. Ship the honest version first and let real use argue for it.
- **Sort by comment count or reaction count instead of recency.** Rejected:
  popularity is not urgency, and it buries the quiet issue someone just assigned
  to you — the single most useful row in the list.
