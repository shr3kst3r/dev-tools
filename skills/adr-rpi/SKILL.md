---
name: adr-rpi
description: >-
  Research → Plan → Implement with Architecture Decision Records as the durable
  memory layer. Research reads the accepted ADR corpus first so you stop
  re-litigating settled questions and stop contradicting them; Plan authors a
  Proposed ADR for anything architecturally significant and an ordinary plan for
  what's merely mechanical; Implement works against the Accepted ADR and
  supersedes it rather than quietly deviating; a final review pass resolves what
  it finds. Use this for any feature, refactor, migration, or non-trivial bug fix
  where the approach is a real decision — choosing a datastore or library,
  changing a contract or schema, adding a dependency, reworking a boundary, or
  anything a new engineer would later ask "why is it like this?" about. Prefer it
  over plain /rpi whenever the work will outlive its ticket. Skip it only for
  mechanical changes with no decision in them.
when_to_use: >-
  Triggers: "use rpi with ADRs", "write an ADR for this", "what did we already
  decide about X", "is that decision recorded anywhere", "supersede that ADR",
  "plan this migration properly", "why is the code like this", or any ticket whose
  approach is not obvious.
argument-hint: "<context>"
allowed-tools:
  - Read
  - Grep
  - Glob
  - Write
  - Edit
  - Agent
  - Skill
  - "Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/*)"
---

# /adr-rpi — Research → Plan → Implement, backed by ADRs

Ordinary RPI artifacts are ephemeral: `research.md` and `plan.md` get overwritten
on the next run, so every task starts from nothing and the same questions get
re-litigated. This workflow keeps the ephemeral artifacts *and* adds a durable,
accumulating, version-controlled corpus of decisions. Each phase relates to that
corpus differently:

| Phase | Relationship to the corpus |
|---|---|
| Research | **Reads** it, to find what is already settled |
| Plan | **Writes** to it, one `Proposed` ADR per significant decision |
| Implement | **Works against** it, and supersedes rather than deviates |
| Review | **Checks the diff against it**, and resolves what it finds |

Follow the phases in order and **do not skip the gates**. Gates exist because the
corpus is only worth reading if a human accepted everything in it.

## Optional argument

`/adr-rpi <context>` takes free-form context up front — a Linear link, a stack
trace, a customer repro. Treat it as evidence for Phase 1. If it conflicts with
repo facts, say so and ask rather than picking a side.

## Before you start: load the format spec

Invoke the `adr-format` skill now, in the main session. It carries the
significance bar, the frontmatter contract, and the immutability rules. Every
judgment in Phase 2 depends on it, and subagents get it preloaded — the main
session should not be the one working from memory.

## Phase 0 — Scope and corpus

Establish three things before reading any code.

**0.1 The goal.** If it is not obvious which repo/subproject to work in, or what
"done" means, stop and ask: the concrete goal, expected vs actual behavior,
available artifacts (links, traces, payloads), and hard constraints (no upstream
changes, no new dependencies, deadline).

**0.2 The corpus.** Resolve it in this order and report which one you found:

1. `docs/adrs/` — the house default.
2. `docs/adr/`, `designs/adrs/`, `designs/adr/` — accept an existing corpus where
   it already lives rather than creating a second one.
3. Nothing yet → `docs/adrs/`, created in Phase 2 only if an ADR is actually
   warranted. Do not scaffold an empty corpus on the way past.

Set `CORPUS` to that path for the rest of the run.

**0.3 Ephemeral artifact location.** `.context/adr-rpi/` if `.context/` exists,
otherwise `.adr-rpi/`. These hold `research.md` and `plan.md` — the parts that
are genuinely per-task and may be overwritten. Nothing in the corpus is ever
written here, and nothing here is ever treated as a decision record.

**0.4 Design docs, if any.** If `designs/` exists, look for a doc covering this
ticket and read it for background. It is a head start on research, not a
constraint — and in this workflow it is **not** the decision log. Decisions live
in the corpus; a design doc points at ADR ids rather than restating them.

## Phase 1 — Research

Goal: a high-confidence explanation of what is true today, including which
questions are already closed.

### 1.1 Read the corpus first — before the code

Reading the corpus first is what stops you proposing something the team already
rejected, or contradicting a constraint you never saw. Do it in tiers, so you read
three relevant ADRs instead of forty:

```bash
# Everything active, one line each — cheap orientation.
cat $CORPUS/INDEX.md

# The ADRs that actually govern what you are about to touch.
python3 ${CLAUDE_SKILL_DIR}/scripts/adr_chain.py $CORPUS --relevant checkout,sessions
```

- **Accepted and relevant to the components you will touch** → read in full.
- **Accepted, other components** → the one-line row in `INDEX.md` is enough.
- **`Proposed`** → read it and flag it: a decision is in flight and your work may
  collide with it.
