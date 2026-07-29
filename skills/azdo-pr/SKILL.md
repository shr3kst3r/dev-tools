---
name: azdo-pr
description: >-
  Triage the Azure DevOps pipeline for the current branch's GitHub PR. Finds the
  azdo build through the PR's GitHub check rollup (the buildId is embedded in the
  check's details URL), waits for in-flight jobs, and on failure pulls the failed
  tasks' logs and diagnoses them. Applies a fix and commits locally when the root
  cause is in scope — lint/format, a static-analysis line, a missing import or
  dependency, an unambiguously wrong test. Never pushes; the caller owns that.
  Trigger when the user asks why CI/the pipeline is failing on their PR, asks you
  to fix the build, or says "check azdo".
argument-hint: "[PR number] (default: auto-detect from current branch)"
allowed-tools:
  - Bash
  - Read
  - Edit
  - Grep
  - Glob
---

# /azdo-pr — diagnose and fix the PR's Azure DevOps build

One pass over one PR: find the build, wait for it, diagnose the failures, fix
what is safely fixable, commit locally, report. It does **not** loop and it does
**not** push — `/pr-land` is the loop that owns both.

## Environment

- `gh` authed to github.com, and the cwd inside the git repo whose PR this is.
- `az` CLI with the `azure-devops` extension, PAT-authed.
- No `az devops configure` defaults are required. The org and project come out of
  the GitHub check's details URL (see below), and the azdo REST API accepts the
  project **GUID** that URL carries — verified against
  `dev.azure.com/example-org/00000000-…/_build/results?buildId=204627`.

If a call fails, check `gh auth status` and `az account show` before assuming the
pipeline is broken.

## Step 1 — Find the build through the GitHub check rollup

**This is the whole trick, and it replaces pipeline discovery entirely.** Azure
DevOps reports back to GitHub as CheckRuns — one for the build (`etl-service`)
plus one per job (`etl-service (Lint)`, `(Test)`, `(Publish)`, …) — and every one
of them has `buildId=<n>` in its `detailsUrl`. Read that and you are done: no
`az pipelines list`, no guessing between `refs/pull/<N>/merge` and
`refs/heads/<branch>`, no polling for a run that may not have queued yet. GitHub
already knows whether the build exists, and which one belongs to this head SHA.

```bash
PR="${1:-$(gh pr view --json number --jq .number)}"
ROLLUP=$(gh pr view "$PR" --json statusCheckRollup,headRefOid,url,title)

# azdo checks only, newest attempt per check name (see the stale-rerun pitfall).
echo "$ROLLUP" | jq -r '
  .statusCheckRollup
  | map(select((.detailsUrl // .targetUrl // "") | contains("dev.azure.com")))
  | group_by(.name) | map(max_by(.completedAt // .startedAt // ""))
  | .[] | "\(.name)\t\(.status)\t\(.conclusion)\t\(.detailsUrl)"'
```

Extract the coordinates from any one of those URLs:

```bash
URL="https://dev.azure.com/example-org/00000000-1111-2222-3333-444444444444/_build/results?buildId=204627"
ORG=$(echo "$URL"     | sed -E 's|.*dev\.azure\.com/([^/]+)/.*|\1|')
PROJECT=$(echo "$URL" | sed -E 's|.*dev\.azure\.com/[^/]+/([^/]+)/_build.*|\1|')
BUILD_ID=$(echo "$URL" | sed -E 's|.*[?&]buildId=([0-9]+).*|\1|')
```

`/pr-land` already computes all of this — if you were invoked by it, take
`checks.azdoBuilds[]` from its snapshot instead of re-deriving it.

### If there is no azdo check at all

Then the pipeline never posted a status. In order of likelihood: the PR is a
draft or from a fork and PR builds are disabled for those, a path filter
excluded the change, the pipeline is disabled, or it simply has not queued yet
(azdo lags 30s–2m behind a push). Wait once for ~2 minutes and re-read the
rollup. If it is still absent, fall back to searching azdo directly:

```bash
REPO_FULL=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
az pipelines list --query "[?repository.type=='GitHub' && repository.name=='$REPO_FULL'].{id:id,name:name}" -o json
az pipelines runs list --pipeline-ids <id> --branch "refs/pull/$PR/merge" --top 5 \
  --query '[].{id:id,status:status,result:result,sourceVersion:sourceVersion,queue:queueTime}' -o json
```

Report the likely cause and stop. **Do not auto-retrigger** — that is the user's
call.

## Step 2 — Wait for in-flight jobs

If any azdo check is `QUEUED` or `IN_PROGRESS`, poll the rollup every 30s. Say
what you are waiting on before the first poll, then one line per poll — no spam.
Cap at 60 minutes, then ask whether to keep waiting.

```bash
while :; do
  STATES=$(gh pr view "$PR" --json statusCheckRollup --jq '
    [.statusCheckRollup[] | select((.detailsUrl // "") | contains("dev.azure.com"))
     | select(.status != "COMPLETED")] | length')
  [[ "$STATES" == "0" ]] && break
  echo "  …$STATES azdo check(s) still running at $(date +%H:%M:%S)"
  sleep 30
done
```

