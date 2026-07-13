"""Tests for the opt-in incremental load cache."""

import hashlib
import json
import os
import types
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from doc_lattice.cache import (
    CacheFile,
    CacheHit,
    CacheMiss,
    Entry,
    LoadCache,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    cache_path,
)
from doc_lattice.check import check_lattice, statuses_json
from doc_lattice.config import load_config
from doc_lattice.constants import MAX_STAT_ROOTS
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.model import FileSections, NodeMeta, ParsedDoc, SectionRecord
from doc_lattice.orchestrate import load_lattice


def _open(tmp_path: Path, *, trust_stat=False, require_verified=False) -> LoadCache:
    return LoadCache.open(
        cache_key="slot",
        project_root=tmp_path,
        env={"XDG_CACHE_HOME": str(tmp_path / "xdg")},
        trust_stat=trust_stat,
        require_verified=require_verified,
    )


def test_is_empty_tracks_entries_and_loaded_baseline(tmp_path: Path):
    cache = LoadCache(
        path=tmp_path / "load-cache.json",
        current_root="/root",
        trust_stat=False,
        require_verified=False,
        entries={},
        roots=[],
        original=None,
    )
    assert cache.is_empty

    cache._entries["docs/a.md"] = Entry(file_sha256="a" * 64, stats={}, node=None)
    assert not cache.is_empty

    cache._entries.clear()
    cache._original = {}
    assert not cache.is_empty


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


def test_stat_tier_size_mismatch_falls_through_to_verify_hit(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=True)
    cache._entries["docs/a.md"] = Entry(
        file_sha256=hashlib.sha256(_doc_bytes(text)).hexdigest(),  # correct; verify tier hits
        stats={cache._current_root: StatRecord(size=st.st_size + 1, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_stat_tier_mtime_mismatch_falls_through_to_verify_hit(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=True)
    cache._entries["docs/a.md"] = Entry(
        file_sha256=hashlib.sha256(_doc_bytes(text)).hexdigest(),  # correct; verify tier hits
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns + 1)},
        node=None,
    )
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_stat_tier_no_record_for_current_root_falls_through_to_verify_hit(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path, trust_stat=True)
    cache._entries["docs/a.md"] = Entry(
        file_sha256=hashlib.sha256(_doc_bytes(text)).hexdigest(),  # correct; verify tier hits
        stats={"/some/other/root": StatRecord(size=1, mtime_ns=1)},  # nothing for current_root
        node=None,
    )
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


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


def test_verify_hit_stat_refresh_uses_the_stat_captured_with_the_read(tmp_path: Path, monkeypatch):
    # TOCTOU regression: the stat threaded into _refresh_stat must be the one captured by the
    # same read that produced the hashed bytes, not a fresh path.stat(). We monkeypatch
    # read_doc_bytes_and_stat (as cache.py imports it) to return the real bytes paired with a
    # sentinel stat whose size/mtime differ from the real file's. If lookup or _refresh_stat
    # re-stats the path instead of using the threaded value, the stored StatRecord will show
    # the real file's stat, not the sentinel, and this assertion fails.
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    real_bytes = _doc_bytes(text)
    real_st = doc.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_st.st_size + 1000, st_mtime_ns=real_st.st_mtime_ns + 999_999_999
    )

    import doc_lattice.cache as cache_module  # noqa: PLC0415

    monkeypatch.setattr(cache_module, "read_doc_bytes_and_stat", lambda _p: (real_bytes, sentinel))

    cache = _open(tmp_path)
    node = NodePayload(
        meta=NodeMeta.model_validate({"id": "a"}), body="# A\n", total_lines=1, sections=[]
    )
    cache._entries["docs/a.md"] = _entry_for(text, cache._current_root, node=node)
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    stored = cache._entries["docs/a.md"].stats[cache._current_root]
    assert stored == StatRecord(size=sentinel.st_size, mtime_ns=sentinel.st_mtime_ns)


def test_miss_carries_and_records_the_stat_captured_with_the_read(tmp_path: Path, monkeypatch):
    # Same TOCTOU concern on the miss path: CacheMiss.stat and record_miss must both use the
    # stat captured with the read, not a fresh path.stat() taken later.
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    real_bytes = _doc_bytes(text)
    real_st = doc.stat()
    sentinel = types.SimpleNamespace(
        st_size=real_st.st_size + 2000, st_mtime_ns=real_st.st_mtime_ns + 888_888_888
    )

    import doc_lattice.cache as cache_module  # noqa: PLC0415

    monkeypatch.setattr(cache_module, "read_doc_bytes_and_stat", lambda _p: (real_bytes, sentinel))

    cache = _open(tmp_path)
    result = cache.lookup("docs/a.md", doc)  # no matching entry: a miss
    assert isinstance(result, CacheMiss)
    assert result.stat is sentinel
    cache.record_miss(
        "docs/a.md",
        result.data,
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
        result.stat,
    )
    stored = cache._entries["docs/a.md"].stats[cache._current_root]
    assert stored == StatRecord(size=sentinel.st_size, mtime_ns=sentinel.st_mtime_ns)


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
        _doc_bytes(text),
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
        doc.stat(),
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
    cache.record_miss("docs/plain.md", _doc_bytes(text), None, text, None, doc.stat())
    assert cache._entries["docs/plain.md"].node is None


