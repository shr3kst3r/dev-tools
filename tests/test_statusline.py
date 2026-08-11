"""Tests for the Claude Code status line script.

`statusline/statusline.sh` is bash rather than Python because Claude Code shells
out to it on every render — an interpreter start per turn would be felt. It is
therefore tested the only way a script can be: run it for real, feed it the JSON
Claude Code sends on stdin, and assert on what it prints.

Two properties matter more than the exact layout:

1. It never fails. A missing key, an empty object, garbage on stdin — the line
   still renders, because a non-zero exit shows up as a broken status bar.
2. It never calls the network during a test. The rate-limit block only reaches
   for the OAuth endpoint when `.rate_limits` is absent from the input, so every
   fixture here supplies it (`test_no_network_without_rate_limits` covers the
   fallback path with `HOME` pointed at an empty directory).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATUSLINE = Path(__file__).resolve().parents[1] / "statusline"
SCRIPT = STATUSLINE / "statusline.sh"
SAMPLE = STATUSLINE / "sample.json"

# ANSI SGR escapes — stripped before asserting on text.
ANSI = re.compile(r"\x1b\[[0-9;]*m")

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="statusline.sh requires jq"
)


def run(payload: Any, *, home: Path | None = None) -> tuple[int, str, str]:
    """Run the script with `payload` on stdin; return (rc, plain stdout, stderr).

    `home` overrides $HOME so the script's reads of ~/.claude (settings, plugin
    cache, usage cache) hit a controlled directory instead of the developer's.
    """
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc.returncode, ANSI.sub("", proc.stdout), proc.stderr


def payload(**overrides: Any) -> dict[str, Any]:
    """A full statusline JSON payload, shaped like Claude Code's."""
    base: dict[str, Any] = {
        "cwd": "/tmp",
        "model": {"id": "claude-opus-4-5", "display_name": "Opus"},
        "context_window": {
            "total_input_tokens": 58000,
            "total_output_tokens": 120,
            "used_percentage": 28.4,
            "context_window_size": 200000,
            "current_usage": {
                "input_tokens": 2000,
                "output_tokens": 500,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 53000,
            },
        },
        "cost": {
            "total_cost_usd": 0.1534,
            "total_duration_ms": 185000,
            "total_lines_added": 42,
            "total_lines_removed": 7,
        },
        # Present in every fixture so the OAuth fallback never runs. See module docstring.
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 4102444800},
            "seven_day": {"used_percentage": 61.0, "resets_at": 4102444800},
        },
    }
    base.update(overrides)
    return base


def test_renders_the_expected_lines(tmp_path: Path) -> None:
    rc, out, err = run(payload(), home=tmp_path)
    assert rc == 0, err
    lines = out.splitlines()
    # dir+git, model summary, context grid row 1, context grid row 2, rate limits
    assert len(lines) >= 5
    assert "Opus" in lines[1]
    assert "58,120 tok" in lines[1]
    assert "$0.1534" in lines[1]
    assert "3m5s" in lines[1]
    assert "+42" in lines[1] and "-7" in lines[1]


def test_context_grid_is_two_rows_of_ten_cells(tmp_path: Path) -> None:
    rc, out, _ = run(payload(), home=tmp_path)
    assert rc == 0
    row1, row2 = out.splitlines()[2], out.splitlines()[3]
    # Each row is `<grid>   <text>`; row 2's text repeats the glyphs as a legend,
    # so count cells in the grid half only.
    grids = [row.split("   ", 1)[0] for row in (row1, row2)]
    for grid in grids:
        cells = sum(grid.count(glyph) for glyph in ("⛁", "⛀", "⛶"))
        assert cells == 10, grid
    # 56,000 input+cache of 200,000 -> 5 cells; 500 output rounds down to 0 but
    # is floored at one cell so it stays visible -> 6 used, 14 free.
    joined = "".join(grids)
    assert joined.count("⛁") == 5
    assert joined.count("⛀") == 1
    assert joined.count("⛶") == 14
    assert "56.0K/200.0K tokens (28%)" in row1


def test_rate_limit_bars_render_from_the_payload(tmp_path: Path) -> None:
    rc, out, _ = run(payload(), home=tmp_path)
    assert rc == 0
    usage = out.splitlines()[-1]
    assert "5h:" in usage and "23%" in usage
    assert "7d:" in usage and "61%" in usage
    # resets_at is epoch seconds, and far future -> a day countdown, not "??:??".
    assert "??:??" not in usage
    assert usage.count("↻") == 2


