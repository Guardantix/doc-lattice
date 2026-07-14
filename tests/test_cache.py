"""End-to-end cache lifecycle tests through orchestrate and CLI, plus the facade contract."""

import hashlib
import json
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from doc_lattice import __version__
from doc_lattice.cache import CacheFile, Entry, StatRecord, cache_path
from doc_lattice.check import check_lattice, statuses_json
from doc_lattice.config import load_config
from doc_lattice.constants import CACHE_VERSION
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.model import TargetId
from doc_lattice.orchestrate import load_lattice


def test_load_writes_current_root_at_ledger_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: tail\n", encoding="utf-8")

    load_lattice(load_config(None, tmp_path))

    path = cache_path("tail", os.environ)
    written = CacheFile.model_validate_json(path.read_text(encoding="utf-8"))
    assert written.roots[-1] == str(tmp_path.resolve())
    assert "docs/a.md" in written.entries


def test_fully_warm_same_root_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    (doc.parent / "plain.md").write_text("# Plain\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: warm\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    load_lattice(project)
    path = cache_path("warm", os.environ)
    before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns

    load_lattice(project)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime_before


def test_version_1_non_node_cache_cannot_hide_unclosed_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "broken.md"
    doc.write_text("---\nid: vanished\n# Missing close\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: legacy\n", encoding="utf-8")
    root = str(tmp_path.resolve())
    st = doc.stat()
    old_cache = CacheFile(
        version=1,
        tool_version=__version__,
        roots=[root],
        entries={
            "docs/broken.md": Entry(
                file_sha256=hashlib.sha256(doc.read_bytes()).hexdigest(),
                stats={root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
                node=None,
            )
        },
    )
    path = cache_path("legacy", os.environ)
    path.parent.mkdir(parents=True)
    path.write_text(old_cache.model_dump_json(), encoding="utf-8")

    with pytest.raises(UnreadableDocError, match="unclosed YAML frontmatter"):
        load_lattice(load_config(None, tmp_path))
    assert old_cache.version < CACHE_VERSION


def test_version_2_cached_sections_are_rederived(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\nid: a\n---\n``` invalid`info\n# Visible\n```\n",
        encoding="utf-8",
    )
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: old-sections\n", encoding="utf-8")
    project = load_config(None, tmp_path)

    load_lattice(project)
    path = cache_path("old-sections", os.environ)
    stale = CacheFile.model_validate_json(path.read_text(encoding="utf-8"))
    node = stale.entries["docs/a.md"].node
    assert node is not None
    node.sections = []
    stale.version = 2
    path.write_text(stale.model_dump_json(), encoding="utf-8")

    lattice = load_lattice(project)

    assert TargetId("a", "visible") in lattice.index


def test_cache_facade_exports_surviving_names():
    from doc_lattice.cache import (  # noqa: F401, PLC0415
        CacheFile,
        CacheHit,
        CacheMiss,
        Entry,
        LookupPolicy,
        NodePayload,
        RunState,
        SectionRecordModel,
        StatRecord,
        StoreSnapshot,
        cache_home,
        cache_path,
        make_entry,
    )


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
