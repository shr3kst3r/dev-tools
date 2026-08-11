# dev-tools

Personal developer tools: live terminal dashboards for the systems I spend the
day waiting on — GitHub PRs, Airflow, Azure DevOps Pipelines — alongside the
agent skills and subagents I work with. **One uv project, one tool per
directory** under `tools/`, all configured from a single `pyproject.toml`.

## Toolchain

| Concern            | Tool                                    |
| ------------------ | --------------------------------------- |
| Tool versions      | [asdf](https://asdf-vm.com/) (pins `uv`, `just`) |
| Packaging / env    | [uv](https://docs.astral.sh/uv/) (Python 3.13) |
| Type checking      | [ty](https://github.com/astral-sh/ty)   |
| Tests              | [pytest](https://docs.pytest.org/)      |
| Git hooks          | [pre-commit](https://pre-commit.com/)   |
| Task runner        | [just](https://github.com/casey/just)   |

Two version-manager layers with clear ownership: **asdf** pins the bootstrap
CLIs (`uv`, `just`) in `.tool-versions`; **uv** owns the Python interpreter
(`.python-version`) and the venv dev tools (`ty`, `pytest`, `pre-commit`).
Python is deliberately *not* in `.tool-versions` to keep a single source of
truth for the interpreter.

## Getting started

```bash
asdf install  # install pinned uv + just (needs asdf + the uv/just plugins)
just sync     # create .venv and install everything (incl. dev group)
just hooks    # install the pre-commit git hook
just check    # ty + pytest — run before pushing
just          # list every recipe

# only needed for the pr-notebook / azdo-then-notebook skills:
cp dev-tools.env.example ~/.dev-tools.env && chmod 600 ~/.dev-tools.env
```

## Layout

```
pyproject.toml           # the single manifest: deps, dev group, entry points, tool config
spg.toml                 # which tools/skills this repo publishes (read by `spg`, a separate tool)
dev-tools.env.example    # template for ~/.dev-tools.env (per-account skill config)
justfile                 # task runner
.pre-commit-config.yaml  # hooks (whitespace/eof/yaml/toml + ty + uv-lock check)
.tool-versions           # asdf: pinned uv + just
.python-version          # uv: 3.13
tools/
  pr_watch/              # one package per tool — see below
skills/
  slack-me/SKILL.md      # agent skills, one directory per skill
agents/
  adr-implementer.md     # subagent definitions
docs/adrs/               # architecture decision records
tests/                   # test suite for all tools
```

Each tool is a package under `tools/` and gets an entry point in
`[project.scripts]`, so `uv run <tool>` (or `just <tool>`) just works.

## Tools

### `pr-watch`

A live, auto-refreshing terminal view of the GitHub PR for a directory's current
branch — modeled after the databricks-tools `follow` command. Every 30s it shows:

- **metrics** at a glance: diff size (+/−), files, commits, opened/updated age,
  review decision (with approval / changes-requested counts), and mergeable state;
- the status of **every check** on the head commit (CI, statuses), sorted
  failures → pending → passing, with a summary count;
- **unresolved review threads** and **who they belong to**, with file location
  and an outdated marker.

```bash
just pr-watch                 # watch the repo in the current directory
just pr-watch ~/code/myrepo   # watch another checkout
uv run pr-watch -i 15 .       # custom interval (seconds)
uv run pr-watch --once .      # one snapshot, no live loop (good for scripting)
```

Uses the `gh` CLI for auth, so `gh auth login` must be done once. The PR is
found by the directory's current git branch; if there's no open PR yet, the view
waits and picks it up automatically.

### `airflow-watch`

A Textual monitor for Airflow deployments on Astronomer Astro, built around one
loop: **see what's failing → drill into the failed task → read its log.**

- recent DAG runs across every DAG, newest and most-broken first, filterable by
  state; `v` switches to a **full DAG list** — paused and stale DAGs included and
  labelled, never hidden;
- `enter` drills a run into its **task instances**, shown in **dependency order**
  (an upstream task above the tasks it feeds, indented, numbered), and a task
  instance into its **log** (`<` / `>` step through attempts);
- `/` **searches** whatever is on screen — the DAG list, the runs list, the task
  list, or the text of a log — client-side and instantly, with the match count in
  the footer;
- every **URL a log prints is clickable**, and a **Databricks run page** is
  hoisted to a line above the log — the operators name it exactly once in
  thousands of lines — so `o` opens the run in a browser without hunting for it;
- a **DAG import errors** pane (`e`) and a header indicator, because an
  unparseable DAG file looks exactly like "nothing scheduled";
- an in-TUI **deployment switcher** (`D`), and an **activity log** (`l`) of every
  poll — its call count and wall clock — and every action;
- safe actions — pause/unpause, trigger, clear (retry), mark success/failed —
  each behind a confirmation modal that names its target and offers a **dry-run
  preview** first.

```bash
just airflow-watch                       # watch the deployment you last had open
uv run airflow-watch -d Production       # pick one by name or id
uv run airflow-watch --state failed      # only failing runs (repeatable)
uv run airflow-watch --once              # one snapshot, no live loop
uv run airflow-watch --once --view dags  # every DAG, paused and stale included
```

**No silent truncation.** Airflow caps a page at 100 records whatever you ask
for, and the `astro` CLI's own `--paginate` does not work against this API — so
every list is paged explicitly off the server's `total_entries`, and the header
says `N of M` whenever it is holding less than the whole thing.

Uses the `astro` CLI for auth and transport, so `astro login` must be done once —
there is no HTTP client and no credential handling in this repo. **Airflow 2 and
Airflow 3**: the version is detected during deployment discovery (or, for a plain
`--api-url` target, by probing `/version` once at startup), and any other major is
refused by name rather than half-supported. The two dialects differ in more than
spelling — `/api/v1` against `/api/v2`, `only_active` against `exclude_stale`,
one set-state endpoint against another — and all of it lives behind one seam
(`tools/airflow_watch/api.py`). See
`docs/adrs/2026-07-24-airflow-access-via-astro-cli.md`,
`docs/adrs/2026-07-24-airflow-2-only-behind-a-version-seam.md` and
`docs/adrs/2026-07-27-airflow-3-joins-the-version-seam.md`.

### `azdo-watch`

`airflow-watch`'s shape pointed at Azure DevOps Pipelines, because the loop is the
same one: **see what's running → drill into the step that broke → read its log.**

- **recent runs across every pipeline**, newest first, filterable by state; `v`
  switches to a **pipeline list** in the shape of the azdo *Recent* tab — each
  pipeline with its last run, its result, and a live dot per run in flight;
- `enter` drills a run into **Azure DevOps' own timeline tree** — a stage above its
  phases, a phase above its jobs, a job above its tasks, indented and numbered —
  and a step into its **log**;
- **the errors azdo already recorded are hoisted out of the logs.** Every timeline
  record carries its own issues *with the log line each was printed on*, so the
  Step pane and the `e` overlay answer "which step failed and what did it say"
  with no log fetch at all. `E` then filters the open log to the `##[error]`
  markers, and `<` / `>` jump between the run's failed steps;
- drilling in lands on the **failed task**, not the failed stage: azdo marks the
  whole chain failed and only the leaf says what actually went wrong, so `enter`
  twice reaches the log that explains it;
- `/` **searches** whatever is on screen — the pipeline list, the runs list, the
  step tree (including the issue messages, so `/tfplan` finds the step that could
  not find the plan file), or the text of a log — client-side and instantly;
- logs arrive **cleaned**: the agent's per-line ISO timestamp and the test runner's
  ANSI colour codes come off at parse time, line numbers preserved, and the azdo
  markers (`##[error]`, `##[section]`, …) are coloured so a log skims the way it
  does in the browser. Any **URL a log prints is clickable**, and `o` opens the
  selected run or pipeline in the web UI;
- an in-TUI **project switcher** (`P`), and an **activity log** (`l`) of every poll
  — its call count and wall clock — and every action;
- actions — queue a run, cancel a run, re-run a failed stage — each behind a
  confirmation modal that names its target. There is **no dry-run offer**, because
  Azure DevOps has no preview for any of them, and the modal says so;
- `i` hands the run to **goblin-watcher**: a worker gathers the timeline, the
  recorded issues and the logs worth reading (failed steps, plus every job — a
  job's log already contains its tasks') into a report, then `gw scratch` opens an
  agent session pointed at it.

```bash
just azdo-watch                              # watch the project you last had open
uv run azdo-watch --project Main             # pick one by name or id
uv run azdo-watch --org my-org               # or by org, name or URL
uv run azdo-watch --state inProgress         # only fetch runs in this azdo status
uv run azdo-watch --once                     # one snapshot, no live loop
uv run azdo-watch --once --view pipelines    # every pipeline and its last run
```

**Anything in flight is always on screen.** The main run window is bounded and
ordered by queue time, so a build that has been running since last week is not in
it — this org had three when the tool was written. So every poll spends one extra
call on `statusFilter=inProgress,notStarted,cancelling` and merges the two lists by
run id. That is the dashboard's central claim, and it is worth a call.

**"More available", not "N of M".** Azure DevOps pages its build list by an opaque
continuation token and reports **no total**, so the honest phrasing is `218 runs ·
more available`; an M would be invented. It also means deeper paging is *serial* —
page two's token is inside page one — which is why the default window is one large
`$top` (1000 rows in 2.9s) rather than several polite pages.

Uses the `az` CLI with the `azure-devops` extension for auth and transport, so
`az extension add --name azure-devops` and `az devops login` must be done once —
there is no HTTP client and no credential handling in this repo, the same call
`/azdo-pr` already makes. The org and project default to whatever
`az devops configure --defaults` is set to. Every call pins `--api-version`, so an
unrelated `az extension update` cannot change what the tool does.

## Skills

Agent skills — the instructions that teach an AI coding agent how to work here —
live in a plain top-level **`skills/`** directory, one directory per skill, each
with a `SKILL.md`. Subagent definitions live alongside them in **`agents/`**:

```
skills/
  slack-me/SKILL.md      # ping yourself on Slack (drives the slack-me CLI)
  adr-rpi/SKILL.md       # Research → Plan → Implement, backed by ADRs
  adr-format/SKILL.md    # ADR format + significance bar; preloaded into subagents
agents/
  adr-implementer.md     # implement phase of adr-rpi
  adr-reviewer.md        # review-and-resolve phase of adr-rpi
```

Those are the source of truth, checked in and easy to find. Claude Code only
discovers *project* skills under `.claude/skills/` and *project* subagents under
`.claude/agents/`, so each one is bridged there with a committed relative
symlink:

```
.claude/skills/slack-me           -> ../../skills/slack-me
.claude/agents/adr-implementer.md -> ../../agents/adr-implementer.md
```

So `.claude/` holds no content — only links. Edit the top-level path; never edit
through the symlink.

To use these from anywhere — not just when your shell is inside this repo —
`spg.toml` declares a `[links.<name>]` entry per skill and agent, and `spg`
materializes them:

```bash
spg install    # creates ~/bin wrappers AND the ~/.claude symlinks
spg sync       # refresh them later; spg uninstall removes them
```

Each link's `source` points at the top-level path (`skills/adr-rpi`,
`agents/adr-reviewer.md`), not at the `.claude/` bridge, so nothing resolves
through two hops. Skills use `target = "~/.claude/skills/"` (trailing slash: link
*into* the directory, table name as the leaf); the agents use an explicit
`target = "~/.claude/agents/<name>.md"`, because Claude Code only reads `.md`
files out of an agents directory and the trailing-slash form would drop the
extension.

Requires a `spg` new enough to support `[links]`. An older `spg` ignores the
tables silently rather than erroring — wrappers appear, links don't.

### Local configuration — `~/.dev-tools.env`

The `pr-notebook` and `azdo-then-notebook` skills need values that are specific
to *your* cloud account: the AWS account behind the ECR registry, the region and
CLI profile, the Databricks login a single-user cluster runs as, and the instance
profile and Secrets Manager ARNs that cluster assumes.

None of that is committed. It lives in **`~/.dev-tools.env`**, outside the repo,
and the skills `source` it at the start of any phase that needs it:

```bash
cp dev-tools.env.example ~/.dev-tools.env
chmod 600 ~/.dev-tools.env
$EDITOR ~/.dev-tools.env
```

`dev-tools.env.example` is the tracked template and documents every key;
`skills/pr-notebook/references/config.md` is the contract the skills follow —
which keys each phase requires, and the rule that a missing key is a hard stop
rather than a silently defaulted one. Set `$DEV_TOOLS_ENV` to keep the file
somewhere else.

Nothing in it is a credential — auth still comes from the `aws` and `databricks`
CLI profiles — but the values do identify an account, so the skills never echo a
resolved ARN, account id, or registry host into a Slack summary, a PR comment, or
a commit message. `.gitignore` also refuses `dev-tools.env` inside the repo, in
case a copy lands there by accident.

### `adr-rpi`

Research → Plan → Implement where **Architecture Decision Records are the durable
memory layer**, rather than the per-task scratch files ordinary RPI overwrites
every run. Research reads the accepted corpus so settled questions stop getting
re-litigated; Plan authors a `Proposed` ADR for anything architecturally
significant; Implement works against the `Accepted` ADR and supersedes it rather
than deviating; a final pass reviews the diff against the ADR and fixes what it
finds.

Decisions live in `docs/adrs/`, one file per decision, immutable once accepted
except for their supersession fields — and only a human ever flips a status. The
corpus carries a generated `INDEX.md` you can rebuild at any time:

```bash
python3 skills/adr-rpi/scripts/adr_index.py docs/adrs             # rebuild INDEX.md
python3 skills/adr-rpi/scripts/adr_chain.py docs/adrs --validate  # check supersession links
```

Those scripts are stdlib-only so they run in any repo the skill is pointed at,
with no venv. Full docs, invariants, and model routing: `skills/adr-rpi/README.md`.

## Adding a tool

1. Create `tools/<name>/` with a `cli.py` exposing `main() -> int`.
2. Add `<name> = "tools.<name>.cli:main"` under `[project.scripts]`.
3. Add a `just <name>` recipe.
4. Put tests in `tests/`.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with `name` + `description` frontmatter.
2. Bridge it for Claude Code:
   `ln -s ../../skills/<name> .claude/skills/<name>` (commit the symlink).
3. Optionally link it into `~/.claude/skills/` to use it outside this repo.

Same shape for a subagent: `agents/<name>.md`, bridged with
`ln -s ../../agents/<name>.md .claude/agents/<name>.md`.
