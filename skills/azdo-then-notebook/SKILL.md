---
name: azdo-then-notebook
description: >-
  Gate a Databricks notebook run on a green Azure DevOps pipeline. Runs /azdo-pr
  until the current branch's PR passes (a green pipeline is what publishes a fresh
  docker image to ECR), then hands off to /pr-notebook to pin that image by its
  immutable sha256 digest, run the notebook on a throwaway cluster, and triage the
  outputs — finishing with one Slack summary covering both halves. Aborts before
  the notebook if the pipeline cannot be made green. Use when the build is not
  green yet, or when you want the whole "land it and verify it" loop to run
  unattended.
argument-hint: "<notebook-path> [--cluster <id>] [--profile <p>] [--workers <n>] [--node-type <t>]"
allowed-tools: [Bash, Read, Write, Grep, Glob, Skill]
---

# /azdo-then-notebook — green pipeline, then the notebook

Two existing skills chained, plus the abort rule between them. Almost nothing is
implemented here: `/azdo-pr` owns the pipeline, `/pr-notebook` owns the image and
the run. **This file owns only the gate and the combined summary** — when the
cluster spec or the digest lookup needs changing, change it in
`skills/pr-notebook/references/`, where both paths read it from.

```
/azdo-pr  ──green──►  /pr-notebook --summary-only  ──►  one Slack summary
    │
    └──red / unfixable──►  abort, Slack the failure, notebook never runs
```

Arguments are exactly `/pr-notebook`'s and are passed straight through. See that
skill's table.

## Phase 0 — Sanity check

`/pr-notebook`'s Phase 0 covers the Databricks/AWS/PR side. Add the azdo side
before spending time on a pipeline you cannot read:

```bash
az account show >/dev/null && az extension show --name azure-devops >/dev/null
```

## Phase 1 — Gate on the pipeline

Invoke `/azdo-pr`. Let it run to completion: it locates the build through the PR's
GitHub check rollup, waits for in-flight jobs, and on failure diagnoses and — when
the cause is in scope — fixes it and commits **locally**.

Capture for the summary: PR number and URL, build id and URL, final state, and any
fix commits it made.

Then branch, and be strict about it:

| `/azdo-pr` outcome | Do |
|---|---|
| Green | Continue to Phase 2. |
| Red, and it committed a fix | The fix is local and unpushed, so the pipeline has **not** re-run. Push it (`git push`) only if the user has authorized pushing — otherwise stop and tell them the fix is waiting. Then re-invoke `/azdo-pr` for the new build. Cap at **2 fix cycles**; past that, abort and report. For an unbounded, self-pushing version of this loop, that is what `/pr-land` is. |
| Red, no fix possible | **Abort.** Skip to Phase 3 and Slack the failure. The notebook does not run — that is this skill's entire reason to exist. |
| No azdo check at all | Do not guess. Report the likely cause (`/azdo-pr` Step 1 enumerates them) and ask whether to proceed ungated — which is just `/pr-notebook`. |

## Phase 2 — Hand off to /pr-notebook

Invoke `/pr-notebook` with the user's arguments plus `--summary-only`, and give it
the azdo build's **start time**. It uses that as the staleness floor for the ECR
digest: an image pushed before the build started means the publish step did not
run for this commit, and pinning it would produce a green-looking run of old code.

`/pr-notebook` returns its composed summary block instead of Slacking it, so the
chain posts exactly once.

## Phase 3 — Slack the combined summary

Per `skills/pr-notebook/references/summary.md`, including the pipeline phase this
time. Prefix the header with `— ABORTED at pipeline step` when Phase 1 aborted, so
the outcome is legible from the notification preview rather than three lines down.

Then the `PushNotification` call and the terminal recap, both per that reference.

## Rules

- **Never start the notebook on a red pipeline.** Everything else here is
  convenience; this is the invariant.
- **Never push without authorization.** `/azdo-pr` commits locally by design. This
  skill inherits that and adds no push of its own.
- **Slack once.** Both halves in one message — hence `--summary-only`.
- **Two fix cycles, then stop.** An unbounded fix/push/re-check loop is `/pr-land`,
  which was designed for it and has the guard rails.

## Examples

```text
/azdo-then-notebook "/Users/you@example.com/magic/20260520: vendor bronze"
/azdo-then-notebook "/Users/you@example.com/prod/refresh_gold" --workers 8
```
