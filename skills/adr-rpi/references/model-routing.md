# Model routing

Phases have genuinely different cost and judgment profiles, so roles are pinned
rather than everything running on whatever the session happens to be.

## The routing table

| Phase | Runs on | Expressed as |
|---|---|---|
| Research — evidence gathering | `Explore` subagents, session model | `Agent(subagent_type: "Explore")` with **no** `model` override |
| Research — root-cause synthesis | Main session | — |
| Plan — ADR authorship | Main session, never delegated | — |
| Implement | `adr-implementer` | `model: opus` in agent frontmatter |
| Review and resolve | `adr-reviewer` | `model: opus`, `effort: xhigh` |
| Final verification | Main session | — |

Two properties make this work better here than in most delegation setups:

**The ADR is a better handoff artifact than a plan.** It carries context and
consequences, not just steps, so an implementer reading an accepted ADR can tell a
detail it may choose from a constraint it must honor. A plan flattens that
distinction into a list of instructions.

**State lives on disk.** Subagents starting with fresh context is mostly a
non-issue: they need a file path, not conversation history. Pass paths, not pasted
contents — a subagent that reads the ADR itself is reading the same bytes the next
agent will.

### Why research delegates at all

Not for cost — the `Explore` agents inherit the session model. For context: a
call-stack trace or a corpus scan returns conclusions instead of flooding the main
window with file contents that get read once. Because they aren't cheap, scope
them tightly: a handful of agents with specific questions, not a broad sweep.

### Why implement and review are pinned up, not down

Implementation is pinned to `opus` rather than a tier below the session. On an
Opus session that is a no-op; it starts mattering the moment the session runs on
Sonnet, where implement stays on Opus instead of following the session down. The
`opus` alias is used rather than `claude-opus-5` so it tracks the current
generation.

Review adds reasoning effort rather than model tier, because there is no tier
above Opus: `effort: xhigh` against a session default of `high`.

### Why the review gate still demands evidence

Verification evidence — quoted build output, named tests — is required not because
a cheaper lane needs keeping honest (nothing here is cheaper) but because from the
main session a subagent's "all tests pass" is indistinguishable from a subagent
that never ran them. Evidence is the only part that can actually be checked. Re-run
the checks in the main session anyway.

## How the routing can degrade silently

Both of these fail quietly. Neither errors.

**`CLAUDE_CODE_SUBAGENT_MODEL` overrides everything.** Model resolution order is:

1. `CLAUDE_CODE_SUBAGENT_MODEL` environment variable
2. the per-invocation `model` parameter on the `Agent` call
3. the agent definition's `model` frontmatter
4. the main conversation's model

So if that variable is set — in your shell, or in an `env` block in
`settings.json` — every pin in the table above is defeated and nothing says so.
Check it before concluding the topology is working:

```bash
echo "${CLAUDE_CODE_SUBAGENT_MODEL:-unset}"
```

As of Claude Code v2.1.196, setting it to `inherit` is equivalent to leaving it
unset. In earlier versions `inherit` forced every subagent onto the session model.

**Models excluded by an org allowlist fall back without erroring.** Claude Code
checks the environment variable, the per-invocation parameter, and the frontmatter
against the organization's `availableModels` allowlist, skips any value that
resolves to an excluded model, and runs the subagent on the inherited model
instead. The same applies to a skill's `model:` field. The observable symptom is a
phase that behaves like the session model while the frontmatter still claims
otherwise — so if a routed phase seems oddly like the main session, suspect this
before rewriting the prompt.

## Other environment behavior worth knowing

**Subagents run in the background by default** as of v2.1.198, and background
subagents keep only a restricted set of built-in tools. `AskUserQuestion` is not
among them, so a backgrounded implementer or reviewer cannot ask you anything —
it can only finish or fail. Both agent definitions therefore set `background:
false`, which keeps their full toolset and makes the main session wait for the
result it is about to verify.

**Permission prompts surface in the main session.** A subagent cannot approve its
own prompts, so the lint and test runs in Phase 3 and Phase 4 raise prompts here,
labeled with the subagent's name. That is expected; it is not the subagent
malfunctioning.

**Subagents cannot spawn subagents** unless
`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` is raised. The topology here is
deliberately one level deep, so this is not needed — but it does mean an
implementer cannot delegate its own reading.

**Preloading has one hard constraint.** A skill with
`disable-model-invocation: true` cannot be preloaded through a subagent's
`skills:` field, because preloading draws from the same set of skills a model may
invoke. `adr-format` is therefore `user-invocable: false` (it is background
knowledge, not a command) but deliberately **not**
`disable-model-invocation` — flipping that would silently stop the discipline
reaching subagents. For the same reason the bundled `/code-review` and `/verify`
skills cannot be preloaded onto `adr-reviewer`; its checklist has to live in its
own definition.

**Write new delegations against `Agent`, not `Task`.** The tool was renamed in
v2.1.63. `Task(...)` still resolves as an alias in older settings and agent files,
but nothing new should use it.
