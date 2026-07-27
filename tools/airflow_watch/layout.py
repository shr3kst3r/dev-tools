"""Persisted window layout for airflow-watch.

The dashboard remembers how you left it — where the detail pane lives (`d`),
where the divider sits (`[` / `]`), whether the activity chart is shown (`g`),
and which deployment was selected — in a small JSON state file. Parsing/serializing is pure so it can be unit-tested;
only `load`/`save` touch the filesystem, and both shrug off a missing,
malformed, or unwritable file (a broken state file must never take the
dashboard down — it just means default layout).

Deliberately a mirror of `my_prs/layout.py` rather than an import of it:
`state_path()` there is hardcoded to `my-prs`, and generalising it would mean
changing a working, unrelated tool for a cosmetic win. The duplication is a
noted follow-up, not an oversight.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Where the detail pane lives, in the order `d` cycles through.
DETAIL_MODES = ("right", "below", "hidden")

# The list window's share of the split, as a percentage. `[` / `]` step it
# between the bounds; the app's CSS min-width/min-height keep either window
# usable even when the percentage would squeeze it further.
SPLIT_DEFAULT = 50
SPLIT_MIN = 20
SPLIT_MAX = 80
SPLIT_STEP = 5


@dataclass(frozen=True)
class Layout:
    detail_mode: str = DETAIL_MODES[0]
    split: int = SPLIT_DEFAULT
    # The deployment key (its Astro id, or its URL for a plain Airflow) that
    # was selected last. Empty means "no preference — take the first one".
    deployment: str = ""
    # Whether the activity chart under the detail pane is shown (`g`).
    chart: bool = True


def clamp_split(value: int) -> int:
    return max(SPLIT_MIN, min(SPLIT_MAX, value))


def from_dict(data: object) -> Layout:
    """Build a Layout from persisted JSON, defaulting anything unrecognized."""
    if not isinstance(data, dict):
        return Layout()
    mode = data.get("detail_mode")
    if not isinstance(mode, str) or mode not in DETAIL_MODES:
        mode = DETAIL_MODES[0]
    split = data.get("split")
    if not isinstance(split, int) or isinstance(split, bool):
        split = SPLIT_DEFAULT
    deployment = data.get("deployment")
    if not isinstance(deployment, str):
        deployment = ""
    chart = data.get("chart")
    if not isinstance(chart, bool):
        chart = True
    return Layout(
        detail_mode=mode,
        split=clamp_split(split),
        deployment=deployment,
        chart=chart,
    )


def to_dict(layout: Layout) -> dict[str, object]:
    return {
        "detail_mode": layout.detail_mode,
        "split": layout.split,
        "deployment": layout.deployment,
        "chart": layout.chart,
    }


def state_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "airflow-watch" / "layout.json"


def load(path: Path) -> Layout:
    try:
        return from_dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return Layout()


def save(layout: Layout, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_dict(layout), indent=2) + "\n")
    except OSError:
        pass
