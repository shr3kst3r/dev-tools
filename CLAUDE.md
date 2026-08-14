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
statusline/statusline.sh # Claude Code status line (bash, see below)
tmux/tmux.conf           # tmux config, published to ~/.tmux.conf (see below)
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
developer's `~/.claude/skills/` or `~/.agents/skills/` so it works outside this repo — that chain
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
  agents (and the status line) land where Claude Code and Antigravity look for them
  (`~/.claude/` and `~/.agents/`), and the tmux config lands at `~/.tmux.conf`. Commands
  have a `run` (shell to execute from the repo root), a `description`, and
  optional `args` that drive tab completion; links have a `source`
  (repo-relative) and a `target`
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
- `spg` only creates links. Anything that also needs a key in
  `~/.claude/settings.json` — the status line's `statusLine.command` is the one
  case — documents that step in the README instead of editing a user's settings.

Run `spg install` (or `spg sync`) to materialize wrappers and links after
editing `spg.toml`.

## The status line is bash on purpose

`statusline/statusline.sh` is the one non-Python program here. Claude Code shells
out to it on **every render**, so an interpreter start per turn would be felt —
that is why it isn't a tool under `tools/`. Two rules follow from that:

- **It must never fail.** A non-zero exit is a broken status bar, so every lookup
  falls back to a default and an unparseable payload degrades to `{}` rather than
  tripping `set -e`. `jq` is the only dependency.
- **It must never block.** Network calls (the OAuth usage endpoint, when the
  session JSON carries no `.rate_limits`) are `curl --max-time`-bounded, cached
  for five minutes, and skipped when the keychain token is already expired.

It is tested by running it: `tests/test_statusline.py` pipes fixture payloads in
and asserts on the rendered lines. Every fixture supplies `.rate_limits` and an
overridden `$HOME`, so no test touches the network or the real `~/.claude`.
`just statusline` previews the render from `statusline/sample.json`.

## The tmux config is a dotfile, not a tool

`tmux/tmux.conf` is the second non-Python thing here, and the first `[links]`
target outside `~/.claude`: tmux reads `~/.tmux.conf` directly, so the symlink
`spg install` creates *is* the install — there is no settings key to point at
it, and no wrapper on `$PATH`.

- **tpm is not vendored.** Plugins are declared with `@plugin` but the plugin
  manager itself is cloned by hand. The `run` at the bottom is therefore wrapped
  in an `if-shell` existence check — unguarded it exits 127 on a machine without
  tpm, which the user sees on every tmux start and every `prefix + r`.
- **Keep the tpm line last**, as tpm requires, and keep the guard when you touch
  it.

Like the status line, it is tested by running it: `tests/test_tmux_conf.py`
starts a private tmux server on its own socket with `-f /dev/null`, sources the
file into it with `source-file` (which, unlike `tmux -f`, reports a bad option as
a non-zero exit), and asserts on the options and key bindings the server ends up
with. `$HOME` is a tmp directory in every test, so nothing reads or writes the
developer's plugins or resurrect state. `just tmux-check` is the same load
without the assertions. Tests skip when `tmux` isn't installed.

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
3. Publish it: add `[links.<name>]` (`target = "~/.claude/skills/"`) and
   `[links.agents-<name>]` (`target = "~/.agents/skills/<name>"`) entries to
   `spg.toml`, then run `spg install`.
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