Prefer `Bash run_in_background=true` for long waits so the conversation is not
blocked and you get notified on completion.

## Step 3 — Diagnose a failed build

Failed leaf tasks live in the timeline with `result == "failed"` and
`type == "Task"`; each carries the `log.id` you need.

```bash
az devops invoke --area build --resource timeline \
  --route-parameters project="$PROJECT" buildId="$BUILD_ID" \
  --api-version 7.1-preview \
  --query "records[?result=='failed' && type=='Task'].{name:name,log:log.id,parent:parentId,finish:finishTime}" \
  -o json
```

Then each failed task's log:

```bash
az devops invoke --area build --resource logs \
  --route-parameters project="$PROJECT" buildId="$BUILD_ID" logId="$LOG_ID" \
  --api-version 7.1-preview --http-method GET \
  --query-parameters '$format=json' --query 'value' -o json
```

The response is a JSON **array of lines**, each prefixed with an ISO timestamp —
not raw text — and test runners emit **ANSI color codes** inside them, which break
naive greps and look like garbage when quoted into a summary. Strip them first:

```bash
az devops invoke … --query 'value' -o json \
  | jq -r '.[]' \
  | sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g'
```

Logs run to thousands of lines; grep rather than reading whole:

| Pattern | Means |
|---|---|
| `##[error]` | Azure Pipelines error marker — start here, but see below |
| `##[section]Finishing:` | task boundaries, for locating the failure's neighborhood |
| `Traceback (most recent call last):` | Python — the *last* line is the cause |
| `FAILED tests/…::test_…` | pytest failure list |
| `error TS`, `npm ERR!`, `ruff`, `mypy:`, `E\d\d\d ` | toolchain-specific |
| `##[warning]` | only promote to a cause if it explains a later failure |

**`##[error]` is usually not the root cause.** In practice the last error line is
`##[error]Bash exited with code '1'` — the shell reporting that the step failed,
which you already knew. The cause is *above* it, in the tool's own output. Real
case, build 205127: the only `##[error]` was the exit code, and the actual failure
was `FAIL … conversation-surface.stories.tsx` sixty lines earlier. Find the last
`##[error]`, then read backwards from it to the preceding
`##[section]Starting:` for the tool output that explains it.

Summarize each failed task as **task name → one-line root cause → `file:line`**,
with the build URL. If two tasks failed for the same reason, say so once.

## Step 4 — Fix, if it is in scope

In scope — the log names the problem and the repair is local to this repo:

- **Lint/format** — run the project's own formatter, discovered from the
  `justfile` / `package.json` / `pyproject.toml` (`just lint`, `ruff format .`,
  `prettier --write`). Never hand-edit what a formatter will rewrite.
- **Static analysis** (mypy, ty, tsc, ruff check) — fix the reported lines.
- **Missing or unused import.**
- **Missing dependency** — add it to the manifest the project actually uses.
- **A failing test whose intent is unambiguous** — read the test *and* the source
  first. If which one is wrong is a judgment call, stop and report.

Out of scope — report, do not guess:

- Infra: agent pool offline, image pull failure, expired secret, permission error.
- Flakes: recommend a re-run instead of "fixing" the test.
- Anything needing credentials or services you cannot reach.
- Anything that changes product behavior without clear user intent.

**Reproduce locally before committing** whenever the repo makes it cheap
(`just check`, `just lint`, `pytest <the one test>`). A fix that only "looks
right" costs a full pipeline cycle to disprove.

### Commit

```bash
git add <only the files you touched>
git commit -m "$(cat <<'EOF'
fix(ci): <root cause> in <task name>

Build: https://dev.azure.com/<org>/<project>/_build/results?buildId=<BUILD_ID>

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

One commit per logical fix. **Never `git push`** — this skill is deliberately
local-only so it is safe to invoke on someone else's branch or mid-review. When
`/pr-land` calls this skill, `/pr-land` pushes.

## Step 5 — Report

Per build: name, final state, URL. For each failure: the diagnosis in one line,
plus either the fix and its commit SHA or why no fix was attempted. One `✓` line
if everything is green. Terse.

## Pitfalls

- **Stale re-run duplicates.** A re-run does not replace the old CheckRun in the
  rollup — both are returned, and `statusCheckRollup.state` counts the dead
  failure. Real case: etl-service#943 reported `FAILURE` while the only failing
  entries were two superseded attempts of a check that later passed. Always
  `group_by(.name) | max_by(.completedAt)` before believing the rollup.
- **Job checks vs the build check.** `etl-service` and `etl-service (Lint)`
  share one `buildId`. Group by buildId or you will diagnose the same build
  seven times.
- **`line: null` on the timeline.** Task records carry no source line; the log is
  the only place a `file:line` exists.
- **`az devops invoke` 401** — PAT expired or missing scope. Tell the user; never
  try to recover credentials silently.
- **Project GUID is fine.** Do not translate the GUID from the check URL into a
  project name — the REST API takes either.
