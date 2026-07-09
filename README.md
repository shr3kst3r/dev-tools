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
justfile                 # task runner
.pre-commit-config.yaml  # hooks (whitespace/eof/yaml/toml + ty + uv-lock check)
.tool-versions           # asdf: pinned uv + just
.python-version          # uv: 3.13
tools/
  pr_watch/              # tool 1 — see below
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

## Adding a tool

1. Create `tools/<name>/` with a `cli.py` exposing `main() -> int`.
2. Add `<name> = "tools.<name>.cli:main"` under `[project.scripts]`.
3. Add a `just <name>` recipe.
4. Put tests in `tests/`.
