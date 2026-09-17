"""Tests for per-file cache tier selection."""

import types
from pathlib import Path

import pytest

import doc_lattice.cache.lookup as lookup_module
from doc_lattice import orchestrate
from doc_lattice.cache.lookup import CacheHit, CacheMiss, LookupPolicy, resolve
from doc_lattice.cache.schema import Entry, StatRecord, make_entry, stat_record
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.model import FileSections, SectionRecord
from doc_lattice.orchestrate import _parse_file_facts

ROOT = "/abs/current-root"
VERIFY = LookupPolicy(current_root=ROOT, trust_stat=False)
TRUSTING = LookupPolicy(current_root=ROOT, trust_stat=True)
NODE_TEXT = "---\nid: a\n---\n# A {#a-top}\nbody\n"


def _entry_for(text: str, stats: dict[str, StatRecord] | None = None) -> Entry:
    data = text.encode("utf-8")
    entry = make_entry(
        data,
        _parse_file_facts(text, Path("docs/a.md")),
        types.SimpleNamespace(st_size=len(data), st_mtime_ns=0),  # ty: ignore[invalid-argument-type]
        ROOT,
    )
    if stats is not None:
        entry.stats = stats
    return entry


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


def test_verify_hit_reconstructs_facts_and_carries_refreshed_stat(tmp_path: Path) -> None:
    path = _write(tmp_path, NODE_TEXT)
    result = resolve(_entry_for(NODE_TEXT), path, VERIFY)

    assert isinstance(result, CacheHit)
    assert result.facts.parsed.meta is not None
    assert result.facts.parsed.meta.id == "a"
    assert result.facts.body_first_line == 4
    assert result.facts.sections == FileSections(
        total_lines=2,
        sections=(SectionRecord(anchor="a-top", start=1, end=2),),
    )
    assert result.refreshed_stat == stat_record(path.stat())


@pytest.mark.parametrize("policy", [VERIFY, TRUSTING], ids=["verify", "stat"])
@pytest.mark.parametrize(
    "case",
    [
        ("", "untracked", 1),
        ("# Plain\n", "untracked", 1),
        ("---\n---\n# Body\n", "untracked", 3),
        ("---\nscalar\n---\n# Body\n", "untracked", 4),
        ("---\n- item\n---\n# Body\n", "untracked", 4),
        ("---\nname: foreign\n---\n# Body\n", "id-less", 4),
        ("---\nname: foreign\n---\n", "id-less", 4),
        ("# Intro\n<!-- doc-lattice\nid: late\n-->\n", "misplaced-envelope", 1),
        (
            "---\nname: foreign\n---\n<!-- doc-lattice\nid: late\n-->\n",
            "misplaced-envelope",
            4,
        ),
        ("---\nid: a\n---\n<!-- doc-lattice\nid: other\n-->\n", "tracked", 4),
        ("---\nid: a\ntitle: &t A\nlayer: &t design\n---\n# A\n", "tracked", 6),
    ],
)
def test_both_tiers_return_complete_cold_facts(tmp_path, monkeypatch, policy, case):
    text, disposition, first_line = case
    path = _write(tmp_path, text)
    cold = _parse_file_facts(text, path)
    entry = _entry_for(text, {ROOT: stat_record(path.stat())})
    persisted = Entry.model_validate_json(entry.model_dump_json())

    def forbidden(*_args, **_kwargs):
        pytest.fail("cache hits must not parse or derive sections")

    monkeypatch.setattr(orchestrate, "parse_document", forbidden)
    monkeypatch.setattr(orchestrate, "derive_file_sections", forbidden)
    result = resolve(persisted, path, policy)

    assert isinstance(result, CacheHit)
    assert result.facts == cold
    assert result.facts.parsed.disposition == disposition
    assert result.facts.body_first_line == first_line
    assert result.refreshed_stat == (None if policy.trust_stat else stat_record(path.stat()))


def test_changed_content_is_a_miss_carrying_current_bytes(tmp_path: Path) -> None:
    path = _write(tmp_path, "changed\n")
    result = resolve(_entry_for("original\n"), path, VERIFY)
    assert isinstance(result, CacheMiss)
    assert result.data == b"changed\n"


def test_trusting_stat_hit_skips_read_and_hash(tmp_path: Path, monkeypatch) -> None:
    text = "---\nname: foreign\n---\n# A\n"
    path = _write(tmp_path, text)
    entry = _entry_for(text, {ROOT: stat_record(path.stat())})
    entry.file_sha256 = "deadbeef" * 8
    cold = _parse_file_facts(text, path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("a stat hit must not read or hash document bytes")

    monkeypatch.setattr(lookup_module, "read_doc_bytes_and_stat", forbidden)
    monkeypatch.setattr(lookup_module.hashlib, "sha256", forbidden)
    assert resolve(entry, path, TRUSTING) == CacheHit(facts=cold)


def test_verify_policy_disables_stat_tier(tmp_path: Path) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    entry = _entry_for(text, {ROOT: stat_record(path.stat())})
    entry.file_sha256 = "deadbeef" * 8
    assert isinstance(resolve(entry, path, VERIFY), CacheMiss)


@pytest.mark.parametrize(("size_delta", "mtime_delta"), [(1, 0), (0, 1)])
def test_trusting_stat_mismatch_falls_to_verify_hit(
    tmp_path: Path, size_delta: int, mtime_delta: int
) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    st = path.stat()
    entry = _entry_for(
        text,
        stats={
            ROOT: StatRecord(size=st.st_size + size_delta, mtime_ns=st.st_mtime_ns + mtime_delta)
        },
    )
    result = resolve(entry, path, TRUSTING)
    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == stat_record(st)


def test_no_current_root_stat_falls_to_verify_hit(tmp_path: Path) -> None:
    text = "# A\n"
    path = _write(tmp_path, text)
    entry = _entry_for(text, stats={"/abs/other": StatRecord(size=1, mtime_ns=1)})
    result = resolve(entry, path, TRUSTING)
    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == stat_record(path.stat())


def test_trusting_current_root_stat_on_missing_path_raises_unreadable(tmp_path: Path) -> None:
    with pytest.raises(UnreadableDocError):
        resolve(_entry_for("gone\n"), tmp_path / "docs" / "a.md", TRUSTING)


def test_verify_hit_uses_stat_captured_with_read(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path, NODE_TEXT)
    real_stat = path.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_stat.st_size + 1000,
        st_mtime_ns=real_stat.st_mtime_ns + 999_999_999,
    )
    monkeypatch.setattr(
        lookup_module, "read_doc_bytes_and_stat", lambda _path: (NODE_TEXT.encode(), sentinel)
    )
    result = resolve(_entry_for(NODE_TEXT), path, VERIFY)
    assert isinstance(result, CacheHit)
    assert result.refreshed_stat == StatRecord(size=sentinel.st_size, mtime_ns=sentinel.st_mtime_ns)


def test_miss_carries_stat_captured_with_read(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path, "new\n")
    real_stat = path.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_stat.st_size + 2000,
        st_mtime_ns=real_stat.st_mtime_ns + 888_888_888,
    )
    monkeypatch.setattr(
        lookup_module, "read_doc_bytes_and_stat", lambda _path: (b"new\n", sentinel)
    )
    result = resolve(None, path, VERIFY)
    assert isinstance(result, CacheMiss)
    assert result.stat is sentinel
