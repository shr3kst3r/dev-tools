---
id: 2026-07-24-adr-backed-rpi
status: Proposed
supersedes: null
superseded-by: null
components: [skills, workflow]
ticket: null
date: 2026-07-24
---
# Keep architectural decisions in an immutable ADR corpus, written by the plan phase

## Context

The existing `op-dev:rpi` workflow produces three artifacts — `research.md`,
`plan.md`, `implementation.md` — all of them per-task and overwritten on the next
run. Whatever reasoning went into a decision survives only as long as the working
copy of the file, so every task starts from nothing. In practice that means the
same questions get re-litigated across tasks, and an agent with no memory of a
prior constraint contradicts it without knowing the constraint exists.

Repos here already reach for something more durable: six of them keep a
`docs/adrs/` directory, two more keep decision notes under `designs/`, and
`op-dev:rpi` itself has a Phase 3.4 that appends to
`designs/<feature>/decisions.md`. So the demand is established; what is missing is
a workflow that reads those records before deciding anything, and rules about who
may change them.

The specific failure mode worth designing against is not an agent that forgets a
decision. It is an agent that finds a decision inconvenient and removes it while
sincerely tidying up. Git would keep the history, but the working tree is what the
next agent reads, so a constraint deleted from the tree is gone in every way that
matters.

## Decision

Architectural decisions live in an append-only corpus of ADR files at
`docs/adrs/`, one decision per file, named `YYYY-MM-DD-<slug>.md`. A new `adr-rpi`
skill reads that corpus in its research phase, authors `Proposed` ADRs in its plan
phase, and implements against `Accepted` ones.

An `Accepted` ADR is immutable except for its `status`, `supersedes`, and
`superseded-by` fields. Only a human flips a status — accepting an ADR and
superseding one are both approval gates. Agents never write `Accepted`, never edit
accepted prose, and never delete ADR source.

`docs/adrs/INDEX.md` is generated deterministically from frontmatter by a script;
`CONSTRAINTS.md`, when the corpus is large enough to need it, is regenerated
wholesale by a model. Both are projections and both are disposable. `op-dev:rpi`
is left untouched, and this skill is a sibling to it.

## Consequences

**Easier.** Research can find what is already settled instead of rediscovering or
contradicting it. Handoff to an implementation subagent improves, because an ADR
carries context and consequences rather than only steps — an implementer can tell
a detail it may choose from a constraint it must honor. Decisions become reviewable
in the PR that implements them.

**Harder, and accepted.** Two extra approval gates per task that produces an ADR,
so a workflow that already stops twice now stops three or four times; this is a
real tax on small work, which is why the significance bar exists to keep most tasks
at zero ADRs. Superseding is deliberately more expensive than editing — the cost
buys the audit trail, and it will feel wrong the first time a record is merely
stale. Two decision logs coexist during the transition, since `op-dev:rpi` keeps
appending to `designs/decisions.md`; design docs are expected to carry pointers to
ADR ids rather than restating them, and nothing enforces that.

**Constraints this imposes.** No agent-run process may rewrite or delete a file
under `docs/adrs/`; the permitted mutation surface of an accepted ADR is exactly
three frontmatter fields. Generated artifacts in the corpus must carry a header
saying they are generated, and must be reproducible from source alone.

**Load-bearing risk.** The corpus is only worth reading if the significance bar
holds. If ADRs get written for choices nobody would question, the research phase
learns the corpus contains nothing load-bearing and stops reading it — at which
point the one ADR that mattered is skipped too, and the system is worse than
having no corpus. The bar is the thing to tune first, ahead of any compaction
machinery.

## Alternatives considered

- **Keep using `designs/<feature>/decisions.md`** (status quo, per `op-dev:rpi`
  Phase 3.4). Decisions are appended after implementation, which makes them
  rationalization rather than decisions, and they are feature-scoped with no
  component index, so nothing can answer "what constrains checkout?" without
  reading every design doc.
- **Sequentially numbered ADRs** (`0007-redis-sessions.md`), the classic Nygard
  convention. Rejected: two agents on two branches both reach for `0007`, git
  merges both cleanly, and a human untangles it. Date prefixes collide only when
  two decisions share a day *and* a slug.
- **Let the skill accept its own ADRs**, gating only the plan. Rejected: an ADR
  nobody signed off on is a model's opinion in a record's clothing, and the corpus
  derives its whole value from being trustworthy on read.
- **A mutable decision log the agent may edit or prune**, kept small by
  consolidation. Rejected for the failure mode in Context: it makes inconvenient
  constraints removable by exactly the agent they constrain.
- **Add ADR reading to `op-dev:rpi` directly** instead of a sibling skill.
  Rejected for now: the phase structure differs enough (a corpus read before
  research, two gates around status changes, a review pass) that folding it in
  would change behavior for everyone already using `/rpi`.
