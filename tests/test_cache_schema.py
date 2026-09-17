"""Tests for cache persistence models and the pure codec."""

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from doc_lattice import __version__
from doc_lattice.cache.schema import (
    CacheFile,
    Entry,
    FilePayload,
    SectionRecordModel,
    StatRecord,
    make_entry,
    reconstruct_facts,
    stat_record,
)
from doc_lattice.constants import CACHE_VERSION
from doc_lattice.loader import derive_file_sections
from doc_lattice.model import FileFacts, FileSections, NodeMeta, ParsedMeta, SectionRecord
from doc_lattice.orchestrate import _parse_file_facts

ROOT = "/abs/current-root"


def _fake_stat(size: int = 10, mtime_ns: int = 123) -> SimpleNamespace:
    return SimpleNamespace(st_size=size, st_mtime_ns=mtime_ns)


def _entry(facts: FileFacts, data: bytes = b"raw bytes\n") -> Entry:
    return make_entry(
        data,
        facts,
        _fake_stat(size=len(data)),  # ty: ignore[invalid-argument-type]
        ROOT,
    )


def _sample_cache_file() -> CacheFile:
    texts = {
        "docs/a.md": "---\nid: a\n---\n# A\n",
        "docs/plain.md": "# plain\n",
        "docs/id-less.md": "---\nname: foreign\n---\n# Body\n",
    }
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=[ROOT],
        entries={
            path: _entry(_parse_file_facts(text, Path(path)), text.encode())
            for path, text in texts.items()
        },
    )


def test_cache_file_round_trips_through_json() -> None:
    original = _sample_cache_file()
    reloaded = CacheFile.model_validate_json(original.model_dump_json())
    assert reloaded == original
    assert isinstance(reloaded.entries["docs/a.md"].payload.meta, NodeMeta)


def test_stat_record_captures_size_and_mtime() -> None:
    result = stat_record(
        _fake_stat(size=42, mtime_ns=999)  # ty: ignore[invalid-argument-type]
    )
    assert result == StatRecord(size=42, mtime_ns=999)


def test_make_entry_hashes_bytes_resets_stats_and_preserves_file_payload() -> None:
    data = b"raw bytes\n"
    meta = NodeMeta.model_validate({"id": "a"})
    facts = FileFacts(
        parsed=ParsedMeta(meta=meta, disposition="tracked"),
        body="# A\nbody\n",
        body_first_line=4,
        sections=FileSections(
            total_lines=2,
            sections=(SectionRecord(anchor="a-top", start=1, end=2),),
        ),
    )

    entry = _entry(facts, data)

    assert entry.file_sha256 == sha256(data).hexdigest()
    assert entry.stats == {ROOT: StatRecord(size=len(data), mtime_ns=123)}
    assert entry.disposition == "tracked"
    assert entry.payload == FilePayload(
        meta=meta,
        body="# A\nbody\n",
        body_first_line=4,
        total_lines=2,
        sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
    )
    assert reconstruct_facts(entry) == facts


@pytest.mark.parametrize(
    ("text", "disposition", "body", "first_line"),
    [
        ("# Plain\n", "untracked", "# Plain\n", 1),
        ("", "untracked", "", 1),
        ("---\n---\n", "untracked", "", 3),
        ("---\n---", "untracked", "", 2),
        ("---\n---\n# Body\n", "untracked", "# Body\n", 3),
        ("---\nscalar\n---\n# Body\n", "untracked", "# Body\n", 4),
        ("---\n- item\n---\n# Body\n", "untracked", "# Body\n", 4),
        ("---\nname: foreign\n---\n# Body\n", "id-less", "# Body\n", 4),
        ("---\nname: foreign\n---\n", "id-less", "", 4),
        (
            "---\nname: foreign\n---\n<!-- doc-lattice\nid: late\n-->\n",
            "misplaced-envelope",
            "<!-- doc-lattice\nid: late\n-->\n",
            4,
        ),
    ],
)
def test_non_node_facts_survive_the_codec(text, disposition, body, first_line):
    facts = _parse_file_facts(text, Path("docs/foreign.md"))
    entry = _entry(facts, text.encode())
    revived = Entry.model_validate_json(entry.model_dump_json())

    assert revived.disposition == disposition
    assert revived.payload.meta is None
    assert revived.payload.body == body
    assert revived.payload.body_first_line == first_line
    assert reconstruct_facts(revived) == facts
    if body == "# Body\n":
        assert facts.sections == FileSections(1, (SectionRecord("body", 1, 1),))
    elif not body:
        assert facts.sections == FileSections(1, ())


