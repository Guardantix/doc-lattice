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
    assert str(result).startswith(str(tmp_path))


def test_safe_resolve_escapes_root(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        safe_resolve("../../etc/passwd", root=tmp_path)
