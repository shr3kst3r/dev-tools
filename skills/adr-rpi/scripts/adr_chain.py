#!/usr/bin/env python3
"""Load an ADR corpus, resolve supersession chains, and validate its structure.

Stdlib only, deliberately. This runs inside whatever repository the adr-rpi
skill is pointed at — not inside this project's venv — so it cannot import
PyYAML or anything else off PyPI. The frontmatter dialect accepted here is
correspondingly small; see `parse_frontmatter` for exactly what it handles.

No model is involved in anything this file does. Every answer it gives is a
function of the frontmatter on disk, which is the point: the index and the
chain view can be regenerated at any time and cannot drift from the source.

Usage:
    adr_chain.py <corpus-dir>                      # human-readable chain report
    adr_chain.py <corpus-dir> --json               # same, as JSON
    adr_chain.py <corpus-dir> --validate           # exit 1 on structural errors
    adr_chain.py <corpus-dir> --head <adr-id>      # latest ADR in this one's chain
    adr_chain.py <corpus-dir> --relevant a,b       # active ADRs touching a or b
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

STATUSES = ("Proposed", "Accepted", "Superseded")
ARCHIVE_DIRNAME = "superseded"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_NULLISH = frozenset({"", "null", "~", "none"})

ERROR = "error"
NOTE = "note"


class AdrFormatError(Exception):
    """A single ADR file could not be parsed at all."""


@dataclass(frozen=True)
class Problem:
    """Something wrong with the corpus. `level` is ERROR or NOTE."""

    level: str
    where: str
    message: str

    def render(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


@dataclass(frozen=True)
class Adr:
    path: Path
    id: str
    title: str
    status: str
    date: str
    components: tuple[str, ...]
    supersedes: tuple[str, ...]
    superseded_by: str | None
    ticket: str | None
    archived: bool

    @property
    def active(self) -> bool:
        """Proposed and Accepted ADRs are active; Superseded ones are history."""
        return self.status != "Superseded"


@dataclass
class Corpus:
    root: Path
    adrs: dict[str, Adr] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    def sorted_adrs(self, *, newest_first: bool = True) -> list[Adr]:
        return sorted(
            self.adrs.values(),
            key=lambda a: (a.date, a.id),
            reverse=newest_first,
        )

    def rel(self, adr: Adr) -> str:
        """Corpus-relative POSIX path, for links inside generated artifacts."""
        return adr.path.relative_to(self.root).as_posix()


# --- frontmatter -------------------------------------------------------------


def _strip_comment(value: str) -> str:
    """Drop a trailing `# comment`, respecting quotes.

    The ADR template ships with inline comments (`status: Proposed  # Proposed |
    Accepted | Superseded`), so a parser that keeps them would read the whole
    enumeration back as the status.
    """
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _parse_inline_list(value: str) -> list[str]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    return [_unquote(part.strip()) for part in inner.split(",") if part.strip()]


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the YAML subset ADR frontmatter is allowed to use.

    Supported: `key: scalar`, `key: [a, b]`, `key:` followed by `- a` block
    items, `null`/`~`/empty for absent, single or double quotes, and trailing
    comments. Anything else is a format error rather than a silent
    misinterpretation — a decision record that parses differently than it reads
    is worse than one that refuses to parse.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AdrFormatError("file does not open with a `---` frontmatter fence")

    fields: dict[str, object] = {}
    key: str | None = None
    pending: list[str] | None = None

    def flush() -> None:
        nonlocal key, pending
        if key is not None and pending is not None:
            fields[key] = pending if pending else None
        key, pending = None, None

    for raw in lines[1:]:
        stripped = raw.strip()
        if stripped == "---":
            flush()
            return fields
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            if pending is None:
                raise AdrFormatError(f"list item outside a key: {stripped!r}")
            item = _unquote(_strip_comment(stripped[1:].strip()))
            if item:
                pending.append(item)
            continue
        if ":" not in stripped:
            raise AdrFormatError(f"not a `key: value` line: {stripped!r}")
        flush()
        name, _, rest = stripped.partition(":")
        name = name.strip()
        value = _strip_comment(rest.strip())
        if value == "":
            key, pending = name, []
        elif value.startswith("[") and value.endswith("]"):
            fields[name] = _parse_inline_list(value)
        else:
            unquoted = _unquote(value)
            # Normalize nullishness here so every caller sees one representation
            # of "absent" instead of having to remember that `null` arrives as a
            # four-character string.
            fields[name] = None if unquoted.lower() in _NULLISH else unquoted

    raise AdrFormatError("frontmatter is never closed with `---`")


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    text = str(value).strip()
    return () if text.lower() in _NULLISH else (text,)


def _as_scalar(name: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        raise AdrFormatError(f"`{name}` must be a single value, not a list")
    text = str(value).strip()
    return None if text.lower() in _NULLISH else text


def _title_of(text: str) -> str:
    body = text.split("---", 2)[-1]
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    raise AdrFormatError("no `# Title` heading after the frontmatter")


def parse_adr(path: Path, *, archived: bool) -> Adr:
    text = path.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)
    missing = [k for k in ("id", "status", "date") if _as_scalar(k, fields.get(k)) is None]
    if missing:
        raise AdrFormatError(f"missing required frontmatter: {', '.join(missing)}")
    return Adr(
        path=path,
        id=str(_as_scalar("id", fields["id"])),
        title=_title_of(text),
        status=str(_as_scalar("status", fields["status"])),
        date=str(_as_scalar("date", fields["date"])),
        components=_as_tuple(fields.get("components")),
        supersedes=_as_tuple(fields.get("supersedes")),
        superseded_by=_as_scalar("superseded-by", fields.get("superseded-by")),
        ticket=_as_scalar("ticket", fields.get("ticket")),
        archived=archived,
    )


# --- corpus ------------------------------------------------------------------


def load_corpus(root: Path) -> Corpus:
    """Read every ADR under `root` and `root/superseded/`, collecting problems.

    Parse failures become problems rather than exceptions so one malformed file
    cannot stop the index from regenerating — a corpus that refuses to project
    itself because of one bad file is a corpus nobody reads.
    """
    corpus = Corpus(root=root)
    if not root.is_dir():
        corpus.problems.append(Problem(ERROR, str(root), "corpus directory does not exist"))
        return corpus

    candidates = sorted(root.glob("*.md")) + sorted((root / ARCHIVE_DIRNAME).glob("*.md"))
    for path in candidates:
        if path.name in ("INDEX.md", "CONSTRAINTS.md", "README.md"):
            continue
        archived = path.parent.name == ARCHIVE_DIRNAME
        where = path.relative_to(root).as_posix()
        try:
            adr = parse_adr(path, archived=archived)
        except (AdrFormatError, OSError, UnicodeDecodeError) as exc:
            corpus.problems.append(Problem(ERROR, where, str(exc)))
            continue
        if adr.id in corpus.adrs:
            corpus.problems.append(
                Problem(ERROR, where, f"duplicate id `{adr.id}`, also in "
                                      f"{corpus.rel(corpus.adrs[adr.id])}")
            )
            continue
        corpus.adrs[adr.id] = adr
    return corpus


def validate(corpus: Corpus) -> list[Problem]:
    """Cross-check the corpus. Returns load problems plus structural ones.

    The rules encode the immutability contract: supersession has to be recorded
    on both ends, so `supersedes` and `superseded-by` are checked against each
    other rather than trusted individually. A one-sided link is how a decision
    quietly disappears.
    """
    problems = list(corpus.problems)
    for adr in corpus.sorted_adrs(newest_first=False):
        where = corpus.rel(adr)

        if adr.status not in STATUSES:
            problems.append(
                Problem(ERROR, where, f"status `{adr.status}` is not one of {', '.join(STATUSES)}")
            )
        if not DATE_RE.match(adr.date):
            problems.append(Problem(ERROR, where, f"date `{adr.date}` is not YYYY-MM-DD"))
        if not ID_RE.match(adr.id):
            problems.append(Problem(ERROR, where, f"id `{adr.id}` has unusable characters"))
        if adr.id != adr.path.stem:
            problems.append(
                Problem(ERROR, where, f"id `{adr.id}` does not match filename `{adr.path.stem}`")
            )
        if not adr.components:
            problems.append(
                Problem(NOTE, where, "no components — this ADR will not match a component filter")
            )

        if adr.status == "Superseded" and adr.superseded_by is None:
            problems.append(Problem(ERROR, where, "status is Superseded but superseded-by is empty"))
        if adr.status != "Superseded" and adr.superseded_by is not None:
            problems.append(
                Problem(ERROR, where, f"superseded-by is set but status is `{adr.status}`")
            )
        if adr.archived and adr.status != "Superseded":
            problems.append(
                Problem(ERROR, where, f"archived under {ARCHIVE_DIRNAME}/ but status is `{adr.status}`")
            )

        if adr.superseded_by is not None:
            successor = corpus.adrs.get(adr.superseded_by)
            if successor is None:
                problems.append(
                    Problem(ERROR, where, f"superseded-by `{adr.superseded_by}` does not exist")
                )
            elif adr.id not in successor.supersedes:
                problems.append(
                    Problem(ERROR, where, f"`{successor.id}` does not list this ADR in supersedes")
                )

        for old_id in adr.supersedes:
            predecessor = corpus.adrs.get(old_id)
            if predecessor is None:
                problems.append(Problem(ERROR, where, f"supersedes `{old_id}` which does not exist"))
                continue
            if predecessor.superseded_by != adr.id:
                problems.append(
                    Problem(ERROR, where, f"`{old_id}` does not point back with superseded-by")
                )
            if predecessor.status != "Superseded":
                problems.append(
                    Problem(ERROR, where, f"`{old_id}` is superseded but still `{predecessor.status}`")
                )

    for adr in corpus.sorted_adrs(newest_first=False):
        cycle = _cycle_from(corpus, adr.id)
        if cycle is not None:
            problems.append(
                Problem(ERROR, corpus.rel(adr), f"supersession cycle: {' -> '.join(cycle)}")
            )
            break
    return problems


def _cycle_from(corpus: Corpus, start: str) -> list[str] | None:
    seen: list[str] = []
    current: str | None = start
    while current is not None:
        if current in seen:
            return seen[seen.index(current):] + [current]
        seen.append(current)
        adr = corpus.adrs.get(current)
        current = adr.superseded_by if adr is not None else None
    return None


def head(corpus: Corpus, adr_id: str) -> str:
    """Walk forward to the live ADR in this one's chain.

    An agent that stumbles on an old decision needs one hop to the one that
    replaced it, without reading the intervening history.
    """
    seen: set[str] = set()
    current = adr_id
    while current not in seen:
        seen.add(current)
        adr = corpus.adrs.get(current)
        if adr is None or adr.superseded_by is None:
            return current
        current = adr.superseded_by
    return current


def ancestors(corpus: Corpus, adr_id: str) -> tuple[str, ...]:
    """Every ADR this one replaced, transitively, oldest first."""
    found: dict[str, Adr] = {}
    stack = list(corpus.adrs[adr_id].supersedes) if adr_id in corpus.adrs else []
    while stack:
        current = stack.pop()
        if current in found:
            continue
        adr = corpus.adrs.get(current)
        if adr is None:
            continue
        found[current] = adr
        stack.extend(adr.supersedes)
    return tuple(a.id for a in sorted(found.values(), key=lambda a: (a.date, a.id)))


def chains(corpus: Corpus) -> list[tuple[str, tuple[str, ...]]]:
    """Every (head, ancestors) pair, for ADRs that replaced something."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for adr in corpus.sorted_adrs():
        if adr.superseded_by is None and adr.supersedes:
            out.append((adr.id, ancestors(corpus, adr.id)))
    return out


