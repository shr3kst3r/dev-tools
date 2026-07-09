# CLAUDE.md

Guidance for AI agents working in this repo.

## What this is

`spg` — a personal developer toolbox. **One uv project, one tool per directory**
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
spg.toml                 # tool registry / catalog (see below)
justfile                 # task runner
.pre-commit-config.yaml  # hooks
.tool-versions           # asdf: pinned uv + just
.python-version          # uv: Python 3.13
tools/<name>/            # one package per tool, each with cli.py:main() -> int
tests/                   # test suite for all tools
```

## Two config files, two jobs

- **`pyproject.toml`** — packaging. Deps, build backend, `[project.scripts]`
  entry points, `[tool.*]` config.
- **`spg.toml`** — the tool *catalog*. One `[[tool]]` entry per tool with its
  package, entry point, `just` recipe, and a human description. It's the single
  place that answers "what tools live here and how do I run each one?".

**Invariant:** these three must stay in sync — every tool has (1) a `[[tool]]`
entry in `spg.toml`, (2) a `[project.scripts]` entry in `pyproject.toml`, and
(3) a `just <recipe>` recipe in the `justfile`.

## Adding a tool

1. Create `tools/<name>/` with a `cli.py` exposing `main() -> int`.
2. Add `<name> = "tools.<name>.cli:main"` under `[project.scripts]`.
3. Add a `[[tool]]` entry in `spg.toml`.
4. Add a `just <name>` recipe.
5. Put tests in `tests/`.

## Conventions

- Tools should have a **rich terminal UI** (the `rich` library) where it makes
  sense — favor a polished, readable TUI over plain prints.
- Keep a pure, side-effect-free parse/logic layer separate from I/O so it can be
  unit-tested (see `tools/pr_watch/github.py`).
- Run `just check` before pushing. Open PRs with `gw pr open`.
