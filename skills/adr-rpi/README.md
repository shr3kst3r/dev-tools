# adr-rpi

Research → Plan → Implement, with Architecture Decision Records as the durable
memory layer instead of per-task scratch files.

A sibling to `op-dev:rpi`, not a replacement for it. Ordinary RPI artifacts are
ephemeral — `research.md` and `plan.md` are overwritten every run, so each task
starts from nothing. ADRs accumulate, are immutable once accepted, and live in
version control, which changes what each phase can rely on:

- **Research** reads the corpus to find what is already settled, so decided
  questions stop being re-litigated and existing constraints stop being
  contradicted.
- **Plan** authors a `Proposed` ADR for anything architecturally significant, and
  an ordinary plan for what is merely mechanical.
- **Implement** works against the `Accepted` ADR, and supersedes it rather than
  deviating when the decision turns out wrong.
- **Review** checks the diff against the ADR and resolves what it finds, without
  authority to re-decide the architecture.

## What's here

```
skills/adr-rpi/
  SKILL.md                        the workflow: phases, gates, delegation
  README.md                       this file
  references/
    corpus-operations.md          tiered read, CONSTRAINTS.md, supersession, regeneration
    model-routing.md              per-phase routing and how it degrades silently
  scripts/
    adr_chain.py                  parse, resolve chains, validate — no model involved
    adr_index.py                  generate INDEX.md from frontmatter
  assets/
    adr-template.md               copy this to start an ADR
    corpus-README.md              dropped into a repo's docs/adrs/ on first use
skills/adr-format/SKILL.md        format spec + significance bar; preloaded into subagents
agents/adr-implementer.md         implement phase, Opus
agents/adr-reviewer.md            review phase, Opus at xhigh effort
```

The scripts are **stdlib-only Python 3** on purpose: they run inside whatever repo
the skill is pointed at, not inside this project's venv, so `python3 script.py`
has to work with nothing installed. That constraint also keeps the frontmatter
dialect small enough to stay hand-editable. Tested against Python 3.11 and 3.13.

## Invariants

These are the load-bearing rules. Everything else is arrangeable.

1. **An `Accepted` ADR is immutable except `status`, `supersedes`, and
   `superseded-by`.** That is the entire permitted mutation surface, and it is how
   supersession stays auditable.
2. **The skill never sets `status: Accepted` itself.** Agents write `Proposed`;
   human approval at the plan gate flips it. Without that, an ADR is just a
   model's opinion wearing a suit.
3. **Moving an ADR to `Superseded` sits behind the same human gate as accepting
   one.** Same weight of decision, opposite direction.
4. **No agent deletes or rewrites ADR source.** Git keeps history, but the working
   tree is what the next agent reads, so a constraint compacted out of the tree is
   functionally gone. This binds hardest when the decision is *inconvenient* — an
   agent must not be able to make a constraint disappear while sincerely
   "consolidating stale decisions."
5. **No post-hoc ADRs.** A record written after the code exists is rationalization
   with a decision record's formatting. The ADR precedes implementation, which is
   why it is authored in the plan phase, behind the gate.
6. **The phase gates are hard.** No proceeding on inference, in either direction.

`scripts/adr_chain.py --validate` enforces the mechanical half of #1 and #3: it
rejects one-sided supersession links, dangling references, status/pointer
mismatches, and cycles. The rest is enforced by the gates and by the prose in
`skills/adr-format/SKILL.md`, which is preloaded into every subagent that could
otherwise get creative.

## Regenerating the derived artifacts

Both are projections of ADR frontmatter and are safe to delete — they come back.

```bash
CORPUS=docs/adrs
SKILL=skills/adr-rpi

python3 $SKILL/scripts/adr_index.py  $CORPUS            # rewrite INDEX.md
python3 $SKILL/scripts/adr_index.py  $CORPUS --check     # CI: exit 1 if stale
python3 $SKILL/scripts/adr_chain.py  $CORPUS --validate  # check the links
python3 $SKILL/scripts/adr_chain.py  $CORPUS --json      # machine-readable corpus
python3 $SKILL/scripts/adr_chain.py  $CORPUS --head 2026-01-01-old   # → live ADR id
python3 $SKILL/scripts/adr_chain.py  $CORPUS --relevant checkout,sessions
```

`INDEX.md` is deterministic and contains no timestamp, so regenerating an
unchanged corpus is a byte-for-byte no-op — which is what makes `--check` usable
in CI and keeps diffs quiet.

`CONSTRAINTS.md` is the model-authored projection and is regenerated **wholesale**,
never edited in place. It only earns its keep past roughly eight active ADRs for
the components in play; below that, research reads the ADRs directly. See
`references/corpus-operations.md`.

Regenerate **on write** — when an ADR is added or a status flips — not when the
corpus gets big. A projection that refreshes on a size threshold is stale exactly
when you have started relying on it. The workflow does this inline; wiring it to a
hook or CI step in a target repo is documented in `references/corpus-operations.md`
and deliberately not installed for you.

## Scope discipline

Tune the significance bar and per-component scoping before reaching for compaction
machinery. If a research phase reads one component's eight ADRs, it needs no
digest. The generator exists; a large corpus is not inevitable. A corpus that needs
aggressive collapsing usually has a significance-bar problem instead.

## Installation notes

Skills live at the repo top level and are bridged into `.claude/` with committed
relative symlinks — see the repo `CLAUDE.md`. Both skills and agent files are
discovered through those symlinks.

Two routing caveats worth knowing before you trust the model pins, both covered in
`references/model-routing.md`: `CLAUDE_CODE_SUBAGENT_MODEL` silently overrides
every pin in the workflow, and a model excluded by an org `availableModels`
allowlist falls back to the session model without erroring.

## Dogfood

`docs/adrs/2026-07-24-adr-backed-rpi.md` records the decision to adopt this system,
written under its own rules — date-prefixed id, real alternatives, costs named in
Consequences, and `status: Proposed`, because the skill is not allowed to accept
its own ADR.
