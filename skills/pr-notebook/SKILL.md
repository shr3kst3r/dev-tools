---
name: pr-notebook
description: >-
  Run a Databricks notebook against the current PR's freshly-built docker image,
  with no pipeline gate. Resolves the image's immutable sha256 digest from ECR so
  Databricks cannot serve a cached older image, submits the notebook on an inline
  new_cluster pinned to that digest (the cluster dies with the run), watches it to
  terminal, triages the outputs for tracebacks, suspicious warnings, empty result
  frames, and run-metadata anomalies, then Slacks a summary. Use when you already
  trust the build and just want the notebook run now. For the gated variant that
  waits on the Azure DevOps pipeline first, use /azdo-then-notebook.
argument-hint: "<notebook-path> [--cluster <id>] [--profile <p>] [--workers <n>] [--node-type <t>] [--summary-only]"
allowed-tools: [Bash, Read, Write, Grep, Glob, Skill]
---

# /pr-notebook — run a notebook against this PR's image

The **ungated** sibling of `/azdo-then-notebook`: same digest pinning, same inline
cluster, same watch/triage/report, but it does not wait on the pipeline. Reach for
it when you know the build is green, or when you are iterating on the notebook
rather than on the code.

## Arguments

| Argument | Required | Default | Meaning |
|---|---|---|---|
| `<notebook-path>` | yes | — | Full Databricks workspace path. **Always quote it** — these paths routinely contain spaces and colons. |
| `--cluster <id>` | no | — | Run on an existing cluster instead. Skips the digest lookup entirely. |
| `--profile <p>` | no | `production-data` | Databricks CLI profile. Pass it on **every** `databricks` call. |
| `--workers <n>` | no | `3` | Worker count for the inline cluster. |
| `--node-type <t>` | no | `i3.8xlarge` | Node type for the inline cluster. |
| `--summary-only` | no | off | Return the summary block instead of sending it to Slack. `/azdo-then-notebook` passes this so the chain Slacks once, not twice. |

## Phase 0 — Sanity check

In parallel:

```bash
gh --version
databricks --version
databricks auth describe --profile "$PROFILE"
aws sts get-caller-identity --profile Administrator-000000000000
command -v slack-me dbtools
gh pr view --json number,headRefName,headRepository,url,title
```

Stop on any failure and say what to fix. A missing `slack-me` is a warning, not a
stop — the run proceeds and the summary prints to the terminal. A missing PR
means this skill does not apply: point the user at the `databricks-tools` skill
(`dbtools submit` / `dbtools follow-run`) instead.

## Phase 1 — Resolve the image digest

Follow `references/ecr-digest.md`. It has the tag-slug transform, the ECR lookup,
and the two conditions that must **stop** the run: no such image, and an image
older than the code. Skip this phase entirely when `--cluster` was passed.

## Phase 2 — Submit on an inline `new_cluster`

Verify the notebook exists first — a typo'd path otherwise costs a full cluster
boot to discover:

```bash
databricks workspace get-status "$NOTEBOOK_PATH" --profile "$PROFILE"
```

Then submit per `references/cluster-spec.md` with `SKILL_NAME=pr-notebook` and
`RUN_SLUG=pr-<PR_NUMBER>`, or use that file's existing-cluster override when
`--cluster` was given.

## Phase 3 — Watch and triage

Per `references/cluster-spec.md` — `dbtools follow-run`, then `notebook pull`,
`notebook triage`, and `runs show`. Four-hour cap, export once at the end.

## Phase 4 — Report

Per `references/summary.md` — Slack draft → confirm → send, then the
`PushNotification` call and the terminal recap. Phases are numbered 2/3/4 there;
drop the pipeline line, which belongs to `/azdo-then-notebook`.

Under `--summary-only`, skip the Slack send and the push notification and return
the composed block to your caller.

## Rules

- **Pin by digest, never by tag.** The `:pr-foo` form reintroduces exactly the
  caching bug this skill exists to defeat.
- **Stop on a missing image.** Never fall back to `:prod`, `:latest`, or the base
  branch — a silent fallback yields a run that looks like it tested the PR and
  did not.
- **`--cluster` means the image is unverified.** Say so in the summary rather than
  letting the run read as digest-pinned.
- **Confirm before starting anything expensive.** A cold multi-node prod cluster
  is real money; get an OK before the first submit.
- **Never push.** This skill only reads the repo. `/pr-land` owns pushing.
- **Do not run `/azdo-pr`.** If the user wanted the gate, they would have invoked
  `/azdo-then-notebook`.

## Examples

```text
/pr-notebook "/Users/you@example.com/magic/20260520: vendor bronze"
/pr-notebook "/Users/you@example.com/magic/20260520: vendor bronze" --cluster 0520-174641-un2qujgw
/pr-notebook "/Users/you@example.com/prod/refresh_gold" --workers 8 --node-type i3.4xlarge
```

## Bundled references

- `references/ecr-digest.md` — the tag-slug transform, the ECR digest lookup, and
  the two hard stops. Shared with `/azdo-then-notebook`.
- `references/cluster-spec.md` — the `dr:*` inline `new_cluster` spec, the
  existing-cluster override, and the watch/triage commands. Shared.
- `references/summary.md` — Slack mrkdwn summary shape, push notification,
  terminal recap. Shared.
