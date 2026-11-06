---
id: 2026-11-06-status-line-moves-to-dotfiles
status: Proposed
supersedes: null
superseded-by: null
components: [statusline, spg-links, repo-boundary]
ticket: null
date: 2026-11-06
---
# Move the Claude Code status line to the dotfiles repo

## Context

`2026-08-11-claude-config-collapses-into-dev-tools` made this repo the owner of
the whole Claude Code setup, the status line included, published by a
`[links.statusline]` entry to `~/.claude/statusline.sh`.

Since then the dotfiles repo has taken over setting up a machine: `spg install`
there installs Homebrew, the asdf runtimes, and Claude Code itself, then links
the shell, tmux, git, and terminal configs. The status line is configuration
for a tool dotfiles installs, and it only means something once Claude Code is
present. dotfiles can say that directly (`requires = ["claude-code"]` on the
link). dev-tools has no install for Claude Code to hang that on.

Two spg projects can't both link the same target. Whichever runs last wins,
and the other then reports a conflict. So one repo has to own it.

This record is written after the fact: dotfiles PR #4 already added the script
and its link, and this machine already has dev-tools' link disabled with
`spg disable link:statusline`. That's stated here so a reviewer can decide
whether the record is worth keeping, not hidden.

## Decision

The dotfiles repo owns the status line (`claude/statusline.sh`, linked by
`spg.d/claude.toml` once Claude Code is installed). dev-tools drops the script,
its sample payload, its tests, the `just statusline` recipe, and the
`[links.statusline]` entry.

Skills and subagents stay here. Only the status line moves, so the rest of
`2026-08-11-claude-config-collapses-into-dev-tools` still describes this repo.
That's why this record doesn't set `supersedes`. Marking the old ADR
`Superseded` would retire its skills decision too, which still stands. Read
this ADR as overriding the status-line part of it, and nothing else.

## Consequences

One repo links `~/.claude/statusline.sh`, so spg no longer has two owners for
it, and a fresh machine gets the status line from the same `spg install` that
installs Claude Code.

dev-tools goes back to being purely a uv Python project plus markdown skills
and agents. The bash exception recorded in `CLAUDE.md` goes away.

The cost: the script loses its tests. `tests/test_statusline.py` ran it against
fixture payloads, and dotfiles has no test runner. Until dotfiles grows one,
the only check is piping a payload in by hand. The never-fail and never-block
rules that `CLAUDE.md` recorded here need to travel with the script's header
comment instead.

## Alternatives considered

- **Keep it here and have dotfiles not link it.** Then a fresh machine needs
  both repos cloned and installed for the status bar to work, while everything
  else about Claude Code comes from dotfiles.
- **Link it from dotfiles to this checkout's file.** It hardcodes a path into
  another repo's working tree, which is the same fragility that the 2026-08-11
  ADR moved away from.
- **Move the tests to dotfiles too.** Worth doing eventually, but it means
  adding Python and pytest tooling to a repo that has none, just for one script.
