# spg — a personal developer toolbox
# `just` with no args lists every recipe.

set shell := ["bash", "-cu"]

# Show all recipes.
default:
    @just --list

# Create/update the virtualenv and install deps (incl. dev group).
sync:
    uv sync

# Update the lockfile.
lock:
    uv lock

# Type-check everything with ty.
typecheck:
    uv run ty check

# Run the test suite.
test *args:
    uv run pytest {{args}}

# Type-check + test. Run this before pushing.
check: typecheck test

# Run all pre-commit hooks against every file.
lint:
    uv run pre-commit run --all-files

# Install the pre-commit git hook.
hooks:
    uv run pre-commit install

# --- tools -------------------------------------------------------------

# Monitor an Astro Airflow deployment: failing runs, task instances, logs.
airflow-watch *args:
    uv run airflow-watch {{args}}

# Monitor an Azure DevOps project: pipelines, runs, stages/jobs, logs.
azdo-watch *args:
    uv run azdo-watch {{args}}

# Dashboard of your open GitHub issues from the last 2 weeks, across all repos.
my-issues *args:
    uv run my-issues {{args}}

# Dashboard of your open GitHub PRs from the last 2 weeks, across all repos.
my-prs *args:
    uv run my-prs {{args}}

# Watch the GitHub PR for the repo in DIR (default: current dir).
pr-watch dir="." *args:
    uv run pr-watch {{dir}} {{args}}

# Send yourself a Slack message via the webhook in ~/.slack-me.toml.
slack-me *args:
    uv run slack-me {{args}}

# --- Claude Code config ------------------------------------------------

# Preview the status line from statusline/sample.json (needs jq).
statusline:
    @statusline/statusline.sh < statusline/sample.json

# --- terminal config ---------------------------------------------------

# Load tmux/tmux.conf into a throwaway tmux server to check it for errors.
tmux-check:
    #!/usr/bin/env bash
    set -euo pipefail
    socket="dev-tools-check-$$"
    trap 'tmux -L "$socket" kill-server 2>/dev/null || true' EXIT
    tmux -L "$socket" -f /dev/null new-session -d
    tmux -L "$socket" source-file tmux/tmux.conf
    echo "tmux/tmux.conf: OK"
