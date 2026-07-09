"""Config loading for slack-me.

The config lives at ``~/.slack-me.toml`` and, at minimum, names the Slack
incoming-webhook URL to post to::

    webhook = "https://hooks.slack.com/services/XXX/YYY/ZZZ"

    # Optional: prefix every message with this (e.g. the host name).
    username = "slack-me"

The parse layer (`parse_config`) is pure and string-in/dataclass-out so it can
be unit-tested; `load_config` is the thin filesystem wrapper around it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Where the config lives. Overridable via $SLACK_ME_CONFIG (mostly for tests).
DEFAULT_CONFIG_PATH = Path.home() / ".slack-me.toml"

# A webhook may also come straight from the environment, which wins over the
# file — handy for one-off use or CI without writing a config.
WEBHOOK_ENV_VAR = "SLACK_ME_WEBHOOK"


class ConfigError(Exception):
    """Raised when the config is missing, malformed, or lacks a webhook."""


@dataclass(frozen=True)
class Config:
    """Resolved slack-me settings."""

    webhook: str
    username: str | None = None


def _looks_like_webhook(url: str) -> bool:
    """A cheap sanity check so we fail with a clear message, not a 404 later."""
    return url.startswith("https://hooks.slack.com/")


def parse_config(text: str, *, env_webhook: str | None = None) -> Config:
    """Parse config TOML text into a `Config`.

    Pure: no filesystem, no network. `env_webhook` (if set) overrides the
    webhook from the file, and can stand in for a missing file entirely.
    """
    data: dict = {}
    if text.strip():
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config is not valid TOML: {exc}") from exc

    webhook = env_webhook or data.get("webhook")
    if not webhook:
        raise ConfigError(
            "no Slack webhook configured — set `webhook = \"...\"` in "
            f"{DEFAULT_CONFIG_PATH} or export ${WEBHOOK_ENV_VAR}."
        )
    if not isinstance(webhook, str) or not _looks_like_webhook(webhook):
        raise ConfigError(
            f"webhook does not look like a Slack incoming webhook URL: {webhook!r}"
        )

    username = data.get("username")
    if username is not None and not isinstance(username, str):
        raise ConfigError("`username` must be a string.")

    return Config(webhook=webhook, username=username)


def load_config(path: Path | None = None) -> Config:
    """Read and parse the config file, honoring the env-var overrides.

    A missing file is fine *iff* the webhook is supplied via the environment.
    """
    env_webhook = os.environ.get(WEBHOOK_ENV_VAR)
    config_path = path or Path(
        os.environ.get("SLACK_ME_CONFIG", DEFAULT_CONFIG_PATH)
    )

    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if env_webhook:
            return parse_config("", env_webhook=env_webhook)
        raise ConfigError(
            f"config file not found: {config_path}\n"
            f"Create it with:\n\n    webhook = \"https://hooks.slack.com/services/...\"\n"
        ) from None
    except OSError as exc:
        raise ConfigError(f"could not read config {config_path}: {exc}") from exc

    return parse_config(text, env_webhook=env_webhook)
