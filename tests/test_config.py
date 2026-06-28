"""Tests for config loading."""

from pathlib import Path

import pytest

from game_lattice.config import load_config
from game_lattice.error_types import ConfigError


def test_absent_config_uses_defaults(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["docs"]
    assert project.project_root == tmp_path.resolve()
    assert project.resolved_roots == (tmp_path.resolve() / "docs",)


def test_loads_and_resolves_roots(tmp_path: Path):
    (tmp_path / "design").mkdir()
    (tmp_path / ".game-lattice.yml").write_text(
        "docs_roots: [design]\nignore_globs: ['**/x/**']\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.ignore_globs == ["**/x/**"]
    assert project.resolved_roots == (tmp_path.resolve() / "design",)


def test_root_escaping_project_is_rejected(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['../outside']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_absolute_outside_root_is_rejected(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['/etc']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_missing_explicit_config_path_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yml", tmp_path)


def test_explicit_config_path_loads_and_resolves_roots(tmp_path: Path):
    (tmp_path / "design").mkdir()
    cfg = tmp_path / "custom.yml"
    cfg.write_text("docs_roots: [design]\n", encoding="utf-8")
    project = load_config(cfg, tmp_path)
    assert project.project_root == tmp_path.resolve()
    assert project.resolved_roots == (tmp_path.resolve() / "design",)


def test_malformed_config_yaml_raises_config_error(tmp_path: Path):
    # A syntactically broken config surfaces as a clean ConfigError, not a raw YAMLError.
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)
