"""Tests for discovery."""

from pathlib import Path

import pytest

from game_lattice.discovery import discover_doc_paths, read_doc
from game_lattice.error_types import UnreadableDocError


def test_discovers_markdown_sorted(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "b.md").write_text("b", encoding="utf-8")
    (root / "a.md").write_text("a", encoding="utf-8")
    (root / "note.txt").write_text("x", encoding="utf-8")
    found = discover_doc_paths([root], [])
    assert [p.name for p in found] == ["a.md", "b.md"]


def test_ignore_globs_exclude(tmp_path: Path):
    root = tmp_path / "docs"
    (root / "archive").mkdir(parents=True)
    (root / "keep.md").write_text("k", encoding="utf-8")
    (root / "archive" / "old.md").write_text("o", encoding="utf-8")
    found = discover_doc_paths([root], ["**/archive/**"])
    assert [p.name for p in found] == ["keep.md"]


def test_ignore_glob_is_root_anchored(tmp_path: Path):
    # A root-relative glob must not bleed into a same-named nested directory.
    root = tmp_path / "docs"
    (root / "drafts").mkdir(parents=True)
    (root / "chapters" / "drafts").mkdir(parents=True)
    (root / "drafts" / "top.md").write_text("x", encoding="utf-8")
    (root / "chapters" / "drafts" / "deep.md").write_text("y", encoding="utf-8")
    found = {p.relative_to(root).as_posix() for p in discover_doc_paths([root], ["drafts/*.md"])}
    assert "drafts/top.md" not in found  # top-level drafts excluded
    assert "chapters/drafts/deep.md" in found  # nested same-named dir is kept


def test_read_doc_returns_text(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_text("hello", encoding="utf-8")
    assert read_doc(p) == "hello"


def test_read_doc_non_utf8_raises(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(UnreadableDocError):
        read_doc(p)


def test_discovery_skips_symlink_escaping_root(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("secret content", encoding="utf-8")
    (root / "leak.md").symlink_to(secret)
    (root / "keep.md").write_text("safe content", encoding="utf-8")
    with pytest.warns(UserWarning, match="escapes the project root"):
        found = discover_doc_paths([root], [])
    names = [p.name for p in found]
    assert "keep.md" in names
    assert "leak.md" not in names  # skipped, but loudly (not silently)