def relevant(corpus: Corpus, components: list[str]) -> list[Adr]:
    """Active ADRs touching any of `components`, newest first.

    This is the deterministic entry point for the research phase's tiered read:
    the set of ADRs to read in full comes from frontmatter, not from a model's
    guess about which filenames look related.
    """
    wanted = {c.strip().lower() for c in components if c.strip()}
    return [
        adr
        for adr in corpus.sorted_adrs()
        if adr.active and (not wanted or {c.lower() for c in adr.components} & wanted)
    ]


# --- cli ---------------------------------------------------------------------


def _report(corpus: Corpus, problems: list[Problem]) -> str:
    lines: list[str] = []
    active = [a for a in corpus.sorted_adrs() if a.active]
    superseded = [a for a in corpus.sorted_adrs() if not a.active]
    lines.append(f"{len(active)} active, {len(superseded)} superseded, in {corpus.root}")

    resolved = chains(corpus)
    if resolved:
        lines.append("")
        lines.append("Supersession chains (oldest -> live):")
        for head_id, olds in resolved:
            lines.append(f"  {' -> '.join([*olds, head_id])}")
    if problems:
        lines.append("")
        lines.append("Problems:")
        lines.extend(f"  {p.render()}" for p in problems)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("corpus", type=Path, help="ADR corpus directory, e.g. docs/adrs")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--validate", action="store_true", help="exit 1 if the corpus has errors")
    parser.add_argument("--head", metavar="ID", help="print the live ADR in ID's chain")
    parser.add_argument("--relevant", metavar="A,B", help="active ADRs touching these components")
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    problems = validate(corpus)
    errors = [p for p in problems if p.level == ERROR]

    if args.head:
        print(head(corpus, args.head))
        return 1 if args.validate and errors else 0

    if args.relevant is not None:
        for adr in relevant(corpus, args.relevant.split(",")):
            print(f"{adr.status}\t{adr.id}\t{corpus.rel(adr)}")
        return 1 if args.validate and errors else 0

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(corpus.root),
                    "adrs": [
                        {
                            "id": a.id,
                            "title": a.title,
                            "status": a.status,
                            "date": a.date,
                            "components": list(a.components),
                            "supersedes": list(a.supersedes),
                            "superseded_by": a.superseded_by,
                            "ticket": a.ticket,
                            "path": corpus.rel(a),
                        }
                        for a in corpus.sorted_adrs()
                    ],
                    "chains": [{"head": h, "ancestors": list(o)} for h, o in chains(corpus)],
                    "problems": [
                        {"level": p.level, "where": p.where, "message": p.message} for p in problems
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_report(corpus, problems))

    return 1 if errors and args.validate else 0


if __name__ == "__main__":
    sys.exit(main())