- **`Superseded`** → do not read. The ADR that replaced it carries forward what
  the chain concluded. If you need the history, `adr_chain.py $CORPUS --head <id>`
  jumps from any old id to the live one.

If `$CORPUS/CONSTRAINTS.md` exists and the active corpus for your components runs
past roughly eight ADRs, read that instead of the individual files — it is the
"what am I not allowed to do here" projection, and each constraint carries the
ADR id so you can drill into the reasoning when you need to argue with one. Below
that size, read the ADRs directly; a digest of eight files is a lossy copy of
something you could have just read.

If the corpus does not exist yet, say so explicitly in the report. "No ADRs" is a
finding, not an absence of one.

### 1.2 Gather evidence — delegate the reading

High-volume reading goes to subagents so it does not flood this context: call the
`Agent` tool with `subagent_type: "Explore"` and **no** `model` override, so they
inherit the session model. Give each one a specific question, not a topic —
"trace the call path from the checkout mutation to the session write, and list
every place session state is read" beats "look at sessions". Run them in one
message so they go in parallel.

Delegate: call-stack tracing, file discovery, "where else does this pattern
appear", test inventory, log/telemetry archaeology.

Keep in the main session: deciding what the evidence means. Root-cause synthesis
is the judgment this phase exists for, and it needs all the evidence at once.

### 1.3 Synthesize

Pin down, with file paths and line references:

- The failing or missing behavior, and the inputs that produce it.
- The end-to-end path across boundaries: entrypoint → domain logic → data access
  → upstream → storage. At each boundary: what contract is expected, what is
  nullable, where intent is lost.
- Root cause category: data absence, upstream behavior, consumer assumption,
  query construction, schema mismatch, or observability gap.
- Which existing ADRs constrain the fix, and whether any of them look wrong in
  light of what you just learned. Say so now — that is a supersession candidate,
  and Phase 2 is where it gets proposed.

### 1.4 Write the report and stop

Write `<artifacts>/research.md`:

- Symptoms and reproduction inputs
- End-to-end path with evidence paths
- Root cause analysis
- **ADRs consulted** — list the ids, and for each, one line on what it constrains
  or why it turned out irrelevant
- Open questions and anything that looks like a settled decision worth revisiting

The ADR list is not bookkeeping. It is what makes consultation auditable instead
of assumed: a reviewer can see whether you actually read the constraint you are
about to work inside.

**Gate 1.** Stop and ask: "Review `research.md`. What's wrong or missing? Any
constraints I didn't find? Reply `plan` to continue." Do not proceed on inference.

## Phase 2 — Plan

No code changes in this phase. This is the judgment work, and it stays in the main
session on the session model. Never delegate ADR authorship.

### 2.1 Sort the decisions

Walk the changes you are about to propose and apply the significance bar from
`adr-format`:

> An ADR is warranted if reversing the decision later would cost more than a day,
> **or** if a new engineer would be confused about why the code is this way.
> Everything else goes in the plan and dies there.

Be honest in both directions. Under-recording loses the reasoning; over-recording
fills the corpus with noise until the research phase stops reading it, which
costs you the one ADR that mattered. Most tasks produce zero or one ADR. A task
producing four probably contains one real decision and three consequences of it.

### 2.2 Author the ADRs

For each significant decision, copy `${CLAUDE_SKILL_DIR}/assets/adr-template.md`
to `$CORPUS/<YYYY-MM-DD>-<slug>.md` and fill it in. Create `$CORPUS` and drop in
`${CLAUDE_SKILL_DIR}/assets/corpus-README.md` if this is the repo's first ADR.

Non-negotiable, from `adr-format`:

- `status: Proposed`. **You never write `Accepted`.** A human flips it at Gate 2.
- Write `Consequences` with real costs in it. An ADR with only upsides is a sales
  pitch and gets read as one.
- Write `Alternatives considered` honestly. If there were no real alternatives,
  reconsider whether this needed an ADR.
- The ADR precedes the code. No post-hoc records.

**Superseding an existing decision.** If research showed an accepted ADR is wrong,
do not edit it. Author the successor with `supersedes: [<old-id>]`, `status:
Proposed`, and a Context section that states plainly what changed since the
original. Leave the old ADR's `status` and `superseded-by` **untouched** — flipping
those is part of human acceptance at Gate 2, not part of authoring.

### 2.3 Write the plan

`<artifacts>/plan.md`, covering what the ADRs deliberately do not: scope (files
in, files out), sequencing, error-handling policy, observability, tests, rollout.
Describe *what* to build and why, with concrete function and file names — not full
implementations. A single clarifying line of code is fine; a code dump is not.

Reference ADRs by id instead of restating their reasoning. Two copies of a
rationale diverge.

### 2.4 Stop for acceptance

**Gate 2.** Stop and ask, listing each proposed ADR by id:

> Review `plan.md` and the proposed ADR(s): `<ids>`. Accepting an ADR is what
> makes it binding on future work, so it needs your sign-off, not mine. Reply
> `implement` to accept them as written, or tell me what to change.

