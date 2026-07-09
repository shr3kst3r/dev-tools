"""Tests for the pure layers of slack-me (config parsing + payload building)."""

from __future__ import annotations

import pytest

from tools.slack_me.cli import _parse_args, _resolve_message
from tools.slack_me.config import Config, ConfigError, parse_config
from tools.slack_me.slack import build_payload

_WEBHOOK = "https://hooks.slack.com/services/T00/B00/xxx"


# --- config parsing --------------------------------------------------------


def test_parse_config_reads_webhook_and_username() -> None:
    cfg = parse_config(f'webhook = "{_WEBHOOK}"\nusername = "bot"\n')
    assert cfg == Config(webhook=_WEBHOOK, username="bot")


def test_parse_config_username_optional() -> None:
    cfg = parse_config(f'webhook = "{_WEBHOOK}"\n')
    assert cfg.webhook == _WEBHOOK
    assert cfg.username is None


def test_env_webhook_overrides_file() -> None:
    other = "https://hooks.slack.com/services/env/override/zzz"
    cfg = parse_config(f'webhook = "{_WEBHOOK}"', env_webhook=other)
    assert cfg.webhook == other


def test_env_webhook_stands_in_for_empty_config() -> None:
    cfg = parse_config("", env_webhook=_WEBHOOK)
    assert cfg.webhook == _WEBHOOK


def test_missing_webhook_raises() -> None:
    with pytest.raises(ConfigError, match="no Slack webhook"):
        parse_config('username = "bot"\n')


def test_non_slack_webhook_raises() -> None:
    with pytest.raises(ConfigError, match="does not look like"):
        parse_config('webhook = "https://example.com/hook"\n')


def test_invalid_toml_raises() -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        parse_config("webhook = = broken")


def test_non_string_username_raises() -> None:
    with pytest.raises(ConfigError, match="`username` must be a string"):
        parse_config(f'webhook = "{_WEBHOOK}"\nusername = 3\n')


# --- payload building ------------------------------------------------------


def test_build_payload_plain() -> None:
    cfg = Config(webhook=_WEBHOOK)
    assert build_payload("hello", cfg) == {"text": "hello"}


def test_build_payload_includes_username() -> None:
    cfg = Config(webhook=_WEBHOOK, username="bot")
    assert build_payload("hi", cfg) == {"text": "hi", "username": "bot"}


# --- message resolution ----------------------------------------------------


def test_resolve_message_joins_positional_args() -> None:
    args = _parse_args(["hello", "there", "world"])
    assert _resolve_message(args) == "hello there world"


def test_resolve_message_reads_stdin_when_no_args(monkeypatch) -> None:
    import io
    import sys

    args = _parse_args([])
    # io.StringIO.isatty() already returns False, so this reads as a pipe.
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped body\n"))
    assert _resolve_message(args) == "piped body"


def test_quiet_flag_parses() -> None:
    assert _parse_args(["-q", "msg"]).quiet is True
    assert _parse_args(["msg"]).quiet is False
