"""Tests for load_lattice wiring."""

import os
from pathlib import Path

import pytest

from game_lattice import orchestrate
from game_lattice.cache import cache_path
from game_lattice.config import load_config
from game_lattice.error_types import ConfigError, DuplicateIdError, UnreadableDocError
from game_lattice.model import TargetId
from game_lattice.orchestrate import load_lattice


def test_load_lattice_from_dir(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"art-direction", "pc-design", "gdd"}
    assert lat.index[TargetId("art-direction", "accent")].kind == "section"
    # pc-design derives from accent and motion
    refs = {e.target_id for e in lat.nodes_by_id["pc-design"].derives_from}
    assert refs == {TargetId("art-direction", "accent"), TargetId("art-direction", "motion")}
    # gdd's ghost ref is unresolved
    assert lat.nodes_by_id["gdd"].derives_from[0].target_id is None


def test_files_without_frontmatter_skipped(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plain.md").write_text("# just prose\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    lat = load_lattice(project)
    assert lat.nodes_by_id == {}


def test_duplicate_id_propagates(tmp_path: Path):
    # Two discovered files sharing an id must collide in the shared index through the
    # full discovery -> parse -> build seam, surfacing DuplicateIdError (exit 2).
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: dup\n---\n# A\n", encoding="utf-8")
    (docs / "b.md").write_text("---\nid: dup\n---\n# B\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    with pytest.raises(DuplicateIdError) as exc:
        load_lattice(project)
    assert exc.value.code == "DUPLICATE_ID"


@pytest.mark.parametrize(
    ("text", "exc_type", "code"),
    [
        ("---\nid: x\nlayer: [unterminated\n---\n# X\n", UnreadableDocError, "UNREADABLE_DOC"),
        ("---\nid: x\nbogus_key: 1\n---\n# X\n", ConfigError, "CONFIG_ERROR"),
    ],
)
def test_load_lattice_surfaces_parse_errors(tmp_path: Path, text, exc_type, code):
    # The orchestrate loop has no try/except, so unparseable YAML and forbidden keys
    # must propagate rather than be silently skipped.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text(text, encoding="utf-8")
    project = load_config(None, tmp_path)
    with pytest.raises(exc_type) as exc:
        load_lattice(project)
    assert exc.value.code == code


def test_load_lattice_surfaces_non_utf8(tmp_path: Path):
    # A non-UTF-8 doc must surface UnreadableDocError, not be quietly dropped.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_bytes(b"---\nid: x\n---\n\xff\xfe not utf-8\n")
    project = load_config(None, tmp_path)
    with pytest.raises(UnreadableDocError) as exc:
        load_lattice(project)
    assert exc.value.code == "UNREADABLE_DOC"


def test_ignore_globs_exclude_nodes(tmp_path: Path):
    # orchestrate forwards project.config.ignore_globs into discovery; a configured
    # glob must remove the matching node from the assembled lattice end to end.
    docs = tmp_path / "docs"
    (docs / "drafts").mkdir(parents=True)
    (docs / "kept.md").write_text("---\nid: kept\n---\n# Kept\n", encoding="utf-8")
    (docs / "drafts" / "wip.md").write_text("---\nid: wip\n---\n# WIP\n", encoding="utf-8")
    (tmp_path / ".game-lattice.yml").write_text(
        'docs_roots: ["docs"]\nignore_globs: ["drafts/**"]\n', encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"kept"}


def test_multiple_docs_roots_combine(tmp_path: Path):
    # load_lattice must union docs from multiple configured roots into one node set
    # and shared id namespace.
    (tmp_path / "design").mkdir()
    (tmp_path / "production").mkdir()
    (tmp_path / "design" / "a.md").write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    (tmp_path / "production" / "b.md").write_text("---\nid: b\n---\n# B\n", encoding="utf-8")
    (tmp_path / ".game-lattice.yml").write_text(
        'docs_roots: ["design", "production"]\n', encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"a", "b"}


def _with_cache(tmp_path: Path, *, trust_stat: bool = False) -> Path:
    lines = ["cache_key: testslot"]
    if trust_stat:
        lines.append("cache_trust_stat: true")
    (tmp_path / ".game-lattice.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_cached_and_uncached_loads_are_structurally_equal(lattice_dir: Path, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    uncached = load_lattice(load_config(None, lattice_dir))
    _with_cache(lattice_dir)
    cold = load_lattice(load_config(None, lattice_dir))  # writes the cache
    warm = load_lattice(load_config(None, lattice_dir))  # reads it back
    assert cold == uncached
    assert warm == uncached


def test_cache_disabled_leaves_env_untouched(lattice_dir: Path):
    # With no cache_key, load_lattice must never resolve or write a cache.
    project = load_config(None, lattice_dir)
    assert project.config.cache_key is None
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"art-direction", "pc-design", "gdd"}


def test_cached_cold_run_writes_the_cache_file(lattice_dir: Path, monkeypatch, tmp_path):
    # Proof the cached branch is genuinely taken: a cold run with cache_key set must write the
    # slot's cache file to disk. A no-op alias of the uncached path would leave it absent.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _with_cache(lattice_dir)  # writes cache_key: testslot
    assert not cache_path("testslot", os.environ).exists()
    load_lattice(load_config(None, lattice_dir))
    assert cache_path("testslot", os.environ).exists()


def test_warm_cached_run_reparses_nothing(lattice_dir: Path, monkeypatch, tmp_path):
    # Proof the warm path serves from the cache instead of re-parsing: after a cold run populates
    # the cache, a warm run must call parse_meta zero times (every file is a verify-tier hit
    # reconstructed from the cache). A no-op alias would re-parse every discovered node.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _with_cache(lattice_dir)

    calls = {"n": 0}
    real = orchestrate.parse_meta

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orchestrate, "parse_meta", counting)

    load_lattice(load_config(None, lattice_dir))  # cold: cache empty, every node parsed
    assert calls["n"] > 0

    calls["n"] = 0
    load_lattice(load_config(None, lattice_dir))  # warm: every node served from the cache
    assert calls["n"] == 0