On approval, and only then:

1. Flip each new ADR's `status` to `Accepted`.
2. For a supersession, flip the old ADR: `status: Superseded`, `superseded-by:
   <new-id>`. These three fields are the only mutation an accepted ADR ever
   receives.
3. Optionally move superseded files into `$CORPUS/superseded/` — a path change,
   not a content change.
4. Regenerate the index and validate the corpus:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/adr_index.py $CORPUS
   python3 ${CLAUDE_SKILL_DIR}/scripts/adr_chain.py $CORPUS --validate
   ```
   Fix any error it reports before writing code. A one-sided supersession link is
   how a decision quietly disappears.

Do not proceed until the user approves.

## Phase 3 — Implement

Delegate to the `adr-implementer` subagent, pinned to Opus. It starts with fresh
context, which is fine: the state it needs is on disk, so it needs paths, not
conversation history.

The accepted ADR is a better handoff artifact than a plan, because it carries
context and consequences rather than just steps — an implementer reading it can
tell a detail it may choose from a constraint it must honor. Hand it both.

Call `Agent` with `subagent_type: "adr-implementer"` and a prompt that names:

- the accepted ADR paths, to read from disk (do not paste their contents)
- `<artifacts>/plan.md`
- the repo's verification commands (`just check`, `npm test`, whatever applies)
- the scope boundary: which files are in play and which are explicitly not

Then hold the line on two things:

**Verification evidence, not claims.** Require quoted command output and named
tests in what it returns. Not because a cheaper model is doing the work — nothing
here is cheaper — but because from this session, a subagent's "all tests pass" is
indistinguishable from a subagent that did not run the tests. Evidence is the only
part you can actually check.

**Supersede, don't deviate.** If implementation proves the accepted decision
wrong, the implementer stops and reports rather than picking the other branch.
That is a Phase 2 event: author a superseding ADR as `Proposed`, go back through
Gate 2. An implementation that quietly diverges leaves a corpus describing a
system nobody is running.

## Phase 4 — Review and resolve

Implementation being complete is not the same as it being right. Delegate a review
pass to the `adr-reviewer` subagent — same model, one tier up on reasoning effort
— and let it fix what it finds.

Call `Agent` with `subagent_type: "adr-reviewer"`, naming:

- the diff to review (`git diff` against the base branch)
- the accepted ADR paths and `<artifacts>/plan.md` as the spec
- the verification commands to re-run after any fix

What it reviews for: correctness against the ADR's Decision and the plan's scope,
bugs and unhandled cases in the new code, missing or weak tests, observability
gaps, and repo-convention drift.

One boundary matters more than the rest: **the reviewer may fix code, never the
architecture.** If the cleanest resolution to a finding is to contradict the
accepted ADR, that is not a fix — it is a supersession, and it goes back through
Gate 2. A review pass empowered to quietly re-decide things is just a second
implementer with less oversight.

### 4.1 Verify in the main session

When it returns, do not take its word for it:

1. Read the diff yourself — both the implementer's work and the reviewer's fixes.
2. Confirm the quoted verification output is real: re-run the repo's checks here.
3. Check the changes against each accepted ADR's Decision line by line. This is
   the one place the corpus gets tested against reality.
4. Only then declare done.

If the reviewer found nothing, say that plainly rather than inventing findings to
justify the phase.

## Phase 5 — Close out

1. **Regenerate the derived artifacts** if any ADR was added or any status
   changed:
   ```bash
   python3 ${CLAUDE_SKILL_DIR}/scripts/adr_index.py $CORPUS
   python3 ${CLAUDE_SKILL_DIR}/scripts/adr_chain.py $CORPUS --validate
   ```
   Regenerate on write, not when the corpus gets big. A projection that refreshes
   on a size threshold is stale exactly when you start relying on it. See
   `references/corpus-operations.md` for `CONSTRAINTS.md` and for wiring this to
   a hook or CI step.
2. **Write `<artifacts>/implementation.md`**: what changed, how it satisfies each
   ADR by id, tests run with results, how to monitor it.
3. **Point design docs at the ADRs.** If a `designs/` doc covers this work, add
   pointers (`decision recorded in ADR <id>`) rather than copying rationale in,
   and note any plan deviations as an addendum without rewriting earlier
   sections.
4. **Same PR.** ADRs, index, code, and tests ship together — a reviewer should see
   the decision and its implementation in one place.
5. List any Linear tickets whose scope drifted and ask before editing them.

## Reference material

- `references/corpus-operations.md` — tiered read in detail, `CONSTRAINTS.md`
  generation, supersession procedure, archiving, regenerate-on-write wiring, and
  why the corpus is never rewritten.
- `references/model-routing.md` — the per-phase routing table, the two ways
  routing degrades silently, and the environment variables that override it.
- `README.md` — the invariants, and how to regenerate everything by hand.