def test_percentages_are_truncated_not_compared_as_floats(tmp_path: Path) -> None:
    """`[[ -gt ]]` is integer-only, so a float percentage must be truncated."""
    rc, out, err = run(
        payload(
            rate_limits={
                "five_hour": {"used_percentage": 99.9, "resets_at": 4102444800},
                "seven_day": {"used_percentage": 100.0, "resets_at": 4102444800},
            }
        ),
        home=tmp_path,
    )
    assert rc == 0, err
    assert "integer expression expected" not in err
    usage = out.splitlines()[-1]
    assert "99%" in usage and "100%" in usage


def test_zero_rate_limits_hide_the_usage_line(tmp_path: Path) -> None:
    """A 0% bar reads as broken, so the line is omitted rather than shown empty."""
    rc, out, _ = run(
        payload(
            rate_limits={
                "five_hour": {"used_percentage": 0, "resets_at": 4102444800},
                "seven_day": {"used_percentage": 0, "resets_at": 4102444800},
            }
        ),
        home=tmp_path,
    )
    assert rc == 0
    assert "5h:" not in out


def test_home_directory_is_abbreviated(tmp_path: Path) -> None:
    rc, out, _ = run(payload(cwd=f"{tmp_path}/src/project"), home=tmp_path)
    assert rc == 0
    assert out.splitlines()[0].startswith("~/src/project")


def test_git_branch_and_dirty_flags(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q", "-b", "trunk"], check=True)
    subprocess.run([*git, "config", "user.email", "you@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "You"], check=True)
    (repo / "tracked.txt").write_text("one\n")
    subprocess.run([*git, "add", "tracked.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    (repo / "tracked.txt").write_text("two\n")  # modified  -> !
    (repo / "untracked.txt").write_text("new\n")  # untracked -> ?

    rc, out, _ = run(payload(cwd=str(repo)), home=tmp_path)
    assert rc == 0
    line1 = out.splitlines()[0]
    assert "trunk" in line1
    assert "!" in line1 and "?" in line1


def test_non_git_directory_prints_no_branch(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    rc, out, _ = run(payload(cwd=str(plain)), home=tmp_path)
    assert rc == 0
    assert out.splitlines()[0].strip() == str(plain).replace(str(tmp_path), "~")


def test_model_falls_back_to_the_id(tmp_path: Path) -> None:
    rc, out, _ = run(payload(model={"id": "claude-sonnet-4-5"}), home=tmp_path)
    assert rc == 0
    assert "Sonnet" in out.splitlines()[1]


def test_cost_thresholds_do_not_error_on_large_values(tmp_path: Path) -> None:
    rc, out, err = run(
        payload(cost={"total_cost_usd": 12.5, "total_duration_ms": 7_400_000}),
        home=tmp_path,
    )
    assert rc == 0, err
    line2 = out.splitlines()[1]
    assert "$12.5000" in line2
    assert "2h3m" in line2


@pytest.mark.parametrize("stdin", ["{}", "", "not json at all"])
def test_degrades_instead_of_failing(stdin: str, tmp_path: Path) -> None:
    """No input is still a render: the bar must not go blank or exit non-zero."""
    rc, out, err = run(stdin, home=tmp_path)
    assert rc == 0, err
    assert out.strip(), "produced no output"


def test_sample_payload_renders(tmp_path: Path) -> None:
    """`just statusline` previews the render from this file — keep it valid."""
    rc, out, err = run(SAMPLE.read_text(), home=tmp_path)
    assert rc == 0, err
    assert len(out.splitlines()) >= 5


def test_no_network_without_rate_limits(tmp_path: Path) -> None:
    """The OAuth usage fallback is best-effort: no creds, no cache, still fine.

    $HOME is an empty directory here, so the keychain lookup finds nothing and
    the request is skipped — the point is that the missing-cache path exits 0
    and renders, not that it fetches anything.
    """
    body = payload()
    del body["rate_limits"]
    rc, out, err = run(body, home=tmp_path)
    assert rc == 0, err
    assert "Opus" in out
    assert not (tmp_path / ".claude" / ".statusline-usage-cache.json").exists()
