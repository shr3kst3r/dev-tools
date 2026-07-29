# Reporting the run: Slack, push notification, terminal recap

Shared by `/pr-notebook` and `/azdo-then-notebook`. Edit here, not in either
SKILL.md.

## Slack, via the slack-me CLI

```bash
slack-me "$(cat <<'EOF'
<message>
EOF
)"
```

Posts to Dennis's own webhook from `~/.slack-me.toml` — no channel lookup, no MCP
tools. Exit codes: `0` sent · `1` config missing/malformed or Slack rejected it ·
`2` empty message. On `1`, print the summary to the terminal and tell the user to
check `~/.slack-me.toml`; do not retry and do not invent a webhook URL.

**Show the draft and get an OK before sending. No silent posts.** The webhook has
no draft support, so the only two options are *send now* or *skip and print*.

## Compose as Slack mrkdwn, not Markdown

See the `slack-me` skill for the full reference. The three that bite:
`*bold*` (single asterisks — `**bold**` renders literally), `_italic_`, and
`<https://url|text>` for links (`[text](url)` renders literally, and a bare URL
unfurls into a preview card).

## Message shape

Number the phases the invoking skill actually ran; drop the rest.

```text
*<skill-name>* — <PR title, or "no PR">

*1. Azure DevOps pipeline*: <SUCCESS|FAILED|SKIPPED>       ← azdo-then-notebook only
• Build: <build-url|build 204627>
• Fixes applied: <none | commit subjects>

*2. Image*: `<repo>@<short-digest>`
• Tag at lookup: `pr-<slug>` → `<short-digest>`
• Pushed: <iso ts>
  (or: "skipped — ran on existing cluster `<id>`, image not verified")

*3. Notebook*: <SUCCESS|FAILED|INTERNAL_ERROR|n/a — chain aborted>
• Notebook: `<path>`
• Run: <run-page-url|run page>
• Cluster: `ci:multi:prod:<repo>:<slug>` (inline new_cluster | existing `<id>`)
• Duration: <hh:mm:ss>

*4. Triage findings*: <N>
• <finding>
…
(or "no issues found in cell outputs")

_PR: <pr-url|PR link>_
_Started <iso ts> · Finished <iso ts>_
```

Keep it under ~3000 chars; trim tracebacks to their first 5 lines. When a chain
aborted, say so in the header (`— ABORTED at pipeline step`) rather than burying
it in a phase line. **Never invent findings** — "no issues found" is a real,
useful result and must be stated as one.

## Push notification

Webhook posts may not notify, depending on channel settings, so also call the
`PushNotification` tool once after the Slack step — sent or skipped:

```text
<skill-name>: <SUCCESS|FAILED|ABORTED at pipeline> — <notebook basename>, <N> triage findings
```

Under 200 chars, no markdown. The harness suppresses it when the terminal has
focus; a "Not sent" result is expected and is not worth retrying.

## Terminal recap

Print this regardless of what happened to Slack, so it lands in scrollback:

```
## Summary
- PR: <url>
- Pipeline: <state>                 (azdo-then-notebook only)
- Image: <repo>@<short-digest> (pushed <iso ts>)
- Notebook run: <run-page-url> — <state>
- Issues: <count>
- Slack: <sent | not sent>
- Local notebook: <path to exported .ipynb>
```
