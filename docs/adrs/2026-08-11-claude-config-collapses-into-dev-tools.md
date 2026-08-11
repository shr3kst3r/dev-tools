---
id: 2026-08-11-claude-config-collapses-into-dev-tools
status: Proposed
supersedes: null
superseded-by: null
components: [statusline, spg-links, skills, repo-boundary]
ticket: null
date: 2026-08-11
---
# Collapse the global Claude Code config into this repo, keeping the status line as bash

## Context

A second repo, `claude-global`, held the other half of one person's Claude Code
setup: two writing skills (`knowledge-base-article`, `topic-intro`), a 520-line
bash status line, an empty `CLAUDE.md` template, and three `install-*.sh` scripts
over a shared `lib/common.sh` that copied all of it into `~/.claude/`.

The split duplicated work with no boundary to justify it. Both repos publish
skills to the same `~/.claude/skills/` directory, and this one already has a
publisher — `spg`, driven by `spg.toml`'s `[links]` tables — that does by symlink
what `claude-global`'s install scripts did by `cp`. Copying is the worse of the
two: an edit to the repo does not reach `~/.claude` until someone re-runs the
script, so the installed copy silently drifts from the source of truth. In
practice the drift was already being routed around by hand — `~/.claude/statusline.sh`
was a manual symlink into the `claude-global` working copy, not a copy made by
`install-statusline.sh`, which meant the live status line depended on that
checkout continuing to exist at that path.

That dependency is what forces the decision now: `claude-global` is to be
archived, and archiving it breaks the status bar.

Absorbing the skills is mechanical. The status line is not, because it is bash in
a repo whose stated shape is "one uv project, one Python tool per directory under
`tools/`". It also runs on **every** render, so `uv run`'s interpreter start —
fine for a dashboard invoked by hand — would be paid once per turn.

## Decision

This repo owns the whole Claude Code setup. `claude-global`'s two skills move to
`skills/<name>/` under the existing convention (top-level file, `.claude/` bridge
symlink, `[links]` entry), and its status line moves to `statusline/statusline.sh`
and stays bash, published by a `[links.statusline]` entry that symlinks it to
`~/.claude/statusline.sh`.

The `install-*.sh` scripts and `lib/common.sh` are not ported: `spg install`
replaces them. The one thing `spg` cannot do — set `statusLine.command` in
`~/.claude/settings.json` — is documented in the README as a per-machine step
rather than automated, because a publisher that edits a user's settings file is
doing something categorically different from making symlinks.

`claude-global`'s `research-plan-implement` skill is dropped rather than merged;
`adr-rpi` here supersedes it. Its empty `CLAUDE.md` template carries no content.

## Consequences

Editing a skill or the status line now takes effect immediately everywhere,
because everything published is a symlink to the file in the tree — no install
step to forget, no installed copy to drift. One repo, one `just check`, and the
status line gets tests it never had (`tests/test_statusline.py` runs the script
against fixture payloads).

The cost is a repo that is no longer purely a uv Python project. `tools/` still
means "Python tool with a `cli.py`", but the tree now also holds a shell script
that `pyproject.toml` knows nothing about and `ty` cannot check. That exception is
narrow and load-bearing — it exists because of the per-render cost — so it is
recorded in `CLAUDE.md` next to the script's two hard rules (never fail, never
block) to stop a future reader from "fixing" it into `tools/statusline/cli.py`.

Two smaller costs: the repo has no shell linter, so the script's only automated
guard is its tests; and `spg install` will refuse to repoint the existing
`~/.claude/statusline.sh` symlink at the archived checkout — that one is a
deliberate `spg` safety check, and the fix is to remove the stale link by hand
once.

## Alternatives considered

- **Rewrite the status line as `tools/statusline/cli.py`.** Consistent with every
  other tool and type-checked, but it pays a Python interpreter start on every
  render, and `settings.json` would have to invoke `uv run` from a hardcoded repo
  path. The consistency is not worth per-turn latency.
- **Keep `claude-global` alive for the status line alone.** Preserves the current
  setup exactly, at the price of a repo existing to hold one file, with its own
  install script, hooks, and no tests.
- **Port `install-*.sh` alongside `spg.toml`.** Two publishers with different
  mechanisms (copy vs symlink) writing the same targets — the drift that motivated
  this consolidation, re-imported.
- **Bring `research-plan-implement` over too.** It is the workflow `adr-rpi` was
  written to replace; keeping both invites an agent to pick the one without a
  durable memory layer.
