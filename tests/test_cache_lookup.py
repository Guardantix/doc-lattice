"""Tests for per-file cache tier selection."""

import hashlib
import types
from pathlib import Path

import pytest

import doc_lattice.cache.lookup as lookup_module
from doc_lattice.cache.lookup import CacheHit, CacheMiss, LookupPolicy, resolve
from doc_lattice.cache.schema import (
    Entry,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    stat_record,
)
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.model import FileSections, NodeMeta, ParsedDoc, SectionRecord

ROOT = "/abs/current-root"
VERIFY = LookupPolicy(current_root=ROOT, trust_stat=False)
TRUSTING = LookupPolicy(current_root=ROOT, trust_stat=True)


def _node() -> NodePayload:
    return NodePayload(
        meta=NodeMeta.model_validate({"id": "a"}),
        body="# A {#a-top}\nbody\n",
        total_lines=2,
        sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
    )


def _entry_for(
    text: str,
    node: NodePayload | None,
    stats: dict[str, StatRecord] | None = None,
) -> Entry:
    data = text.encode("utf-8")
    return Entry(
        file_sha256=hashlib.sha256(data).hexdigest(),
        stats=stats if stats is not None else {ROOT: StatRecord(size=len(data), mtime_ns=0)},
        node=node,
    )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "docs" / "a.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_absent_entry_is_a_miss_carrying_current_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path, "new\n")

    result = resolve(None, path, VERIFY)

    assert isinstance(result, CacheMiss)
    assert result.data == b"new\n"


def test_verify_hit_reconstructs_doc_and_carries_refreshed_stat(tmp_path: Path) -> None:
    text = "# A {#a-top}\nbody\n"
    path = _write(tmp_path, text)

    result = resolve(_entry_for(text, _node()), path, VERIFY)

    assert isinstance(result, CacheHit)
    assert isinstance(result.doc, ParsedDoc)
    assert result.doc.meta.id == "a"
    assert result.doc.sections == FileSections(
        total_lines=2,
        sections=(SectionRecord(anchor="a-top", start=1, end=2),),
    )
    assert result.refreshed_stat == stat_record(path.stat())


def test_verify_non_node_hit_returns_none_doc(tmp_path: Path) -> None:
    text = "# plain\n"
    path = _write(tmp_path, text)

    result = resolve(_entry_for(text, None), path, VERIFY)

    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_changed_content_is_a_miss_carrying_current_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path, "changed\n")

    result = resolve(_entry_for("original\n", None), path, VERIFY)

    assert isinstance(result, CacheMiss)
    assert result.data == b"changed\n"


def test_trusting_stat_hit_skips_read_and_hash(tmp_path: Path) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    st = path.stat()
    entry = Entry(
        file_sha256="deadbeef" * 8,
        stats={ROOT: stat_record(st)},
        node=None,
    )

    result = resolve(entry, path, TRUSTING)

    assert result == CacheHit(doc=None, refreshed_stat=None)


def test_verify_policy_disables_stat_tier(tmp_path: Path) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    entry = Entry(
        file_sha256="deadbeef" * 8,
        stats={ROOT: stat_record(path.stat())},
        node=None,
    )

    result = resolve(entry, path, VERIFY)

    assert isinstance(result, CacheMiss)


@pytest.mark.parametrize(("size_delta", "mtime_delta"), [(1, 0), (0, 1)])
def test_trusting_stat_mismatch_falls_to_verify_hit(
    tmp_path: Path,
    size_delta: int,
    mtime_delta: int,
) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    st = path.stat()
    entry = _entry_for(
        text,
        None,
        stats={
            ROOT: StatRecord(
                size=st.st_size + size_delta,
                mtime_ns=st.st_mtime_ns + mtime_delta,
            )
        },
    )

    result = resolve(entry, path, TRUSTING)

    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == stat_record(st)


def test_no_current_root_stat_falls_to_verify_hit(tmp_path: Path) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    entry = _entry_for(
        text,
        None,
        stats={"/abs/other": StatRecord(size=1, mtime_ns=1)},
    )

    result = resolve(entry, path, TRUSTING)

    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == stat_record(path.stat())


def test_trusting_current_root_stat_on_missing_path_raises_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "a.md"
    entry = _entry_for("gone\n", None)

    with pytest.raises(UnreadableDocError):
        resolve(entry, path, TRUSTING)


def test_verify_hit_uses_stat_captured_with_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "# A {#a-top}\nbody\n"
    path = _write(tmp_path, text)
    real_stat = path.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_stat.st_size + 1000,
        st_mtime_ns=real_stat.st_mtime_ns + 999_999_999,
    )
    monkeypatch.setattr(
        lookup_module,
        "read_doc_bytes_and_stat",
        lambda _path: (text.encode("utf-8"), sentinel),
    )

    result = resolve(_entry_for(text, _node()), path, VERIFY)

    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == StatRecord(
        size=sentinel.st_size,
        mtime_ns=sentinel.st_mtime_ns,
    )


def test_miss_carries_stat_captured_with_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path, "new\n")
    real_stat = path.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_stat.st_size + 2000,
        st_mtime_ns=real_stat.st_mtime_ns + 888_888_888,
    )
    monkeypatch.setattr(
        lookup_module,
        "read_doc_bytes_and_stat",
        lambda _path: (b"new\n", sentinel),
    )

    result = resolve(None, path, VERIFY)

    assert isinstance(result, CacheMiss)
    assert result.stat is sentinel
