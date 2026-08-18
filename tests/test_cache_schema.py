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
    NodePayload,
    SectionRecordModel,
    StatRecord,
    make_entry,
    reconstruct_doc,
    stat_record,
)
from doc_lattice.constants import CACHE_VERSION
from doc_lattice.loader import derive_file_sections
from doc_lattice.model import FileSections, NodeMeta, ParsedDoc, ParsedMeta, SectionRecord

ROOT = "/abs/current-root"


def _fake_stat(size: int = 10, mtime_ns: int = 123) -> SimpleNamespace:
    return SimpleNamespace(st_size=size, st_mtime_ns=mtime_ns)


def _sample_cache_file() -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=["/abs/root"],
        entries={
            "docs/a.md": Entry(
                file_sha256="a" * 64,
                stats={"/abs/root": StatRecord(size=10, mtime_ns=123)},
                node=NodePayload(
                    meta=NodeMeta.model_validate({"id": "a"}),
                    body="# A\n",
                    total_lines=1,
                    sections=[SectionRecordModel(anchor="a-top", start=1, end=1)],
                ),
                disposition="tracked",
                reused_anchors=False,
            ),
            "docs/plain.md": Entry(
                file_sha256="b" * 64,
                stats={"/abs/root": StatRecord(size=3, mtime_ns=456)},
                node=None,
                disposition="untracked",
                reused_anchors=False,
            ),
            "docs/id-less.md": Entry(
                file_sha256="c" * 64,
                stats={"/abs/root": StatRecord(size=5, mtime_ns=789)},
                node=None,
                disposition="id-less",
                reused_anchors=False,
            ),
        },
    )


def test_cache_file_round_trips_through_json() -> None:
    original = _sample_cache_file()
    dumped = original.model_dump_json()
    reloaded = CacheFile.model_validate_json(dumped)
    assert reloaded == original
    reloaded_node = reloaded.entries["docs/a.md"].node
    assert reloaded_node is not None
    assert isinstance(reloaded_node.meta, NodeMeta)


def test_stat_record_captures_size_and_mtime() -> None:
    result = stat_record(
        _fake_stat(size=42, mtime_ns=999)  # ty: ignore[invalid-argument-type]
    )
    assert result == StatRecord(size=42, mtime_ns=999)


def test_make_entry_hashes_bytes_resets_stats_and_preserves_node_payload() -> None:
    data = b"raw bytes\n"
    meta = NodeMeta.model_validate({"id": "a"})
    sections = FileSections(
        total_lines=2,
        sections=(SectionRecord(anchor="a-top", start=1, end=2),),
    )

    entry = make_entry(
        data,
        ParsedMeta(meta=meta, disposition="tracked"),
        "# A\nbody\n",
        sections,
        _fake_stat(),  # ty: ignore[invalid-argument-type]
        ROOT,
    )

    assert entry.file_sha256 == sha256(data).hexdigest()
    assert entry.stats == {ROOT: StatRecord(size=10, mtime_ns=123)}
    assert entry.disposition == "tracked"
    assert entry.node == NodePayload(
        meta=meta,
        body="# A\nbody\n",
        total_lines=2,
        sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
    )


@pytest.mark.parametrize("disposition", ["untracked", "id-less"])
def test_make_entry_non_node_stores_none_but_keeps_why(disposition: str) -> None:
    entry = make_entry(
        b"plain",
        ParsedMeta(meta=None, disposition=disposition),  # ty: ignore[invalid-argument-type]
        "plain",
        None,
        _fake_stat(),  # ty: ignore[invalid-argument-type]
        ROOT,
    )
    assert entry.node is None
    assert entry.disposition == disposition


def test_entry_requires_every_replayed_diagnostic_rather_than_defaulting_one() -> None:
    # A default would let an entry written before either field existed decode as an ordinary
    # quiet file, which is the silent drop these fields exist to end. CACHE_VERSION is bumped
    # with them so those entries are discarded instead of reinterpreted.
    with pytest.raises(ValidationError) as exc:
        Entry.model_validate(
            {
                "file_sha256": "b" * 64,
                "stats": {ROOT: {"size": 3, "mtime_ns": 456}},
                "node": None,
            }
        )

    missing = {error["loc"] for error in exc.value.errors() if error["type"] == "missing"}
    assert missing == {("disposition",), ("reused_anchors",)}


def test_entry_round_trips_a_reused_anchor_flag() -> None:
    # The flag is the cached half of the reused-anchor diagnostic. If it did not survive the
    # JSON round trip, a warm run would go quiet about a rebound alias (AD-29).
    entry = Entry(
        file_sha256="d" * 64,
        stats={ROOT: StatRecord(size=7, mtime_ns=1)},
        node=None,
        disposition="untracked",
        reused_anchors=True,
    )

    assert Entry.model_validate_json(entry.model_dump_json()).reused_anchors is True


def test_reconstruct_doc_rebuilds_parsed_doc_at_supplied_path() -> None:
    path = Path("/different/root/docs/a.md")
    meta = NodeMeta.model_validate({"id": "a"})
    entry = Entry(
        file_sha256="a" * 64,
        stats={ROOT: StatRecord(size=10, mtime_ns=123)},
        node=NodePayload(
            meta=meta,
            body="# A\nbody\n",
            total_lines=2,
            sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
        ),
        disposition="tracked",
        reused_anchors=False,
    )

    assert reconstruct_doc(entry, path) == ParsedDoc(
        path=path,
        meta=meta,
        body="# A\nbody\n",
        sections=FileSections(
            total_lines=2,
            sections=(SectionRecord(anchor="a-top", start=1, end=2),),
        ),
    )


def test_reconstruct_doc_non_node_returns_none() -> None:
    entry = Entry(
        file_sha256="b" * 64,
        stats={ROOT: StatRecord(size=3, mtime_ns=456)},
        node=None,
        disposition="untracked",
        reused_anchors=False,
    )
    assert reconstruct_doc(entry, Path("docs/plain.md")) is None


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
@given(_markdown_body())
def test_file_sections_survive_cache_codec_round_trip(body: str) -> None:
    original = derive_file_sections(body)
    entry = make_entry(
        body.encode(),
        ParsedMeta(meta=NodeMeta.model_validate({"id": "x"}), disposition="tracked"),
        body,
        original,
        _fake_stat(size=len(body.encode())),  # ty: ignore[invalid-argument-type]
        ROOT,
    )

    reconstructed = reconstruct_doc(entry, Path("docs/x.md"))

    assert reconstructed is not None
    assert reconstructed.sections == original
    assert reconstructed.body == body
