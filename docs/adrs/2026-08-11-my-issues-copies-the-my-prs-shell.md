---
id: 2026-08-11-my-issues-copies-the-my-prs-shell
status: Accepted
supersedes: null
superseded-by: null
components: [my-issues, my-prs, tool-boundaries]
ticket: "#5"
date: 2026-08-11
---
# Give `my-issues` its own copy of the dashboard shell, and share only pr-watch's generic layer

## Context

`my-issues` is a second cross-repo dashboard: same poll-render-select loop as
`my-prs`, pointed at GitHub issues instead of pull requests. Roughly two thirds
of `my-prs` is not about pull requests at all. The threaded poll worker, the
exponential rate-limit backoff, the rolling activity log, the poll-dot strip in
the status bar, the hide list and its pure `partition_hidden`, the persisted
window layout, the help/log modal overlays, the per-view cursor memory, the
`--once` snapshot path, and the `gw` collision-parse/`--rm` retry dance would all
work verbatim on any list of GitHub objects.

The other third is irreducibly about pull requests, and this is the fact that
shapes the decision. An issue has no head branch, no checks, no CI rollup, no
`reviewDecision`, no `mergeable`, no review threads, no draft flag, and no
additions/deletions/changedFiles. So the parts `my-prs` itself borrowed from
`pr-watch` — `PullRequest`, `PRMetrics`, `Check`, `ReviewThread`, `CheckState`,
`parse_pull_request`, and `render_body` — are all the wrong shape here, and
`PrItem`'s four attention flags have no issue analogue whatsoever.

That leaves a real choice about the duplicated two thirds, because the repo has
no precedent for it. `my-prs` reuses `pr-watch`'s *pure* layer — models, parser,
formatting helpers — and owns its own I/O, its own app, and its own rendering.
It has never reused another tool's *shell*. CLAUDE.md describes the unit of
organization as "one tool per directory", each with its own `cli.py:main()`, and
nothing in the repo is currently a library that other tools drive.

There is also a concrete hazard in copying carelessly rather than deliberately:
`hidden.state_path()` and `layout.state_path()` hardcode `my-prs/` under
`$XDG_CONFIG_HOME`, and the hide-list key (`owner/repo#number`) is the *same
shape* for an issue as for a PR. A copy that kept those paths would silently
clobber the PR dashboard's hide list with plausible-looking entries.

## Decision

We copy the dashboard shell into `tools/my_issues/` — `app.py`, `ui.py`,
`github.py`, `gw.py`, `hidden.py`, `layout.py`, `cli.py` — and each tool owns its
copy outright. We do not extract a shared TUI shell, and `my_issues` does not
import from `my_prs`.

Sharing stays exactly where it already is: `tools/my_issues/` imports
`GitHubError`, `_run`, and `require_gh` from `tools.pr_watch.github`, and
`format_relative` from `tools.pr_watch.ui`. That is the same seam `my_prs`
already uses, and it is limited to genuinely domain-free helpers — subprocess
invocation, auth preflight, relative-time formatting. Anything that mentions
pull requests or issues is not shared.

`my-issues` reads and writes its state under `$XDG_CONFIG_HOME/my-issues/`.

## Consequences

The two dashboards can diverge freely, and they will — this ADR's sibling
records that issues get no attention dot, which is already a divergence in the
sort, the columns, and the summary bar. Neither tool can break the other, and
neither has to grow a conditional to accommodate the other's semantics.

The cost is real and should be stated plainly: **a bug fixed in one shell is not
fixed in the other.** Rate-limit backoff, the hide-list degradation behavior, the
gw collision parse, and the layout-file fallbacks now exist twice, and a fix to
`_delay_after` or `parse_exists` in `my_prs` must be applied by hand in
`my_issues`. Roughly 600 lines are duplicated. Anyone touching shell behavior
should grep both trees; the test suites are separate and will not catch the
omission for you.

We are accepting that duplication now because extraction is a larger, separately
justified refactor: it would drag `my-prs` under a new abstraction that does not
exist yet, and designing that abstraction against a sample size of two — where
the second sample is not yet written — is how you get the wrong abstraction. Two
concrete copies are better evidence for what the shared shell should look like
than one copy and a guess.

Constraints this imposes on future work:

- **`my_issues` must not import from `my_prs`, and `my_prs` must not import from
  `my_issues`.** They are peers, not a library and a client. New shared code goes
  into `pr_watch`'s pure layer only if it is genuinely domain-free, or into a new
  module deliberately created for the purpose.
- **Each tool's state files live under its own `$XDG_CONFIG_HOME/<tool>/`
  directory.** No tool reads another's hide list or layout, even though the key
  shapes coincide.
- **A third dashboard is the trigger to revisit this, not to copy again.** When
  the same shell is wanted a third time, extract it and supersede this ADR. Do
  not paste it a third time and cite this record as permission.

## Alternatives considered

- **Extract a generic dashboard shell both tools drive.** The right end state,
  and where a third tool should take us. Wrong now: it requires refactoring
  working, tested code in `my-prs` as a precondition for new work, and the
  abstraction would be designed against one real example plus one imagined one.
  The attention-dot divergence alone suggests the seam is not where it looks.
- **Add issues as a fourth view inside `my-prs`.** No duplication at all, and
  the poll cost could even be folded into the existing request. Rejected: the
  ticket asks for a separate tool, and it is right to — the list columns, the
  detail pane, the sort, and the summary bar would all have to fork on object
  type, turning one clear tool into two tools sharing a namespace. `v` would
  cycle through six views spanning two unrelated concepts.
- **Have `my_issues` import the generic parts directly from `my_prs`.** Cheapest
  possible deduplication, no refactor. Rejected: it makes `my-prs` an accidental
  library, so an ordinary UI change in the PR dashboard becomes a breaking change
  for the issue dashboard, with nothing in `my_prs` marking which of its names
  are load-bearing for someone else.
- **Copy the shell but keep sharing `pr_watch`'s PR models too.** Not viable
  rather than rejected: there is no subset of `PullRequest` that describes an
  issue. Recorded here only because "reuse pr-watch's model" is the reflex the
  repo's existing README trains, and it does not apply this time.
