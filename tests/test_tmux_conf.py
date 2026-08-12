"""Tests for the tmux configuration.

`tmux/tmux.conf` is config, not code, so the only honest way to test it is the
same way the status line is tested: run it for real. Each test starts a private
tmux server (its own socket, `-f /dev/null` so nothing else is loaded), sources
the repo's config into it, and asks the server what it ended up believing.

`source-file` is the check that matters — unlike starting a server with `-f`, it
reports a bad option or an unparseable line as a non-zero exit plus a message,
so a typo in the config fails the suite instead of silently degrading someone's
terminal.

Every server here runs with `$HOME` pointed at an empty tmp directory, so no
test can touch the developer's real config, plugins or resurrect state. That
also pins down the tpm line at the bottom of the config: tpm is cloned by hand
rather than shipped with this file, so sourcing has to stay clean when
`~/.tmux/plugins/tpm` is absent, and still run it when it is not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

TMUX_CONF = Path(__file__).resolve().parents[1] / "tmux" / "tmux.conf"

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@dataclass(frozen=True)
class Server:
    """A throwaway tmux server, addressed by its own socket name."""

    socket: str
    home: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        return subprocess.run(
            ["tmux", "-L", self.socket, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def option(self, name: str) -> str:
        proc = self.run("show-options", "-gv", name)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def prefix_binding(self, key: str) -> str | None:
        """What `prefix + key` runs, or None if the key is unbound.

        `list-keys` prints `bind-key    -T prefix <key>    <command>` with the
        columns padded to line up, so the key is field 3 and the command is
        whatever follows it.
        """
        proc = self.run("list-keys", "-T", "prefix")
        assert proc.returncode == 0, proc.stderr
        for line in proc.stdout.splitlines():
            fields = line.split(maxsplit=4)
            if len(fields) == 5 and fields[3] == key:
                return fields[4]
        return None


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    """A tmux server with an empty $HOME and no config loaded."""
    srv = Server(socket=f"dev-tools-{uuid.uuid4().hex[:12]}", home=tmp_path)
    start = srv.run("-f", "/dev/null", "new-session", "-d", "-x", "200", "-y", "50")
    assert start.returncode == 0, start.stderr
    try:
        yield srv
    finally:
        srv.run("kill-server")


@pytest.fixture
def configured(server: Server) -> Server:
    """The same server, with `tmux/tmux.conf` sourced into it."""
    proc = server.run("source-file", str(TMUX_CONF))
    assert proc.returncode == 0, proc.stderr
    return server


def test_sources_without_errors(server: Server) -> None:
    """The whole file loads clean — no bad options, no unparseable lines."""
    proc = server.run("source-file", str(TMUX_CONF))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_sourcing_is_idempotent(server: Server) -> None:
    """`prefix + r` re-sources the live config; twice has to be as good as once."""
    for _ in range(2):
        proc = server.run("source-file", str(TMUX_CONF))
        assert proc.returncode == 0, proc.stderr
    assert server.option("prefix") == "C-a"


def test_prefix_is_ctrl_a(configured: Server) -> None:
    assert configured.option("prefix") == "C-a"


def test_ctrl_a_passes_through_on_prefix_a(configured: Server) -> None:
    """prefix + a sends a literal C-a — the only way to reach a nested tmux."""
    assert configured.prefix_binding("a") == "send-prefix"


def test_prefix_tab_cycles_panes(configured: Server) -> None:
    """prefix + Tab is pane cycling, which is why extrakto is remapped to C-e."""
    assert configured.prefix_binding("Tab") == "select-pane -t :.+"


def test_quality_of_life_options(configured: Server) -> None:
    assert configured.option("mouse") == "on"
    assert configured.option("history-limit") == "50000"
    assert configured.option("base-index") == "1"
    assert configured.option("renumber-windows") == "on"
    assert configured.option("focus-events") == "on"
    assert configured.option("allow-rename") == "off"
    assert configured.option("set-clipboard") == "on"


def test_two_line_status_bar(configured: Server) -> None:
    """Row 2 carries the pane title and the clock, so row 1's right side is empty."""
    assert configured.option("status") == "2"
    assert configured.option("status-right") == ""
    assert "#{pane_title}" in configured.option("status-format[1]")


def test_tpm_is_optional(configured: Server) -> None:
    """`$HOME` here has no tpm, and the config still loaded — that is the point.

    An unguarded `run '~/.tmux/plugins/tpm/tpm'` exits 127 on such a machine,
    which surfaces as an error on every tmux start and every `prefix + r`.
    """
    assert not (configured.home / ".tmux" / "plugins" / "tpm").exists()


def test_tpm_runs_when_installed(server: Server) -> None:
    """The guard must not be so cautious that it never runs tpm either."""
    tpm = server.home / ".tmux" / "plugins" / "tpm" / "tpm"
    tpm.parent.mkdir(parents=True)
    marker = server.home / "tpm-ran"
    tpm.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    tpm.chmod(0o755)

    proc = server.run("source-file", str(TMUX_CONF))
    assert proc.returncode == 0, proc.stderr

    # if-shell/run-shell are dispatched by the server, so the marker lands
    # shortly after source-file returns.
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "tpm was not run"


def test_vi_copy_mode_bindings(configured: Server) -> None:
    proc = configured.run("list-keys", "-T", "copy-mode-vi")
    assert proc.returncode == 0, proc.stderr
    assert "begin-selection" in proc.stdout
    assert "copy-pipe-and-cancel" in proc.stdout
