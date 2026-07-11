"""Tests for the opt-in incremental load cache."""

import hashlib
import json
from pathlib import Path

import pytest

from game_lattice import __version__
from game_lattice.cache import (
    CacheFile,
    CacheHit,
    CacheMiss,
    Entry,
    LoadCache,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    cache_home,
    cache_path,
)
from game_lattice.constants import CACHE_FILE_NAME, CACHE_VERSION
from game_lattice.error_types import UnreadableDocError
from game_lattice.model import FileSections, NodeMeta, ParsedDoc, SectionRecord


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
            ),
            "docs/plain.md": Entry(
                file_sha256="b" * 64,
                stats={"/abs/root": StatRecord(size=3, mtime_ns=456)},
                node=None,
            ),
        },
    )


def test_cache_file_round_trips_through_json():
    original = _sample_cache_file()
    dumped = original.model_dump_json()
    reloaded = CacheFile.model_validate_json(dumped)
    assert reloaded == original
    # The nested NodeMeta reloads as a validated NodeMeta, not a raw dict.
    reloaded_node = reloaded.entries["docs/a.md"].node
    assert reloaded_node is not None
    assert isinstance(reloaded_node.meta, NodeMeta)


def test_cache_home_uses_absolute_xdg():
    home = cache_home({"XDG_CACHE_HOME": "/custom/cache", "HOME": "/home/u"})
    assert home == Path("/custom/cache")


