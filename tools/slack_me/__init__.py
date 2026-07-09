"""slack-me — send yourself a Slack message from the command line.

Reads a Slack incoming-webhook URL from a config file in your home directory
(`~/.slack-me.toml`) and posts whatever you pass it — a positional message, or
piped stdin — to that webhook. Handy for pinging yourself when a long-running
job finishes: `long-thing && slack-me "done"`.
"""
