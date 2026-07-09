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

# Watch the GitHub PR for the repo in DIR (default: current dir).
pr-watch dir="." *args:
    uv run pr-watch {{dir}} {{args}}

# Send yourself a Slack message via the webhook in ~/.slack-me.toml.
slack-me *args:
    uv run slack-me {{args}}
