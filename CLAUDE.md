# CLAUDE.md

Guidance for AI agents working in this repo.

## What this is

`dev-tools` — a personal developer toolbox. **One uv project, one tool per directory**
under `tools/`, all configured from a single `pyproject.toml`. This is *not* a
uv workspace; there are no per-tool manifests.

## Toolchain

- **uv** owns the Python interpreter (`.python-version`, currently 3.13) and the
  venv, including the dev tools.
- **asdf** (`.tool-versions`) pins only the bootstrap CLIs: `uv` and `just`.
  Python is intentionally *not* pinned in asdf — uv is the single source of
  truth for the interpreter.
- **ty** for type checking, **pytest** for tests, **pre-commit** for git hooks,
  **just** as the task runner. Dev deps live in the PEP 735 `[dependency-groups]`
  `dev` group.

## Common commands

```bash
just            # list every recipe
just sync       # create .venv + install everything (incl. dev group)
just check      # ty + pytest — run this before pushing
just typecheck  # ty only
just test       # pytest only
just lint       # pre-commit against all files
just <tool>     # run a tool (e.g. `just pr-watch`)
```

## Layout

```
pyproject.toml           # packaging: deps, dev group, [project.scripts], tool config
spg.toml                 # spg command-publisher config (see below)
dev-tools.env.example    # template for ~/.dev-tools.env (see below)
justfile                 # task runner
.pre-commit-config.yaml  # hooks
.tool-versions           # asdf: pinned uv + just
.python-version          # uv: Python 3.13
tools/<name>/            # one package per tool, each with cli.py:main() -> int
skills/<name>/SKILL.md   # agent skills — the real files (see below)
agents/<name>.md         # subagent definitions — the real files
.claude/                 # symlinks only, nothing else lives here
docs/adrs/               # architecture decision records (see skills/adr-rpi)
tests/                   # test suite for all tools
```

## Skills and agents live at the top level, not in `.claude/`

Agent skills and subagent definitions are checked in at the top level:
`skills/<name>/SKILL.md` and `agents/<name>.md`. **Those are the source of
truth — always read and edit the top-level path, never the `.claude/` one.**

Claude Code only discovers project skills under `.claude/skills/` and project
subagents under `.claude/agents/`, so each one is bridged with a committed
*relative* symlink:

```
.claude/skills/slack-me           -> ../../skills/slack-me
.claude/agents/adr-implementer.md -> ../../agents/adr-implementer.md
```

`.claude/` therefore contains no content of its own, only links. The indirection
is deliberate: skills and agents are a first-class, visible part of this repo,
not tooling config buried in a dotdir. A skill may also be linked into a
developer's `~/.claude/skills/` so it works outside this repo — that chain
resolves through the committed symlink, so don't delete it.

Symlinked *skill* directories are documented behavior. Symlinked *agent* files
are not documented either way, but were verified working on Claude Code v2.1.219
(a fresh session resolves `adr-implementer` and `adr-reviewer` through the
links). If agent discovery ever breaks, that's the thing to suspect — move the
`.md` file into `.claude/agents/` and note the exception here.

## Two config files, two jobs

- **`pyproject.toml`** — packaging. Deps, build backend, `[project.scripts]`
  entry points, `[tool.*]` config.
- **`spg.toml`** — config for [`spg`](../spg), a per-project publisher. On
  `spg install` it writes a `~/bin/<name>` wrapper (plus zsh completion) for each
  `[commands.<name>]` entry, so this repo's tools land on your `$PATH`, and it
  creates a symlink for each `[links.<name>]` entry, so this repo's skills and
  agents land where Claude Code looks for them. Commands have a `run` (shell to
  execute from the repo root), a `description`, and optional `args` that drive
  tab completion; links have a `source` (repo-relative) and a `target`
  (absolute). This is *not* a packaging file — it only answers "what should this
  repo publish onto my machine, and how does it complete?".

**Invariants:**

- A tool meant to be published has (1) a `[commands.<name>]` entry in
  `spg.toml`, (2) a `[project.scripts]` entry in `pyproject.toml`, and (3) a
  `just <recipe>` recipe in the `justfile`.
- A skill or agent meant to work outside this repo has a `[links.<name>]` entry
  in `spg.toml` whose `source` points at the **top-level** path
  (`skills/<name>`, `agents/<name>.md`) — never at the `.claude/` bridge
  symlink, so nothing resolves through two hops.

