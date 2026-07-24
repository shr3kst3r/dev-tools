# dev-tools (`spg`)

A collection of personal developer tools. **One uv project, one tool per
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
```

## Layout

```
pyproject.toml           # the single manifest: deps, dev group, entry points, tool config
spg.toml                 # which tools get published to ~/bin as commands
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