def _load_written(tmp_path: Path) -> CacheFile:
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    return CacheFile.model_validate_json(path.read_text(encoding="utf-8"))


def test_finalize_writes_current_root_at_ledger_tail(tmp_path: Path):
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    cache = _open(tmp_path)
    cache.record_miss(
        "docs/a.md",
        b"---\nid: a\n---\n# A\n",
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
        doc.stat(),
    )
    cache.finalize({"docs/a.md"})
    written = _load_written(tmp_path)
    assert written.roots[-1] == str(tmp_path.resolve())
    assert "docs/a.md" in written.entries


def test_fully_warm_same_root_run_writes_nothing(tmp_path: Path):
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    # Prime the cache with a real run.
    first = _open(tmp_path)
    first.record_miss(
        "docs/a.md",
        text.encode(),
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
        doc.stat(),
    )
    first.finalize({"docs/a.md"})
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns
    # A second warm run from the same root: verify-tier hit, no changes, no write.
    second = _open(tmp_path)
    assert isinstance(second.lookup("docs/a.md", doc), CacheHit)
    second.finalize({"docs/a.md"})
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime_before


def test_presence_reclamation_drops_entry_no_root_claims(tmp_path: Path):
    cache = _open(tmp_path)
    cache._entries["docs/old.md"] = Entry(
        file_sha256="a" * 64,
        stats={cache._current_root: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    cache.finalize(set())  # nothing discovered this run
    written = _load_written(tmp_path)
    assert "docs/old.md" not in written.entries


def test_presence_reclamation_keeps_entry_a_second_root_claims(tmp_path: Path):
    cache = _open(tmp_path)
    other_root = "/some/other/root"
    cache._roots.append(other_root)
    cache._entries["docs/shared.md"] = Entry(
        file_sha256="a" * 64,
        stats={
            cache._current_root: StatRecord(size=1, mtime_ns=1),
            other_root: StatRecord(size=1, mtime_ns=1),
        },
        node=None,
    )
    cache.finalize(set())  # this root did not discover it, but the other root still claims it
    written = _load_written(tmp_path)
    assert "docs/shared.md" in written.entries
    assert cache._current_root not in written.entries["docs/shared.md"].stats
    assert other_root in written.entries["docs/shared.md"].stats


def test_ledger_evicts_over_cap_head_roots_and_scrubs_their_stats(tmp_path: Path):
    cache = _open(tmp_path)
    # Fill the ledger with MAX_STAT_ROOTS old roots plus an entry they all claim.
    old_roots = [f"/root/{i}" for i in range(MAX_STAT_ROOTS)]
    cache._roots.extend(old_roots)
    cache._entries["docs/x.md"] = Entry(
        file_sha256="a" * 64,
        stats={r: StatRecord(size=1, mtime_ns=1) for r in old_roots}
        | {cache._current_root: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    cache.finalize({"docs/x.md"})
    written = _load_written(tmp_path)
    assert len(written.roots) == MAX_STAT_ROOTS
    assert old_roots[0] not in written.roots  # head evicted
    assert old_roots[0] not in written.entries["docs/x.md"].stats  # its stat scrubbed


def _run_check(project) -> str:
    return json.dumps(statuses_json(check_lattice(load_lattice(project))))


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    edits=st.lists(
        st.sampled_from(["body", "frontmatter", "add", "delete", "rename", "touch"]),
        min_size=1,
        max_size=8,
    )
)
def test_default_tier_matches_uncached_under_random_edits(tmp_path_factory, edits):
    base = tmp_path_factory.mktemp("proj")
    xdg = tmp_path_factory.mktemp("xdg")
    docs = base / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A {#a}\nbody a\n", encoding="utf-8")
    (docs / "b.md").write_text(
        "---\nid: b\nderives_from:\n  - ref: a#a\n---\n# B\nbody b\n", encoding="utf-8"
    )
    cached_cfg = base / ".doc-lattice.yml"
    for counter, edit in enumerate(edits):
        target = docs / "a.md"
        if edit == "body" and target.exists():
            target.write_text(target.read_text() + f"\nmore {counter}\n", encoding="utf-8")
        elif edit == "frontmatter" and target.exists():
            body = target.read_text().split("---\n", 2)[-1]
            target.write_text(f"---\nid: a\ntitle: t{counter}\n---\n{body}", encoding="utf-8")
        elif edit == "add":
            (docs / f"extra{counter}.md").write_text(
                f"---\nid: extra{counter}\n---\n# E\n", encoding="utf-8"
            )
        elif edit == "delete":
            extras = sorted(docs.glob("extra*.md"))
            if extras:
                extras[0].unlink()
        elif edit == "rename":
            extras = sorted(docs.glob("extra*.md"))
            if extras:
                extras[0].rename(docs / f"renamed{counter}.md")
        elif edit == "touch" and target.exists():
            target.touch()

        # Uncached reference (no cache_key).
        cached_cfg.unlink(missing_ok=True)
        reference = _run_check(load_config(None, base))
        # Default-tier cached run (verify tier), sharing one XDG home across iterations.
        cached_cfg.write_text("cache_key: prop\n", encoding="utf-8")
        os.environ["XDG_CACHE_HOME"] = str(xdg)
        try:
            cached_result = _run_check(load_config(None, base))
        finally:
            os.environ.pop("XDG_CACHE_HOME", None)
        assert cached_result == reference


def test_require_verified_load_sees_fresh_content_after_same_stat_rewrite(tmp_path, monkeypatch):
    # Even under trust_stat, a require_verified load must read fresh bytes, so reconcile never
    # plans from stale content. Simulated by a rewrite that keeps size and mtime_ns identical.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "a.md"
    doc.write_text("---\nid: a\n---\n# A\naaaa\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        "cache_key: rv\ncache_trust_stat: true\n", encoding="utf-8"
    )
    load_lattice(load_config(None, tmp_path))  # warm the cache, populating the stat hint
    st = doc.stat()
    # Rewrite with identical byte length, then restore the exact mtime_ns.
    doc.write_text("---\nid: a\n---\n# A\nbbbb\n", encoding="utf-8")
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns))
    # Negative control: a plain warm load trusts the stat tier (same size, same mtime_ns) and
    # so serves the STALE cached body, hiding the rewrite. This is the caveat require_verified
    # exists to defeat; without it, reconcile could plan a seen-hash from stale content.
    stale = load_lattice(load_config(None, tmp_path))
    assert "aaaa" in stale.nodes_by_id["a"].body
    assert "bbbb" not in stale.nodes_by_id["a"].body
    # require_verified disables the stat tier, forcing a content re-read: fresh bytes.
    verified = load_lattice(load_config(None, tmp_path), require_verified=True)
    assert "bbbb" in verified.nodes_by_id["a"].body


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file read permissions")
def test_trust_stat_serves_unreadable_file_from_cache_a_documented_caveat(tmp_path, monkeypatch):
    # Documented (spec section 1/5): under trust_stat a file made unreadable without changing its
    # size or mtime_ns is served from cache, where the default (verify) tier re-reads and so
    # raises the same UnreadableDocError an uncached run would. Pins both halves of that caveat.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "a.md"
    doc.write_text("---\nid: a\n---\n# A\naaaa\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        "cache_key: unread\ncache_trust_stat: true\n", encoding="utf-8"
    )
    load_lattice(load_config(None, tmp_path))  # warm the cache, populating the stat hint
    st = doc.stat()
    doc.chmod(0o000)  # unreadable; chmod bumps only ctime, so size and mtime_ns are unchanged
    try:
        assert doc.stat().st_mtime_ns == st.st_mtime_ns  # precondition: mtime really unchanged
        # trust_stat serves the cached node without opening the file: no error.
        served = load_lattice(load_config(None, tmp_path))
        assert "aaaa" in served.nodes_by_id["a"].body
        # The verify tier re-reads and surfaces the read failure, matching an uncached run.
        with pytest.raises(UnreadableDocError):
            load_lattice(load_config(None, tmp_path), require_verified=True)
    finally:
        doc.chmod(0o644)


def test_verify_tier_serves_schema_valid_node_corruption_a_documented_limit(tmp_path, monkeypatch):
    # Documented (spec section 1/7): the verify tier proves the file bytes match file_sha256 but
    # cannot re-confirm the stored node without re-parsing. A hand-edited, still-schema-valid node
    # whose file_sha256 still matches the real file is therefore served even in the default tier.
    # This pins the integrity boundary: the cache is a trusted single-writer artifact.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "a.md"
    doc.write_text("---\nid: a\n---\n# A\nreal body\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: corrupt\n", encoding="utf-8")
    load_lattice(load_config(None, tmp_path))  # warm the cache
    # Tamper with the stored body while leaving file_sha256 (and the on-disk file) intact.
    path = cache_path("corrupt", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    loaded = CacheFile.model_validate_json(path.read_text(encoding="utf-8"))
    entry = loaded.entries["docs/a.md"]
    assert entry.node is not None
    entry.node.body = "# A\nTAMPERED body\n"
    path.write_text(loaded.model_dump_json(), encoding="utf-8")
    # The default (verify) tier serves the tampered node: hash matches, node is trusted as-is.
    served = load_lattice(load_config(None, tmp_path))
    assert "TAMPERED" in served.nodes_by_id["a"].body