Run `spg install` (or `spg sync`) to materialize wrappers and links after
editing `spg.toml`.

## No account or employer identifiers in this repo

**This repo is generic on purpose. Nothing in it names an employer, a colleague, a
cloud account, or an internal system.** That is a hard rule, not a preference —
it is what lets the skills be shared, published, or read by anyone without
leaking where they were written.

Never commit, in code, docs, skills, or test fixtures:

- a company or organization name, or a GitHub / Azure DevOps org that identifies
  one;
- an AWS account id, ARN, ECR registry host, or Databricks workspace host or
  org id;
- a real work email, colleague name, or GitHub login other than the repo owner's;
- an internal repository, service, pipeline, cluster, dataset, or vendor-contract
  name.

Use placeholders instead — `example-org`, `you@example.com`, `alice`,
`000000000000`, `etl-service` — and keep test fixtures anonymised even when they
started as captures of real API responses. Say so in the fixture's docstring, as
`tests/test_azdo_watch.py` does.

When a skill genuinely **needs** a real value at runtime, it reads it from
**`~/.dev-tools.env`** rather than carrying it:

- `dev-tools.env.example` is the tracked template and documents every key.
- `skills/pr-notebook/references/config.md` is the contract: how to source the
  file, which keys each phase asserts, and the rule that a missing key is a hard
  stop — never a value guessed from the ambient environment.
- The real file is gitignored under both `dev-tools.env` and `.dev-tools.env`.
- Skills must not echo a resolved ARN, account id, or registry host into a Slack
  summary, a PR comment, or a commit message. Report the repo, tag, and short
  digest instead.

If a worked example in a skill needs to cite a real past incident to make its
point, keep the *lesson* and anonymise the *coordinates* — "a build reported
`FAILURE` while the only failing entries were superseded attempts" carries the
same weight without naming the pipeline.

## Adding a tool

1. Create `tools/<name>/` with a `cli.py` exposing `main() -> int`.
2. Add `<name> = "tools.<name>.cli:main"` under `[project.scripts]`.
3. Add a `[commands.<name>]` entry in `spg.toml` (`run = "uv run <name>"`).
4. Add a `just <name>` recipe.
5. Put tests in `tests/`.
6. Run `spg install` to publish the new command to `~/bin`.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with at least `name` + `description`
   frontmatter (`description` is what makes an agent pick it up — say *when* to
   trigger it, not just what it does).
2. Bridge it: `ln -s ../../skills/<name> .claude/skills/<name>`, then
   `git add .claude/skills/<name>` so the symlink itself is committed (mode
   `120000`).
3. Publish it: add a `[links.<name>]` entry to `spg.toml` with
   `source = "skills/<name>"` and `target = "~/.claude/skills/"` (trailing slash
   means "link into that directory, using the table name as the leaf"), then run
   `spg install`.
4. Keep any supporting files (references, scripts) inside `skills/<name>/`.

A skill's bundled scripts may need to run in *other* repos, not just this one.
When that's true, write them **stdlib-only** so `python3 script.py` works with
nothing installed — `skills/adr-rpi/scripts/` is the worked example, and its
tests live in `tests/test_adr_scripts.py` even though the code isn't under
`tools/`.

## Adding a subagent

1. Create `agents/<name>.md` with `name` + `description` frontmatter; add `model`,
   `effort`, `tools`, and `skills` as needed.
2. Bridge it: `ln -s ../../agents/<name>.md .claude/agents/<name>.md` and
   `git add` the symlink.
3. Publish it with a `[links.<name>]` entry in `spg.toml` — and here use the
   explicit target form, `target = "~/.claude/agents/<name>.md"`. The trailing
   slash form would drop the `.md`, and Claude Code only reads `.md` files out of
   an agents directory.
4. Prefer `background: false` for agents whose result you intend to verify in the
   same turn — background subagents lose most built-in tools, `AskUserQuestion`
   among them, so they can't ask you anything.
5. Preload shared discipline with `skills: [<skill>]` rather than restating it in
   every delegation prompt. A skill with `disable-model-invocation: true` cannot
   be preloaded.

## Conventions

- Tools should have a **rich terminal UI** (the `rich` library) where it makes
  sense — favor a polished, readable TUI over plain prints.
- Keep a pure, side-effect-free parse/logic layer separate from I/O so it can be
  unit-tested (see `tools/pr_watch/github.py`).
- Run `just check` before pushing. Open PRs with `gw pr open`.
