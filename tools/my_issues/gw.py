"""Launching a goblin-watcher (gw) task from an issue.

`gw new --issue <url>` creates a fresh worktree and spawns an agent on it for the
issue, resolving the project by matching the issue's repo against gw's registered
projects. The dashboard shells out to the gw binary rather than importing
goblin-watcher: gw owns its own config, state, and tmux windowing, and its CLI is
the stable seam between the two tools.

The subprocess inherits stdin/stdout so gw's interactive side works — when run
outside tmux it execs `tmux attach`, which needs the real terminal, so the app
suspends itself around the call. stderr is captured to classify a failure: the
one the dashboard can resolve itself is the "task already exists" collision,
which it offers to retry with --rm.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExistingTask:
    """The task-id collision gw reported: which task, in which project."""

    task_id: str
    project: str


@dataclass(frozen=True, slots=True)
class GwLaunch:
    """The outcome of one `gw new` run."""

    ok: bool
    exists: ExistingTask | None = None  # the failure is a task-id collision
    error: str | None = None  # one concise line for the log when not ok


def new_issue_command(url: str, *, rm: bool = False) -> list[str]:
    """The `gw new` invocation for an issue. `--rm` removes an existing task with
    the same id first (gw keeps the branch — only the stale worktree and task
    record go — and refuses if that worktree has uncommitted changes)."""
    cmd = ["gw", "new", "--issue", url]
    if rm:
        cmd.append("--rm")
    return cmd


# gw's collision error, verbatim (from `gw new`):
#   Error: Task 'the-id' already exists in project 'the-project'.
_EXISTS = re.compile(r"Task '(?P<task>[^']+)' already exists in project '(?P<project>[^']+)'")


def parse_exists(stderr: str) -> ExistingTask | None:
    """The existing task named in a collision error, or None for any other
    failure (including --rm's own refusal on a dirty worktree).

    gw prints errors through a rich Console, which word-wraps at 80 columns
    when stderr is a pipe — a long task id pushes "already exists" onto the
    next line — so whitespace is flattened before matching."""
    match = _EXISTS.search(" ".join(stderr.split()))
    if match is None:
        return None
    return ExistingTask(task_id=match["task"], project=match["project"])


def error_line(stderr: str) -> str:
    """gw's error message as one concise line: the (possibly wrapped) lines up
    to its 'Hint:' — which names flags the dashboard can't pass — without the
    'Error:' prefix."""
    lines: list[str] = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line or line.startswith("Hint:"):
            if lines:
                break
            continue
        lines.append(line)
    if not lines:
        return "gw failed with no error output."
    return " ".join(lines).removeprefix("Error:").strip()


def classify(returncode: int, stderr: str) -> GwLaunch:
    if returncode == 0:
        return GwLaunch(ok=True)
    return GwLaunch(ok=False, exists=parse_exists(stderr), error=error_line(stderr))


def run_new(url: str, *, rm: bool = False) -> GwLaunch:
    """Run `gw new --issue` with the terminal's stdin/stdout (call this with the
    Textual app suspended). Blocks until gw exits — which, outside tmux, is
    when the user detaches from the agent's tmux session."""
    if shutil.which("gw") is None:
        return GwLaunch(ok=False, error="gw not found on PATH — is goblin-watcher installed?")
    proc = subprocess.run(new_issue_command(url, rm=rm), stderr=subprocess.PIPE, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)  # keep gw's errors on the terminal too
    return classify(proc.returncode, proc.stderr or "")
