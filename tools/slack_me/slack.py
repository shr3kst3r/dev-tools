"""Building and sending Slack incoming-webhook payloads.

`build_payload` is pure (message in, JSON-able dict out) and is what the tests
exercise; `post_message` is the thin urllib wrapper that actually talks to
Slack. We use stdlib `urllib` rather than adding a `requests` dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import Config

# Slack rejects genuinely empty webhook posts; keep the timeout short so a
# hung network doesn't wedge a shell one-liner.
_TIMEOUT_SECONDS = 15


class SlackError(Exception):
    """Raised when Slack rejects the post or the network fails."""


def build_payload(text: str, config: Config) -> dict:
    """Build the JSON payload for a Slack incoming webhook.

    Pure. `text` is sent as Slack mrkdwn; `config.username` (if set) overrides
    the display name of the message.
    """
    payload: dict[str, object] = {"text": text}
    if config.username:
        payload["username"] = config.username
    return payload


def post_message(text: str, config: Config) -> None:
    """Post `text` to the configured Slack webhook.

    Raises `SlackError` on any non-200 response or network failure.
    """
    body = json.dumps(build_payload(text, config)).encode("utf-8")
    request = urllib.request.Request(
        config.webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
            status = resp.status
            reply = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise SlackError(
            f"Slack returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SlackError(f"could not reach Slack: {exc.reason}") from exc

    # Incoming webhooks reply with a plain-text "ok" on success.
    if status != 200 or reply != "ok":
        raise SlackError(f"unexpected Slack response (HTTP {status}): {reply!r}")
