# dev-tools

Personal developer tools: live terminal dashboards for the systems I spend the
day waiting on — GitHub PRs, Airflow, Azure DevOps Pipelines — alongside the
agent skills, subagents, and status line I work with. **One uv project, one tool
per directory** under `tools/`, all configured from a single `pyproject.toml`.

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
statusline/statusline.sh # the Claude Code status line
docs/adrs/               # architecture decision records
tests/                   # test suite for all tools
```

Each tool is a package under `tools/` and gets an entry point in
`[project.scripts]`, so `uv run <tool>` (or `just <tool>`) just works.

## Tools

### `pr-watch`

A live, auto-refreshing terminal view of the GitHub PR for a directory's current
branch — modeled after the databricks-tools `follow` command. Every 60s it shows:

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

### `my-prs`

`pr-watch` widened from one PR to all of them: every open PR you touched in the
last two weeks, across every repo, in one Textual master/detail dashboard — the
list on the left, the selected PR's detail on the right.

- **three views**, cycled with `v`: the PRs you authored, the PRs waiting on a
  review from you, and the ones you've hidden;
- sorted **needs-you first**, then most recently updated. A PR needs you if a
  check is failing, a review thread is unresolved, the review is still missing or
  changes-requested, or **the branch no longer merges cleanly** — GitHub computes
  mergeability lazily, so only a definite `CONFLICTING` counts as a conflict,
  never the `UNKNOWN` it returns while it is still working it out;
- `h` **hides** a PR that isn't yours to care about — a bot's dependency bump, a
  spike someone parked, a review request you'll never get to. It drops out of its
  view and turns up in the hidden view, where `h` puts it back. Nothing is
  unsubscribed on GitHub's side; the local list is the only record, and hiding
  takes effect without waiting for a poll;
- `g` hands the PR to **goblin-watcher**: `gw new --pr <url>` checks its head
  branch out in a fresh worktree and spawns an agent on it, and if a task for
  that branch already exists, a modal offers to recreate it;
- `o` opens the PR in a browser, `d` moves or hides the detail pane, `[` / `]`
  move the divider, `l` is the **activity log** of every poll, `?` the keybinding
  overlay. Where you left the windows — and which view was showing — persists to
  a small JSON state file, and a corrupt one degrades to defaults rather than
  taking the dashboard down.

```bash
just my-prs                        # your PRs, last 14 days
uv run my-prs --view review        # open on "needs my review"
uv run my-prs -d 30 --limit 100    # a wider window (GitHub caps the search at 100)
uv run my-prs --once               # one snapshot, no live loop
```

Uses the `gh` CLI for auth, like `pr-watch` — and reuses its `PullRequest` model
and pure parser, so the two tools cannot disagree about what a check state means.

### `my-issues`

`my-prs`' shape pointed at GitHub **issues**: every open issue you're involved in
that moved in the last two weeks, across every repo, in the same Textual
master/detail dashboard.

- **four views**, cycled with `v`: issues assigned to you, issues you filed,
  issues that mention you, and the ones you've hidden. All three searched views
  come back in **one** GraphQL request per poll, so switching is instant;
- sorted by **most recently updated, and nothing else** — there is deliberately
  **no attention dot**. A PR exposes four crisp facts that mean "this needs you"
  (a failing check, an unresolved thread, a missing review, a conflict); an issue
  exposes none, and every substitute (assigned-but-untouched, an unanswered
  comment, a label convention) needs a judgment GitHub does not make. A dot that
  is only sometimes right costs a column and buys nothing, so every column here
  reports a **fact**: the labels the repo defined, in the repo's own colors, who
  is assigned (`—` when nobody is), the comment count, and how long ago. See
  `docs/adrs/2026-08-11-issues-get-no-attention-dot.md`;
- the **people columns follow one rule**: `Author` appears wherever the filer
  might not be you, `Assignees` wherever the assignee might not be you. A column
  that would read as you on every row is left out rather than padded;
- the detail pane shows the issue's header, its labels/assignees/milestone/
  reactions, its body rendered as **markdown**, and the tail of its comment
  thread — with a `+N earlier` note so it never implies it has the whole
  conversation;
- `h` **hides** an issue that isn't yours to care about, `g` hands it to
  **goblin-watcher** (`gw new --issue <url>`), `o` opens it in a browser, `d`
  moves or hides the detail pane, `[` / `]` move the divider, `l` is the activity
  log, `?` the keybinding overlay — the same keys as `my-prs`, so muscle memory
  transfers.

```bash
just my-issues                       # issues assigned to you, last 14 days
uv run my-issues --view created      # open on "I filed"
uv run my-issues --user alice        # someone else's assigned/filed/mentioned
uv run my-issues -d 30 --limit 100   # a wider window (GitHub caps the search at 100)
uv run my-issues --once              # one snapshot, no live loop
```

Uses the `gh` CLI for auth. `my-issues` **owns its own copy** of the dashboard
shell rather than sharing `my-prs`': an issue has no branch, checks, review
decision or mergeability, so there is no subset of `pr-watch`'s `PullRequest` that
describes one, and the two tools' sorts and columns already diverge. Sharing is
limited to the genuinely domain-free helpers (`_run`, `require_gh`,
`format_relative`), the two tools never import each other, and each keeps its
state under its own `$XDG_CONFIG_HOME/<tool>/` — the hide-list keys are the same
`owner/repo#number` shape, so a shared file would silently clobber the other's.
The cost is that a fix to one shell is not a fix to the other; see
`docs/adrs/2026-08-11-my-issues-copies-the-my-prs-shell.md`.

#### Refresh cadence and the GitHub API budget

`pr-watch`, `my-prs` and `my-issues` all poll GitHub's GraphQL API, and they draw
on **one shared budget of 5000 points/hour** — points, not requests, scored on the
nodes a query might return. Their defaults are picked to fit inside it together:

