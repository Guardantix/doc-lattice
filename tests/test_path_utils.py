"""Tests for path utilities."""

import pytest

from game_lattice.path_utils import ensure_dir, normalize_path, safe_resolve


def test_normalize_path_resolves(tmp_path):
    p = normalize_path(tmp_path / "subdir" / ".." / "file.txt")
    assert ".." not in str(p)


def test_ensure_dir_creates(tmp_path):
    target = tmp_path / "a" / "b"
    result = ensure_dir(target)
    assert target.exists()
    assert result == target


def test_safe_resolve_within_root(tmp_path):
    (tmp_path / "file.txt").touch()
    result = safe_resolve(tmp_path / "file.txt", root=tmp_path)
    assert result == (tmp_path / "file.txt").resolve()


def test_safe_resolve_accepts_root_itself(tmp_path):
    assert safe_resolve(tmp_path, root=tmp_path) == tmp_path.resolve()


def test_safe_resolve_accepts_nonexistent_child(tmp_path):
    result = safe_resolve(tmp_path / "nope.txt", root=tmp_path)
    assert result == (tmp_path / "nope.txt").resolve()


def test_safe_resolve_escapes_root(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        safe_resolve("../../etc/passwd", root=tmp_path)


def test_safe_resolve_rejects_absolute_path_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    intruder = outside / "f.txt"
    with pytest.raises(ValueError, match="outside"):
        safe_resolve(intruder, root=root)


def test_safe_resolve_rejects_symlink_escaping_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = root / "leak.txt"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="outside"):
        safe_resolve(link, root=root)


def test_safe_resolve_defaults_root_to_cwd(tmp_path, monkeypatch):
    (tmp_path / "file.txt").touch()
    monkeypatch.chdir(tmp_path)
    assert safe_resolve("file.txt") == (tmp_path / "file.txt").resolve()


def test_safe_resolve_default_root_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        safe_resolve("../escape.txt")
