---
name: slack-me
description: "Send the user a Slack message from the command line via the slack-me CLI, which posts to an incoming webhook configured in ~/.slack-me.toml. Trigger when the user asks to be pinged/notified/messaged/slacked — e.g. 'slack me when this finishes', 'ping me the result', 'notify me when the build is done', 'text me on Slack' — or when you want to proactively surface a finished long-running job. Also the reference for Slack's mrkdwn formatting (which differs from Markdown) whenever you compose Slack message text."
argument-hint: "<message> | echo <text> | slack-me"
allowed-tools: [Bash]
---

# /slack-me — ping the user on Slack

A tiny CLI that posts a message to the user's own Slack via an incoming webhook.
In this repo run it as `uv run slack-me ...` (or `just slack-me ...`); if
published via spg, plain `slack-me ...` works anywhere on `$PATH`.

## Agent rules

- **Message source**: positional args OR stdin. `slack-me "done"` or
  `long-job 2>&1 | slack-me`. Both work; prefer positional for short messages.
- **Format the text as Slack mrkdwn, not Markdown** (see below). This is the
  most common mistake — `**bold**` and `[text](url)` render as literal garbage.
- **Use `--quiet`** when firing from a script/pipeline where the confirmation
  panel would be noise; omit it when the user wants to see it landed.
- **Trust the exit codes**: `0` sent · `1` config missing/malformed or Slack
  rejected the post · `2` nothing to send (no args and empty stdin).
- **Config**: reads `~/.slack-me.toml` (`webhook = "https://hooks.slack.com/..."`,
  optional `username`). Overridable via `$SLACK_ME_WEBHOOK` and `$SLACK_ME_CONFIG`.
  If exit `1` says no webhook, tell the user to create `~/.slack-me.toml` — don't
  invent a URL.
- Don't send secrets, tokens, or large blobs — this posts to a real channel.

## Intent → command

| User intent | Command |
|---|---|
| "slack me / ping me / notify me: <text>" | `slack-me "<text>"` |
| "slack me when this finishes" | `<their-command> && slack-me "✅ done" \|\| slack-me "❌ failed"` |
| "send the output of X to Slack" | `X 2>&1 \| slack-me` |
| fire from a script, no local echo | `slack-me --quiet "<text>"` |
| show the formatting / config help | `slack-me --help` |

## Slack mrkdwn — NOT regular Markdown

Slack's `text` field auto-formats **mrkdwn**, whose syntax differs from
GitHub-flavored Markdown. Compose message text with these rules:

**What works:**

| Effect | Slack mrkdwn |
|---|---|
| bold | `*bold*` — single asterisks (NOT `**double**`) |
| italic | `_italic_` — underscores |
| strikethrough | `~strike~` — tildes |
| link | `<https://url\|text>` (NOT `[text](url)`) |
| inline code | `` `code` `` |
| code block | triple backticks |
| bullet list | `•` or `-` at line start |
| blockquote | `> quoted` |

**What does NOT work — renders as literal text:** `#` headings, `**bold**`,
`[text](url)` links, Markdown images `![]()`, and tables. Avoid them.

**Two ways to send richer content:**

1. **Simple message** (what `slack-me` sends today): the text goes in the
   webhook's `text` field and Slack auto-formats mrkdwn. Good for almost
   everything.
2. **Block Kit** (for real structure — headers, dividers, fields, buttons):
   post a `blocks` array instead. In blocks, mrkdwn is opt-in *per text object*
   (`{"type": "mrkdwn", "text": "..."}`), and the `header` block is
   plain-text only. `slack-me` doesn't expose Block Kit — reach for a direct
   webhook POST if the user needs it.