| tool | cost/poll | default interval | points/hour |
| --- | --- | --- | --- |
| `my-prs` | ~54 | 180s | ~1080 |
| `my-issues` | ~5 | 180s | ~100 |
| `pr-watch` | ~5 | 60s | ~300 (per instance) |

`my-prs` dominates because a PR search nests a `comments` connection inside a
`reviewThreads` connection, and nested connections multiply: its cost is roughly
`2 searches × --limit × reviewThreads-first / 100`. That is worth knowing before
raising `--limit` — `--limit 100` doubles the cost of every poll.

`-i` lowers the interval, but each tool clamps it to a floor (30s for the two
dashboards, 15s for `pr-watch`) so a stray `-i 1` can't spend the whole hourly
budget in a few minutes. If a limit is hit anyway, the dashboards back off
exponentially rather than retrying on the normal cadence, and say so in the
activity log (`l`).

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

### `slack-me`

The small one: post a message to your own Slack through an incoming webhook, so a
long job — or an agent — can tell you it's finished.

```bash
just slack-me "deploy finished"
long-job 2>&1 | uv run slack-me    # no args: the message comes from stdin
uv run slack-me -q "done"          # no confirmation panel (errors still print)
```

The webhook lives in `~/.slack-me.toml` (`webhook = "https://hooks.slack.com/…"`,
plus an optional `username` to override the message's display name), or in
`$SLACK_ME_WEBHOOK`, which wins over the file — handy for one-off use without
writing a config. `slack-me --help` also carries a **Slack mrkdwn cheatsheet**
(`*bold*` with single asterisks, `<url|text>` links), because Slack's syntax is
not Markdown and the difference bites every time. The `slack-me` skill below is
just this CLI with instructions attached.

## Skills

Agent skills — the instructions that teach an AI coding agent how to work here —
live in a plain top-level **`skills/`** directory, one directory per skill, each
with a `SKILL.md`. Subagent definitions live alongside them in **`agents/`**:

```
skills/
  slack-me/SKILL.md      # ping yourself on Slack (drives the slack-me CLI)
  adr-rpi/SKILL.md       # Research → Plan → Implement, backed by ADRs
  adr-format/SKILL.md    # ADR format + significance bar; preloaded into subagents
  pr-land/SKILL.md       # drive a PR to green: checks, review feedback, replies
  knowledge-base-article/SKILL.md  # write a structured KBA into docs/
  topic-intro/SKILL.md   # research and write a primer on a new topic
agents/
  adr-implementer.md     # implement phase of adr-rpi
  adr-reviewer.md        # review-and-resolve phase of adr-rpi
```

The last two are writing skills rather than workflow ones.
`knowledge-base-article` classifies a topic as how-to / conceptual /
troubleshooting / reference, picks the matching template from
`skills/knowledge-base-article/references/structure-templates.md`, and saves the
article under `docs/`; `topic-intro` researches a subject and writes a primer with
prerequisites, key concepts, a learning path, and a glossary.

The top-level files are the source of truth, checked in and easy to find. Claude
Code only discovers *project* skills under `.claude/skills/` and *project*
subagents under `.claude/agents/`, so each one is bridged there with a committed
relative symlink:

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

## Status line

`statusline/statusline.sh` is the status line Claude Code renders under the
prompt. It reads the session JSON on stdin and prints up to six lines — the last
two appear only when there is something to report:

```
~/src/project
Opus │ 58,120 tok │ $0.1534 │ 3m5s │ +42 -7
⛁ ⛁ ⛁ ⛁ ⛁ ⛀ ⛶ ⛶ ⛶ ⛶   claude-opus-4-5 · 56.0K/200.0K tokens (28%)
⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶   ⛁ in: 2.0K (cache ↩53.0K ↗1.0K)  ⛀ out: 500  ⛶ free: 144.0K
plugins(4): codex, gopls-lsp, op-dev, pyright-lsp │ mcps(3): context7, linear, playwright
5h: ██░░░░░░░░ 23% ↻26805d2h │ 7d: ██████░░░░ 61% ↻26805d2h
```

Directory with `~` collapsed, plus branch, dirty flags (`!` modified, `?`
untracked, `+` staged) and ahead/behind (`↑N`/`↓M`); model, session tokens, cost
(green under $0.25, orange under $1, red above), duration, lines changed; then a
2×10 context grid where each cell is 5% of the window — `⛁` input including
cache, `⛀` output, `⛶` free — beside the exact counts; enabled plugins and MCP
servers; and the 5-hour and 7-day rate limits with a countdown to each reset.

Rate limits come from `.rate_limits` in the session JSON when Claude Code sends
it. When it doesn't, the script falls back to the OAuth usage endpoint using the
token in the macOS keychain, caching the response for five minutes — and skips
the request outright if that token has already expired, rather than burning
`--max-time` on a guaranteed 401 every render.

It's bash, not a Python tool under `tools/`, because it runs on *every* render:
an interpreter start per turn would be felt. `jq` is the one requirement. The
render must never fail, so an unparseable payload degrades to an empty object and
every lookup falls back to a default instead of tripping `set -e`.

```bash
just statusline                    # preview the render from statusline/sample.json
just test tests/test_statusline.py # run it for real against fixture payloads
```

`spg install` links the script to `~/.claude/statusline.sh`. Pointing Claude Code
at it is a one-time, per-machine step — `spg` makes links, it doesn't edit your
settings:

```bash
jq '. + {statusLine: {type: "command", command: "~/.claude/statusline.sh"}}' \
  ~/.claude/settings.json > /tmp/s.json && mv /tmp/s.json ~/.claude/settings.json
```

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
