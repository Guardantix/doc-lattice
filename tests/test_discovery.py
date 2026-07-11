"""Tests for discovery."""

from pathlib import Path

import pytest

from doc_lattice.discovery import (
    decode_doc,
    discover_doc_paths,
    read_doc,
    read_doc_bytes,
    read_doc_bytes_and_stat,
)
from doc_lattice.error_types import UnreadableDocError


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


def test_discovers_skips_nonexistent_root(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("a", encoding="utf-8")
    missing = tmp_path / "does-not-exist"
    # a missing root is tolerated, not fatal; the real root still resolves
    found = discover_doc_paths([missing, root], [])
    assert [p.name for p in found] == ["a.md"]


def test_discovers_dedups_overlapping_roots(tmp_path: Path):
    root = tmp_path / "docs"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "x.md").write_text("x", encoding="utf-8")
    # sub is reachable both as its own root and under root
    found = discover_doc_paths([root, sub], [])
    assert [p.name for p in found] == ["x.md"]  # discovered once, not twice


def test_discovers_sorted_across_roots(tmp_path: Path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "z.md").write_text("z", encoding="utf-8")
    (root_b / "m.md").write_text("m", encoding="utf-8")
    # Pass roots opposite to the sorted output so a per-root concatenation
    # (root_b then root_a) would differ from a globally sorted result.
    found = discover_doc_paths([root_b, root_a], [])
    assert found == sorted(found)
    assert [p.name for p in found] == ["z.md", "m.md"]  # a/z.md sorts before b/m.md


def test_discovery_skips_directory_named_md(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "real.md").write_text("r", encoding="utf-8")
    (root / "weird.md").mkdir()  # a directory whose name matches *.md
    found = discover_doc_paths([root], [])
    assert [p.name for p in found] == ["real.md"]


def test_read_doc_returns_text(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_text("hello", encoding="utf-8")
    assert read_doc(p) == "hello"


def test_read_doc_non_utf8_raises(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(UnreadableDocError):
        read_doc(p)


def test_read_doc_missing_file_raises(tmp_path: Path):
    p = tmp_path / "nope.md"  # never created -> FileNotFoundError (an OSError)
    with pytest.raises(UnreadableDocError) as exc:
        read_doc(p)
    assert exc.value.code == "UNREADABLE_DOC"


def test_read_doc_bytes_returns_raw_bytes(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_bytes(b"# hi\n")
    assert read_doc_bytes(doc) == b"# hi\n"


def test_read_doc_bytes_missing_file_raises_unreadable(tmp_path: Path):
    missing = tmp_path / "gone.md"
    with pytest.raises(UnreadableDocError) as exc:
        read_doc_bytes(missing)
    assert exc.value.code == "UNREADABLE_DOC"
    assert "cannot read doc" in str(exc.value)


def test_read_doc_bytes_and_stat_returns_bytes_and_matching_stat(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_bytes(b"# hi\n")
    data, st = read_doc_bytes_and_stat(doc)
    assert data == b"# hi\n"
    assert st.st_size == len(data)


def test_read_doc_bytes_and_stat_missing_file_raises_unreadable(tmp_path: Path):
    missing = tmp_path / "gone.md"
    with pytest.raises(UnreadableDocError) as via_stat:
        read_doc_bytes_and_stat(missing)
    with pytest.raises(UnreadableDocError) as via_read:
        read_doc_bytes(missing)
    assert str(via_stat.value) == str(via_read.value)


def test_decode_doc_rejects_non_utf8_with_same_message_as_read_doc(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_bytes(b"\xff\xfe not utf-8\n")
    with pytest.raises(UnreadableDocError) as via_decode:
        decode_doc(doc, doc.read_bytes())
    with pytest.raises(UnreadableDocError) as via_read:
        read_doc(doc)
    assert str(via_decode.value) == str(via_read.value)


def test_read_doc_composes_helpers(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_text("# hi\n", encoding="utf-8")
    assert read_doc(doc) == decode_doc(doc, read_doc_bytes(doc))


def test_decode_doc_translates_universal_newlines_like_read_text(tmp_path: Path):
    # Lone-CR (classic Mac) and CRLF endings must collapse to LF exactly as the historical
    # Path.read_text(encoding="utf-8") loader did, so cached and uncached loads stay byte-parity.
    doc = tmp_path / "cr.md"
    raw = b"---\rid: x\r---\rbody\r\nmore\r\n"
    doc.write_bytes(raw)
    assert decode_doc(doc, raw) == doc.read_text(encoding="utf-8")
    assert decode_doc(doc, raw) == "---\nid: x\n---\nbody\nmore\n"


def test_decode_doc_lone_cr_frontmatter_is_still_parsed(tmp_path: Path):
    # Regression: a lone-CR document with valid frontmatter must not be dropped. Without newline
    # translation, split_frontmatter sees one line and never matches the opening fence.
    from doc_lattice.frontmatter_parser import parse_meta, split_frontmatter  # noqa: PLC0415

    doc = tmp_path / "cr.md"
    raw = b"---\rid: cr-node\r---\r# Body\r"
    text = decode_doc(doc, raw)
    raw_meta, _body = split_frontmatter(text)
    meta = parse_meta(raw_meta, doc)
    assert meta is not None
    assert meta.id == "cr-node"


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
