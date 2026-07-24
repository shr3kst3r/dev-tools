# Architecture Decision Records

Durable records of decisions that would be expensive to reverse or confusing to
encounter cold. One file per decision, named `YYYY-MM-DD-short-slug.md`.

Written and read by the `adr-rpi` workflow, but these are for humans first — the
whole point is that the reasoning survives the people who made it.

## Reading this directory

- **`INDEX.md`** — generated. One row per ADR: status, components, ticket, date.
  Start here.
- **`CONSTRAINTS.md`** — generated, if present. What you are *not allowed to do*,
  by component, each line carrying the ADR id that imposed it.
- **`superseded/`** — decisions that have been replaced. Kept, not deleted; the
  ADR that replaced each one links back.
- Everything else is an active ADR, `Proposed` or `Accepted`.

Both generated files are projections of the ADRs' frontmatter. If one disagrees
with an ADR, the ADR is right and the projection is stale.

## The rules that make this worth trusting

- **An `Accepted` ADR is immutable except its `status`, `supersedes`, and
  `superseded-by` fields.** Wrong or stale is not a reason to edit one — it is a
  reason to supersede it.
- **Only a human sets `status: Accepted`.** Agents write `Proposed` and stop.
  Moving one to `Superseded` needs the same sign-off.
- **Nothing here gets deleted or rewritten.** Git keeps history, but the working
  tree is what the next reader sees, so a decision removed from the tree is gone
  in every way that matters.
- **The ADR precedes the code.** A record written after the fact is
  rationalization with a decision record's formatting.

## Regenerating

```bash
python3 <adr-rpi-skill>/scripts/adr_index.py docs/adrs             # rewrite INDEX.md
python3 <adr-rpi-skill>/scripts/adr_index.py docs/adrs --check     # CI: fail if stale
python3 <adr-rpi-skill>/scripts/adr_chain.py docs/adrs --validate  # check the links
```

The validator catches the failure that actually loses decisions: a supersession
recorded on one end only. Run it after any status change.