def test_entry_requires_every_replayed_diagnostic_rather_than_defaulting_one() -> None:
    payload = _sample_cache_file().entries["docs/plain.md"].model_dump(mode="json")
    for field in ("disposition", "reused_anchors", "shadowed_envelope"):
        del payload[field]
    with pytest.raises(ValidationError) as exc:
        Entry.model_validate(payload)

    missing = {error["loc"] for error in exc.value.errors() if error["type"] == "missing"}
    assert missing == {("disposition",), ("reused_anchors",), ("shadowed_envelope",)}


@pytest.mark.parametrize("field", ["meta", "body", "body_first_line", "total_lines", "sections"])
def test_even_empty_files_require_complete_payloads(field):
    payload = _entry(_parse_file_facts("", Path("empty.md")), b"").payload.model_dump()
    del payload[field]
    with pytest.raises(ValidationError) as exc:
        FilePayload.model_validate(payload)
    assert any(
        error["loc"] == (field,) and error["type"] == "missing" for error in exc.value.errors()
    )


@pytest.mark.parametrize("first_line", [0, -1])
def test_payload_refuses_nonpositive_body_line(first_line):
    payload = _sample_cache_file().entries["docs/plain.md"].payload.model_dump()
    payload["body_first_line"] = first_line
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        FilePayload.model_validate(payload)


@pytest.mark.parametrize("flag", ["reused_anchors", "shadowed_envelope"])
def test_diagnostic_flags_survive_reconstruction(flag):
    text = (
        "---\nid: a\ntitle: &t Title\nlayer: &t design\n---\n# A\n"
        if flag == "reused_anchors"
        else "---\nid: a\n---\n<!-- doc-lattice\nid: other\n-->\n"
    )
    facts = _parse_file_facts(text, Path("docs/a.md"))
    assert getattr(facts.parsed, flag) is True
    entry = _entry(facts, text.encode())
    revived = Entry.model_validate_json(entry.model_dump_json())
    assert reconstruct_facts(revived) == facts


@st.composite
def _markdown_body(draw: st.DrawFn) -> str:
    lines = draw(
        st.lists(
            st.one_of(
                st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
                st.builds(
                    lambda n, text: "#" * n + " " + text,
                    st.integers(min_value=1, max_value=6),
                    st.text("abc ", max_size=10),
                ),
            ),
            max_size=25,
        )
    )
    return "\n".join(lines)


@settings(max_examples=200)
@given(_markdown_body(), st.integers(min_value=1, max_value=100))
def test_file_sections_survive_cache_codec_round_trip(body: str, first_line: int) -> None:
    facts = FileFacts(
        parsed=ParsedMeta(meta=None, disposition="untracked"),
        body=body,
        body_first_line=first_line,
        sections=derive_file_sections(body, first_line=first_line),
    )
    entry = _entry(facts, body.encode())
    revived = Entry.model_validate_json(entry.model_dump_json())
    assert reconstruct_facts(revived) == facts


def test_collision_provenance_round_trips_through_the_cache():
    facts = _parse_file_facts("---\nname: foreign\n---\n# Notes\n\n# Notes\n", Path("a.md"))
    revived = Entry.model_validate_json(_entry(facts).model_dump_json())
    reconstructed = reconstruct_facts(revived)

    assert reconstructed == facts
    assert [(r.start, r.end) for r in reconstructed.sections.sections] == [(1, 2), (3, 3)]
    members = reconstructed.sections.sections[0].collision
    assert members is not None
    assert [(m.label, m.line) for m in members] == [("Notes", 4), ("Notes", 6)]


def test_ancestor_context_round_trips_through_the_cache():
    text = "---\nname: foreign\n---\nProduct A\n---------\n\n### Setup\nrun it\n"
    facts = _parse_file_facts(text, Path("a.md"))
    revived = Entry.model_validate_json(_entry(facts, text.encode()).model_dump_json())
    reconstructed = reconstruct_facts(revived)

    assert reconstructed == facts
    assert [record.context for record in reconstructed.sections.sections] == [("## Product A",)]