def test_cache_home_ignores_relative_xdg():
    home = cache_home({"XDG_CACHE_HOME": "relative/cache", "HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_home_falls_back_to_home_dot_cache_when_xdg_unset():
    home = cache_home({"HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_path_composes_slot_and_file_name():
    path = cache_path("my-docs", {"XDG_CACHE_HOME": "/c", "HOME": "/home/u"})
    assert path == Path("/c") / "game-lattice" / "my-docs" / CACHE_FILE_NAME


def _write_cache(path: Path, cache_file: CacheFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache_file.model_dump_json(), encoding="utf-8")


def _open(tmp_path: Path, *, trust_stat=False, require_verified=False) -> LoadCache:
    return LoadCache.open(
        cache_key="slot",
        project_root=tmp_path,
        env={"XDG_CACHE_HOME": str(tmp_path / "xdg")},
        trust_stat=trust_stat,
        require_verified=require_verified,
    )


def test_open_missing_file_is_empty(tmp_path: Path):
    cache = _open(tmp_path)
    assert cache.is_empty


def test_open_valid_file_loads_entries(tmp_path: Path):
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    _write_cache(path, _sample_cache_file())
    cache = _open(tmp_path)
    assert not cache.is_empty


@pytest.mark.parametrize(
    "text",
    [
        "",  # truncated / empty
        "{ not json",  # invalid JSON
        '{"version": 1}',  # schema violation (missing fields)
    ],
)
def test_open_corrupt_file_is_empty(tmp_path: Path, text: str):
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert _open(tmp_path).is_empty


def test_open_invalid_utf8_is_empty(tmp_path: Path):
    # A cache file with invalid UTF-8 bytes raises UnicodeDecodeError (not an OSError); it must
    # still yield an empty cache rather than propagate.
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not valid utf-8\n")
    assert _open(tmp_path).is_empty


def test_open_wrong_version_is_empty(tmp_path: Path):
    bad = _sample_cache_file().model_copy(update={"version": 999})
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    _write_cache(path, bad)
    assert _open(tmp_path).is_empty


def test_open_wrong_tool_version_is_empty(tmp_path: Path):
    bad = _sample_cache_file().model_copy(update={"tool_version": "0.0.0-other"})
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    _write_cache(path, bad)
    assert _open(tmp_path).is_empty


def test_open_invalid_meta_is_empty(tmp_path: Path):
    # A structurally valid file whose node.meta violates NodeMeta must discard wholesale.
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _sample_cache_file().model_dump(mode="json")
    payload["entries"]["docs/a.md"]["node"]["meta"]["id"] = "bad#id"  # '#' is rejected by NodeMeta
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _open(tmp_path).is_empty


def _doc_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _entry_for(text: str, root: str, *, node: NodePayload | None) -> Entry:
    return Entry(
        file_sha256=hashlib.sha256(_doc_bytes(text)).hexdigest(),
        stats={root: StatRecord(size=len(_doc_bytes(text)), mtime_ns=0)},
        node=node,
    )


def test_verify_tier_hit_reconstructs_parsed_doc(tmp_path: Path):
    text = "---\nid: a\n---\n# A {#a-top}\nbody\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    node = NodePayload(
        meta=NodeMeta.model_validate({"id": "a"}),
        body="# A {#a-top}\nbody\n",
        total_lines=2,
        sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
    )
    cache._entries["docs/a.md"] = _entry_for(text, cache._current_root, node=node)
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert isinstance(result.doc, ParsedDoc)
    assert result.doc.meta.id == "a"
    assert result.doc.sections == FileSections(
        total_lines=2, sections=(SectionRecord("a-top", 1, 2),)
    )


def test_verify_tier_non_node_hit_returns_none_doc(tmp_path: Path):
    text = "# plain\n"
    doc = tmp_path / "docs" / "plain.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    cache._entries["docs/plain.md"] = _entry_for(text, cache._current_root, node=None)
    result = cache.lookup("docs/plain.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_content_change_is_a_miss_carrying_current_bytes(tmp_path: Path):
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("changed\n", encoding="utf-8")
    cache = _open(tmp_path)
    cache._entries["docs/a.md"] = _entry_for("original\n", cache._current_root, node=None)
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheMiss)
    assert result.data == b"changed\n"


def test_absent_entry_is_a_miss(tmp_path: Path):
    doc = tmp_path / "docs" / "new.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("new\n", encoding="utf-8")
    cache = _open(tmp_path)
    result = cache.lookup("docs/new.md", doc)
    assert isinstance(result, CacheMiss)


def test_stat_tier_hit_skips_reading_the_file(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=True)
    entry = Entry(
        file_sha256="deadbeef" * 8,  # deliberately wrong; stat tier must not hash
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    cache._entries["docs/a.md"] = entry
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_stat_tier_disabled_without_trust_stat_falls_to_verify(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=False)
    cache._entries["docs/a.md"] = Entry(
        file_sha256="deadbeef" * 8,  # wrong hash; verify tier will miss
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheMiss)


def test_require_verified_disables_stat_tier(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=True, require_verified=True)
    cache._entries["docs/a.md"] = Entry(
        file_sha256="deadbeef" * 8,
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    assert isinstance(cache.lookup("docs/a.md", doc), CacheMiss)


def test_lookup_deleted_file_raises_unreadable(tmp_path: Path):
    doc = tmp_path / "docs" / "gone.md"
    cache = _open(tmp_path, trust_stat=True)
    cache._entries["docs/gone.md"] = Entry(
        file_sha256="a" * 64,
        stats={cache._current_root: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    with pytest.raises(UnreadableDocError):
        cache.lookup("docs/gone.md", doc)


def test_record_miss_resets_stats_to_current_root(tmp_path: Path):
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    cache._entries["docs/a.md"] = Entry(
        file_sha256="old" + "0" * 61,
        stats={"/some/other/root": StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    cache.record_miss(
        "docs/a.md",
        doc,
        _doc_bytes(text),
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
    )
    entry = cache._entries["docs/a.md"]
    assert entry.file_sha256 == hashlib.sha256(_doc_bytes(text)).hexdigest()
    assert set(entry.stats) == {cache._current_root}
    assert entry.node is not None
    assert entry.node.meta.id == "a"


def test_record_miss_non_node_stores_null_node(tmp_path: Path):
    text = "# plain\n"
    doc = tmp_path / "docs" / "plain.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    cache.record_miss("docs/plain.md", doc, _doc_bytes(text), None, text, None)
    assert cache._entries["docs/plain.md"].node is None
