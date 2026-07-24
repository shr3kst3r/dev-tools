---
id: YYYY-MM-DD-short-slug
status: Proposed          # Proposed | Accepted | Superseded — never write Accepted yourself
supersedes: null          # id, or [id, id], of the ADR(s) this replaces
superseded-by: null       # set only when a human accepts a successor
components: []            # subsystems this governs; drives the research phase's tiered read
ticket: null              # Linear id, if this came from a ticket
date: YYYY-MM-DD
---
# Decision in the imperative, as a sentence

## Context

The forces in play. What is true that makes this a decision rather than an obvious
call — constraints, measured numbers, deadlines, what was already tried. Someone
reading this in a year should understand the situation without needing the ticket.

## Decision

What we are doing, in one or two sentences, active voice and present tense. "We
store checkout sessions in Redis with a 30-minute TTL." Not "it was decided that
sessions would be moved."

## Consequences

What becomes easier. What becomes harder. What we are accepting. Name the costs
explicitly — an ADR whose consequences are all upside reads as a sales pitch, and
the next engineer discounts the rest of it accordingly.

State any constraint this imposes on future work as a constraint, since that is
the part later readers need most: "no new session columns in Postgres."

## Alternatives considered

- **Option** — why not, in a line or two.
- **Option** — why not.

If there were no real alternatives, reconsider whether this needed an ADR at all.
