"""Tests for the adr-rpi skill's deterministic corpus scripts.

The scripts live under `skills/adr-rpi/scripts/` rather than `tools/`, because
they ship with the skill and must run in whatever repo the skill is pointed at
(stdlib only, no venv). They are loaded here by path for the same reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "adr-rpi" / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


chain = _load("adr_chain")
index = _load("adr_index")


def write_adr(
    root: Path,
    adr_id: str,
    *,
    status: str = "Accepted",
    components: str = "[checkout, sessions]",
    supersedes: str = "null",
    superseded_by: str = "null",
    ticket: str = "null",
    date: str | None = None,
    title: str | None = None,
    archived: bool = False,
) -> Path:
    directory = root / chain.ARCHIVE_DIRNAME if archived else root
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{adr_id}.md"
    path.write_text(
        "---\n"
        f"id: {adr_id}\n"
        f"status: {status}\n"
        f"supersedes: {supersedes}\n"
        f"superseded-by: {superseded_by}\n"
        f"components: {components}\n"
        f"ticket: {ticket}\n"
        f"date: {date or adr_id[:10]}\n"
        "---\n"
        f"# {title or adr_id.replace('-', ' ')}\n\n"
        "## Context\nForces.\n\n## Decision\nDo the thing.\n",
        encoding="utf-8",
    )
    return path


# --- frontmatter parsing -----------------------------------------------------


def test_parses_inline_lists_block_lists_and_nulls() -> None:
    fields = chain.parse_frontmatter(
        "---\n"
        "id: 2026-01-01-a\n"
        "components: [checkout, sessions]\n"
        "supersedes:\n"
        "  - 2025-12-01-old\n"
        "  - 2025-11-01-older\n"
        "superseded-by: null\n"
        'title_like: "quoted value"\n'
        "ticket: ~\n"
        "---\n"
    )
    assert fields["components"] == ["checkout", "sessions"]
    assert fields["supersedes"] == ["2025-12-01-old", "2025-11-01-older"]
    assert fields["superseded-by"] is None
    assert fields["title_like"] == "quoted value"
    assert fields["ticket"] is None


def test_strips_trailing_comments_but_not_hashes_in_quotes() -> None:
    # The shipped template annotates `status` with the allowed values; a parser
    # that kept the comment would read the whole enumeration back as a status.
    fields = chain.parse_frontmatter(
        "---\n"
        "status: Proposed   # Proposed | Accepted | Superseded\n"
        'note: "issue #42 matters"\n'
        "---\n"
    )
    assert fields["status"] == "Proposed"
    assert fields["note"] == "issue #42 matters"


def test_unclosed_or_unfenced_frontmatter_is_a_format_error() -> None:
    for text in ("# no frontmatter\n", "---\nid: x\n"):
        try:
            chain.parse_frontmatter(text)
        except chain.AdrFormatError:
            continue
        raise AssertionError(f"expected AdrFormatError for {text!r}")


def test_missing_title_heading_is_a_format_error(tmp_path: Path) -> None:
    path = tmp_path / "2026-01-01-a.md"
    path.write_text("---\nid: 2026-01-01-a\nstatus: Accepted\ndate: 2026-01-01\n---\nno heading\n")
    try:
        chain.parse_adr(path, archived=False)
    except chain.AdrFormatError as exc:
        assert "heading" in str(exc)
    else:
        raise AssertionError("expected AdrFormatError")


# --- loading -----------------------------------------------------------------


def test_load_skips_generated_artifacts_and_reads_the_archive(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-02-01-live")
    write_adr(
        tmp_path,
        "2026-01-01-old",
        status="Superseded",
        superseded_by="2026-02-01-live",
        archived=True,
    )
    (tmp_path / "INDEX.md").write_text("# generated\n")
    (tmp_path / "CONSTRAINTS.md").write_text("# generated\n")
    (tmp_path / "README.md").write_text("# prose\n")

    corpus = chain.load_corpus(tmp_path)
    assert set(corpus.adrs) == {"2026-02-01-live", "2026-01-01-old"}
    assert corpus.adrs["2026-01-01-old"].archived is True
    assert corpus.adrs["2026-01-01-old"].active is False


def test_a_malformed_file_becomes_a_problem_not_an_exception(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-02-01-good")
    (tmp_path / "2026-01-01-bad.md").write_text("no frontmatter at all\n")

    corpus = chain.load_corpus(tmp_path)
    assert "2026-02-01-good" in corpus.adrs
    assert any("2026-01-01-bad.md" in p.where for p in corpus.problems)


def test_duplicate_ids_are_reported(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a")
    (tmp_path / "2026-01-02-copy.md").write_text(
        (tmp_path / "2026-01-01-a.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("duplicate id" in p.message for p in problems)


def test_missing_corpus_directory_is_an_error_not_a_crash(tmp_path: Path) -> None:
    corpus = chain.load_corpus(tmp_path / "nope")
    assert corpus.adrs == {}
    assert any(p.level == chain.ERROR for p in corpus.problems)


# --- chains ------------------------------------------------------------------


def _abc(tmp_path: Path) -> None:
    """A -> B -> C, the canonical chain from the brief."""
    write_adr(tmp_path, "2026-01-01-a", status="Superseded", superseded_by="2026-02-01-b")
    write_adr(
        tmp_path,
        "2026-02-01-b",
        status="Superseded",
        supersedes="[2026-01-01-a]",
        superseded_by="2026-03-01-c",
    )
    write_adr(tmp_path, "2026-03-01-c", supersedes="[2026-02-01-b]")


def test_head_walks_forward_to_the_live_decision(tmp_path: Path) -> None:
    _abc(tmp_path)
    corpus = chain.load_corpus(tmp_path)
    assert chain.head(corpus, "2026-01-01-a") == "2026-03-01-c"
    assert chain.head(corpus, "2026-03-01-c") == "2026-03-01-c"
    assert chain.head(corpus, "unknown-id") == "unknown-id"


def test_ancestors_are_transitive_and_oldest_first(tmp_path: Path) -> None:
    _abc(tmp_path)
    corpus = chain.load_corpus(tmp_path)
    assert chain.ancestors(corpus, "2026-03-01-c") == ("2026-01-01-a", "2026-02-01-b")
    assert chain.ancestors(corpus, "2026-01-01-a") == ()


def test_chains_reports_one_entry_per_live_head(tmp_path: Path) -> None:
    _abc(tmp_path)
    write_adr(tmp_path, "2026-04-01-unrelated")
    assert chain.chains(chain.load_corpus(tmp_path)) == [
        ("2026-03-01-c", ("2026-01-01-a", "2026-02-01-b"))
    ]


def test_a_well_formed_chain_validates_clean(tmp_path: Path) -> None:
    _abc(tmp_path)
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert [p for p in problems if p.level == chain.ERROR] == []


def test_cycles_are_caught_rather_than_hanging(tmp_path: Path) -> None:
    write_adr(
        tmp_path,
        "2026-01-01-a",
        status="Superseded",
        supersedes="[2026-02-01-b]",
        superseded_by="2026-02-01-b",
    )
    write_adr(
        tmp_path,
        "2026-02-01-b",
        status="Superseded",
        supersedes="[2026-01-01-a]",
        superseded_by="2026-01-01-a",
    )
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("cycle" in p.message for p in problems)


# --- validation: the immutability contract -----------------------------------


def test_one_sided_supersession_is_an_error(tmp_path: Path) -> None:
    # This is the failure mode that lets a decision quietly disappear: the new
    # ADR claims the old one, the old one never admits it.
    write_adr(tmp_path, "2026-01-01-a")
    write_adr(tmp_path, "2026-02-01-b", supersedes="[2026-01-01-a]")
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("does not point back" in p.message for p in problems)
    assert any("still `Accepted`" in p.message for p in problems)


def test_dangling_supersedes_reference_is_an_error(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-02-01-b", supersedes="[2026-01-01-missing]")
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("does not exist" in p.message for p in problems)


def test_superseded_status_requires_a_successor(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a", status="Superseded")
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("superseded-by is empty" in p.message for p in problems)


def test_successor_pointer_without_superseded_status_is_an_error(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a", superseded_by="2026-02-01-b")
    write_adr(tmp_path, "2026-02-01-b", supersedes="[2026-01-01-a]")
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("status is `Accepted`" in p.message for p in problems)


def test_unknown_status_and_bad_date_and_id_mismatch_are_errors(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a", status="Draft", date="July 2026")
    (tmp_path / "2026-01-02-renamed.md").write_text(
        "---\nid: not-the-filename\nstatus: Accepted\ndate: 2026-01-02\n---\n# T\n",
        encoding="utf-8",
    )
    messages = [p.message for p in chain.validate(chain.load_corpus(tmp_path))]
    assert any("status `Draft`" in m for m in messages)
    assert any("not YYYY-MM-DD" in m for m in messages)
    assert any("does not match filename" in m for m in messages)


def test_archiving_a_non_superseded_adr_is_an_error(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a", archived=True)
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert any("archived under" in p.message for p in problems)


def test_superseded_adr_left_in_place_is_allowed(tmp_path: Path) -> None:
    # Archiving is a safe path change, not a required one.
    write_adr(tmp_path, "2026-01-01-a", status="Superseded", superseded_by="2026-02-01-b")
    write_adr(tmp_path, "2026-02-01-b", supersedes="[2026-01-01-a]")
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert [p for p in problems if p.level == chain.ERROR] == []


def test_componentless_adr_is_a_note_not_an_error(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a", components="[]")
    problems = chain.validate(chain.load_corpus(tmp_path))
    assert [p for p in problems if p.level == chain.ERROR] == []
    assert any(p.level == chain.NOTE and "components" in p.message for p in problems)


# --- relevance ---------------------------------------------------------------


def test_relevant_matches_components_case_insensitively_and_skips_superseded(
    tmp_path: Path,
) -> None:
    write_adr(tmp_path, "2026-03-01-c", components="[Checkout]")
    write_adr(tmp_path, "2026-02-01-b", components="[billing]")
    write_adr(
        tmp_path,
        "2026-01-01-a",
        components="[checkout]",
        status="Superseded",
        superseded_by="2026-03-01-c",
    )
    corpus = chain.load_corpus(tmp_path)
    assert [a.id for a in chain.relevant(corpus, ["checkout"])] == ["2026-03-01-c"]
    assert [a.id for a in chain.relevant(corpus, ["checkout", "billing"])] == [
        "2026-03-01-c",
        "2026-02-01-b",
    ]
    assert [a.id for a in chain.relevant(corpus, [])] == ["2026-03-01-c", "2026-02-01-b"]


# --- index generation --------------------------------------------------------


def test_index_is_deterministic_and_check_detects_staleness(tmp_path: Path) -> None:
    _abc(tmp_path)
    assert index.main([str(tmp_path)]) == 0
    first = (tmp_path / "INDEX.md").read_text(encoding="utf-8")

    assert index.main([str(tmp_path), "--check"]) == 0
    assert index.main([str(tmp_path)]) == 0
    assert (tmp_path / "INDEX.md").read_text(encoding="utf-8") == first, "regeneration must be a no-op"

    write_adr(tmp_path, "2026-05-01-new")
    assert index.main([str(tmp_path), "--check"]) == 1


def test_index_separates_active_from_superseded_and_links_the_replacement(
    tmp_path: Path,
) -> None:
    write_adr(tmp_path, "2026-02-01-live", ticket="PROD-334", title="Move sessions to Redis")
    write_adr(
        tmp_path,
        "2026-01-01-old",
        status="Superseded",
        superseded_by="2026-02-01-live",
        archived=True,
    )
    rendered = index.render_index(chain.load_corpus(tmp_path))

    assert "1 active · 1 superseded" in rendered
    assert "## Active" in rendered and "## Superseded" in rendered
    assert "[2026-02-01-live](2026-02-01-live.md)" in rendered
    assert "[2026-01-01-old](superseded/2026-01-01-old.md)" in rendered
    assert "PROD-334" in rendered
    assert rendered.index("## Active") < rendered.index("## Superseded")
    assert rendered.startswith("<!-- GENERATED"), "the header must announce it is generated"

    active_row = next(line for line in rendered.splitlines() if "2026-02-01-live]" in line)
    superseded_row = next(line for line in rendered.splitlines() if "2026-01-01-old]" in line)
    assert "Move sessions to Redis" in active_row
    assert "[2026-02-01-live](2026-02-01-live.md)" in superseded_row


def test_index_escapes_pipes_in_titles(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-01-01-a", title="Use A | not B")
    rendered = index.render_index(chain.load_corpus(tmp_path))
    assert "Use A \\| not B" in rendered
    assert len([line for line in rendered.splitlines() if line.startswith("| [")]) == 1


def test_empty_corpus_still_renders(tmp_path: Path) -> None:
    rendered = index.render_index(chain.load_corpus(tmp_path))
    assert "0 active · 0 superseded" in rendered
    assert "No ADRs yet." in rendered


def test_index_still_generates_when_the_corpus_has_errors(tmp_path: Path) -> None:
    write_adr(tmp_path, "2026-02-01-b", supersedes="[2026-01-01-missing]")
    assert index.main([str(tmp_path)]) == 0
    assert "2026-02-01-b" in (tmp_path / "INDEX.md").read_text(encoding="utf-8")


# --- chain cli ---------------------------------------------------------------


def test_validate_exits_nonzero_only_on_errors(tmp_path: Path) -> None:
    _abc(tmp_path)
    assert chain.main([str(tmp_path), "--validate"]) == 0

    write_adr(tmp_path, "2026-06-01-broken", supersedes="[2026-01-01-nope]")
    assert chain.main([str(tmp_path), "--validate"]) == 1


def test_cli_head_and_relevant_print_stable_lines(tmp_path: Path, capsys) -> None:
    _abc(tmp_path)
    assert chain.main([str(tmp_path), "--head", "2026-01-01-a"]) == 0
    assert capsys.readouterr().out.strip() == "2026-03-01-c"

    assert chain.main([str(tmp_path), "--relevant", "checkout"]) == 0
    assert capsys.readouterr().out.strip() == "Accepted\t2026-03-01-c\t2026-03-01-c.md"


def test_cli_json_is_sorted_and_includes_chains(tmp_path: Path, capsys) -> None:
    import json

    _abc(tmp_path)
    assert chain.main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["id"] for a in payload["adrs"]] == [
        "2026-03-01-c",
        "2026-02-01-b",
        "2026-01-01-a",
    ]
    assert payload["chains"] == [
        {"head": "2026-03-01-c", "ancestors": ["2026-01-01-a", "2026-02-01-b"]}
    ]
