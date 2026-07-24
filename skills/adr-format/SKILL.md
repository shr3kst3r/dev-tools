---
name: adr-format
description: >-
  The house format for Architecture Decision Records and the significance bar
  that decides whether something deserves one. Covers the required frontmatter,
  the four sections, id naming, and the immutability contract — who may change an
  ADR's status and who may not. Consult this before writing, accepting,
  superseding, or judging the significance of any ADR, and whenever you are
  deciding whether a choice belongs in a durable decision record or just in a
  plan. Also consult it when reading an existing ADR and you need to tell a
  detail you may change from a constraint you must honor.
user-invocable: false
---

# ADR format and the significance bar

An ADR is a durable, immutable-once-accepted record of a decision, kept in
version control so the reasoning survives the people who made it. It is not
documentation of what the code does — the code already does that. It is the
answer to "why is it like this, and what would it cost to change?"

This spec is small on purpose. It is preloaded into subagents, so every line
costs context in every delegation.

## The significance bar

Apply this test before writing anything:

> An ADR is warranted if reversing the decision later would cost more than a
> day, **or** if a new engineer would be confused about why the code is this
> way. Everything else goes in the plan and dies there.

Both halves matter. The first catches expensive-to-reverse choices — a datastore,
a wire format, a dependency you will not easily remove. The second catches cheap
choices that look wrong out of context, where the record's whole value is
stopping someone from "fixing" it.

Hold the bar even when a decision feels important in the moment. A corpus full of
records for choices nobody would question is worse than no corpus: the research
phase reads it, finds nothing load-bearing, and learns to skip it. Then the one
ADR that mattered gets skipped too.

Things that are **not** ADRs: naming, file layout, which helper to extract, test
structure, dependency version bumps, anything you would happily redo in an
afternoon.

## File format

One ADR per file, at the repo's corpus root (`docs/adrs/` by default):

```markdown
---
id: 2026-07-24-redis-session-store
status: Proposed          # Proposed | Accepted | Superseded
supersedes: null
superseded-by: null
components: [checkout, sessions]
ticket: PROD-334
date: 2026-07-24
---
# Move checkout sessions from Postgres to Redis

## Context
Forces in play. What's true that makes this a decision rather than an obvious
call. Constraints, deadlines, measured numbers, things you tried already.

## Decision
What we're doing, in one or two sentences, active voice. "We store checkout
sessions in Redis with a 30-minute TTL." Not "it was decided that…".

## Consequences
What becomes easier. What becomes harder. What we're accepting. Name the costs
explicitly — an ADR with only upsides in this section is a sales pitch, and the
next engineer will not trust the rest of it.

## Alternatives considered
Each option, and why not. One or two lines each. If there were no real
alternatives, the decision probably didn't need an ADR.
```

`assets/adr-template.md` in the adr-rpi skill is this, ready to copy.

### Frontmatter

Machine-navigable frontmatter is what makes the corpus readable at scale — the
index and the relevance filter are generated from these fields alone, with no
model involved, so they cannot drift from the source.

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Must equal the filename without `.md`. |
| `status` | yes | `Proposed`, `Accepted`, or `Superseded`. Nothing else. |
| `date` | yes | `YYYY-MM-DD`, the date the ADR was written. |
| `components` | yes in practice | The subsystems this decision governs. Drives which ADRs the research phase reads in full — an ADR with no components never matches a component filter and is effectively invisible. |
| `supersedes` | yes, may be `null` | Id, or list of ids, this ADR replaces. |
| `superseded-by` | yes, may be `null` | Id of the ADR that replaced this one. |
| `ticket` | yes, may be `null` | Linear id, if the decision came from a ticket. |

Only the small YAML subset shown here parses: `key: value`, `key: [a, b]`, block
lists with `- `, `null`/`~` for absent, quotes optional, trailing `#` comments
allowed. Anything fancier is rejected rather than misread, because a decision
record that parses differently than it reads is worse than one that refuses to
parse.

### Ids are date-prefixed

`2026-07-24-redis-session-store.md`: the date, then a slug of the decision.

Never sequential integers. Two agents on two branches both reach for `0007` and
you get a silent collision that git merges cleanly and a human has to untangle.
The Linear id lives in `ticket:`, not in the filename, so an ADR can exist
without a ticket and the link still survives.

## The immutability contract

Once an ADR is `Accepted`, its prose is frozen. The **only** permitted mutation
is to its `status`, `supersedes`, and `superseded-by` fields. That restriction is
what makes supersession auditable: the old reasoning stays legible next to the
new, so a reader can see what changed and why rather than finding a record that
has quietly always said the current thing.

Concretely, when authoring or handling ADRs:

- **Never edit the Context, Decision, Consequences, or Alternatives of an
  accepted ADR.** Wrong, stale, or embarrassing does not matter — supersede it.
- **Never delete or rewrite an ADR file.** Git keeps history, but the working
  tree is what the next agent reads, so a constraint removed from the tree is
  functionally gone. This applies with full force when a decision is
  *inconvenient* to the work in front of you: consolidating, tidying, or
  "cleaning up stale decisions" is exactly what this rule exists to stop.
- **Never write `status: Accepted` yourself.** Author ADRs as `Proposed`. Only a
  human flips a status, at an explicit approval gate. Without that, an ADR is
  just a model's opinion wearing a suit.
- **Moving an ADR to `Superseded` needs the same human gate as accepting one.**
  Same weight of decision, opposite direction. Propose the successor, explain
  what it invalidates, and wait.
- **No post-hoc ADRs.** An ADR written after the code exists is rationalization
  with a decision record's formatting. The ADR precedes implementation. If you
  find yourself documenting a choice already merged, say so plainly and let a
  human decide whether it is worth recording, rather than backdating judgment.

Archiving is the one move that is always safe: relocating a `Superseded` ADR into
`superseded/` changes its path, not its content, and keeps globs over the active
directory honest.

## Reading an ADR as an implementer

When an ADR is handed to you as the spec for work, read it as two different kinds
of statement:

- The **Decision** and the constraints named in **Consequences** are binding. If
  the code you are about to write contradicts them, stop and report it. Do not
  quietly pick the other branch.
- Everything else — structure, naming, sequencing, which helper does what — is
  yours to choose. The ADR deliberately does not specify it.

If implementation proves the Decision itself wrong, that is a real and useful
outcome, and it is not yours to fix by deviating. Report what you hit, with
evidence, and let the decision be superseded through the gate. Silent deviation
leaves a corpus that describes a system nobody is running, which is worse than
having no corpus at all.
