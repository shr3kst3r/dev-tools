"""The persisted hide list for my-issues.

Some issues are simply not yours to care about — a tracking issue you're only
CC'd on, a wishlist item nobody is going to do, a bot's automated report. `h`
puts one on this list: it drops out of the view it was in and turns up in the
"hidden" view instead, where `h` puts it back. Nothing is ever closed or
unsubscribed on GitHub's side — this is a local mute, and the list is the only
record of it.

The list maps an issue's key (`owner/repo#number`) to the moment you hid it,
which is what lets the hidden view show the most recently dismissed issue first.
It lives in a small JSON file next to layout.json, under
`$XDG_CONFIG_HOME/my-issues/` — **its own directory, never my-prs'.** The key
shape is identical for a PR, so sharing the file would silently clobber the PR
dashboard's hide list with plausible-looking entries (ADR-constrained: see
`docs/adrs/2026-08-11-my-issues-copies-the-my-prs-shell.md`).

Parsing/serializing is pure so it can be unit-tested; only `load`/`save` touch
the filesystem, and both shrug off a missing, malformed, or unwritable file — a
broken hide list must never take the dashboard down, it just means nothing is
hidden.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# key -> when it was hidden (UTC).
HiddenList = dict[str, datetime]

# The stand-in date for an entry with no usable timestamp.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _parse_time(value: object) -> datetime | None:
    """An ISO timestamp from the file, normalized to UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def from_dict(data: object) -> HiddenList:
    """Build a hide list from persisted JSON, dropping anything unrecognized.

    The `hidden` member is normally an object of key -> ISO timestamp; a bare
    list of keys is also accepted (hand-edited files are the point of a plain
    JSON state file), and those entries are dated to the epoch so they sort
    last — oldest — in the hidden view.
    """
    if not isinstance(data, dict):
        return {}
    entries = data.get("hidden")
    if isinstance(entries, list):
        return {
            key: _EPOCH for key in entries if isinstance(key, str) and key
        }
    if not isinstance(entries, dict):
        return {}
    out: HiddenList = {}
    for key, value in entries.items():
        if isinstance(key, str) and key:
            out[key] = _parse_time(value) or _EPOCH
    return out


def to_dict(hidden: HiddenList) -> dict[str, object]:
    return {
        "hidden": {key: when.isoformat() for key, when in sorted(hidden.items())}
    }


def state_path() -> Path:
    """`$XDG_CONFIG_HOME/my-issues/hidden.json` — my-issues' own directory."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "my-issues" / "hidden.json"


def load(path: Path) -> HiddenList:
    try:
        return from_dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}


def save(hidden: HiddenList, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_dict(hidden), indent=2) + "\n")
    except OSError:
        pass
