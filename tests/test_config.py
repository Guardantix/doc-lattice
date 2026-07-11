"""Tests for config loading."""

from pathlib import Path

import pytest

import game_lattice.config as config_module
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


def test_load_config_reuses_safe_yaml_loader(monkeypatch, tmp_path: Path):
    original_yaml = config_module._YAML
    calls: list[str] = []

    class TrackingYAML:
        def load(self, text: str):
            calls.append(text)
            return original_yaml.load(text)

    monkeypatch.setattr(config_module, "_YAML", TrackingYAML())
    projects = [tmp_path / "first", tmp_path / "second"]
    for project in projects:
        project.mkdir()
        (project / ".game-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
        load_config(None, project)

    assert calls == ["docs_roots: [docs]\n", "docs_roots: [docs]\n"]


def test_explicit_config_path_loads_and_resolves_roots(tmp_path: Path):
    (tmp_path / "design").mkdir()
    cfg = tmp_path / "custom.yml"
    cfg.write_text("docs_roots: [design]\n", encoding="utf-8")
    project = load_config(cfg, tmp_path)
    assert project.project_root == tmp_path.resolve()
    assert project.resolved_roots == (tmp_path.resolve() / "design",)


def test_explicit_config_in_subdir_anchors_root_at_its_parent(tmp_path: Path):
    # An explicit --config in a subdir anchors project_root (and docs_roots) at that subdir,
    # not at cwd: project_root = source.resolve().parent.
    sub = tmp_path / "sub"
    (sub / "design").mkdir(parents=True)
    cfg = sub / "custom.yml"
    cfg.write_text("docs_roots: [design]\n", encoding="utf-8")
    project = load_config(cfg, tmp_path)
    assert project.project_root == sub.resolve()
    assert project.resolved_roots == (sub.resolve() / "design",)


def test_empty_config_file_falls_back_to_defaults(tmp_path: Path):
    # A present-but-empty (comment-only) file yields None from the parser; the None -> {}
    # coalescing means Config falls back to defaults instead of raising.
    (tmp_path / ".game-lattice.yml").write_text("# only a comment\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["docs"]
    assert project.resolved_roots == (tmp_path.resolve() / "docs",)


def test_multiple_roots_resolved_in_order(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: [c, a, b]\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.resolved_roots == (
        tmp_path.resolve() / "c",
        tmp_path.resolve() / "a",
        tmp_path.resolve() / "b",
    )


def test_empty_docs_roots_yields_no_resolved_roots(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: []\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.resolved_roots == ()


def test_optional_fields_default_to_none(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.linear_team is None
    assert project.config.binding_layers is None


def test_binding_layers_roundtrips(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text(
        "binding_layers: [binding, derived]\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.binding_layers == ["binding", "derived"]


def test_root_escaping_project_is_rejected(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['../outside']\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert "resolves outside the project root" in str(exc.value)


def test_absolute_outside_root_is_rejected(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: ['/etc']\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert "resolves outside the project root" in str(exc.value)


def test_explicit_config_subdir_rejects_root_escaping_its_parent(tmp_path: Path):
    # With project_root anchored at the config's subdir, a '../design' root now escapes the
    # tightened boundary even though it stays inside cwd.
    sub = tmp_path / "sub"
    sub.mkdir()
    cfg = sub / "custom.yml"
    cfg.write_text("docs_roots: ['../design']\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg, tmp_path)
    assert "resolves outside the project root" in str(exc.value)


def test_symlinked_root_escaping_project_is_rejected(tmp_path: Path):
    # In-project symlink that points outside the project must be rejected (3rd escape vector).
    outside = tmp_path / "outside-target"
    outside.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "design").symlink_to(outside, target_is_directory=True)
    (project / ".game-lattice.yml").write_text("docs_roots: [design]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, project)
    assert exc.value.code == "CONFIG_ERROR"
    assert "resolves outside the project root" in str(exc.value)


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert "invalid config" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "docs_roots: design\n",  # str, not list[str]
        "docs_roots: [1, 2]\n",  # ints, not str
        "ignore_globs: '**/x/**'\n",  # str, not list
        "linear_team: 123\n",  # int, not str | None
    ],
)
def test_strict_config_rejects_wrong_types(tmp_path: Path, body: str):
    # strict=True forbids pydantic from coercing wrong types; each case must surface as ConfigError.
    (tmp_path / ".game-lattice.yml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert "invalid config" in str(exc.value)


def test_missing_explicit_config_path_raises(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.yml", tmp_path)
    assert "config file not found" in str(exc.value)


def test_non_utf8_config_raises_config_error(tmp_path: Path):
    # A non-UTF-8 file trips the read arm of _read_yaml (UnicodeDecodeError), not the parse arm.
    (tmp_path / ".game-lattice.yml").write_bytes(b"\xff\xfe docs_roots: [docs]")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert exc.value.code == "CONFIG_ERROR"
    assert "cannot read config" in str(exc.value)


def test_config_path_is_a_directory_raises_config_error(tmp_path: Path):
    # An explicit --config that exists() but is a directory raises IsADirectoryError (an OSError)
    # in the read arm, surfacing as a clean ConfigError.
    cfg_dir = tmp_path / "as-dir.yml"
    cfg_dir.mkdir()
    with pytest.raises(ConfigError) as exc:
        load_config(cfg_dir, tmp_path)
    assert exc.value.code == "CONFIG_ERROR"
    assert "cannot read config" in str(exc.value)


def test_malformed_config_yaml_raises_config_error(tmp_path: Path):
    # A syntactically broken config surfaces as a clean ConfigError, not a raw YAMLError.
    (tmp_path / ".game-lattice.yml").write_text("docs_roots: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert exc.value.code == "CONFIG_ERROR"
    assert "cannot parse config" in str(exc.value)


def test_safe_yaml_loader_recovers_after_malformed_config(tmp_path: Path):
    config_path = tmp_path / ".game-lattice.yml"
    config_path.write_text("docs_roots: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)

    config_path.write_text("docs_roots: [docs]\n", encoding="utf-8")

    project = load_config(None, tmp_path)

    assert project.config.docs_roots == ["docs"]


def test_safe_yaml_loader_resets_version_between_config_files(tmp_path: Path):
    first_config = tmp_path / "first.yml"
    first_config.write_text("%YAML 1.1\n---\ndocs_roots: [docs]\n", encoding="utf-8")
    second_config = tmp_path / "second.yml"
    second_config.write_text("docs_roots: [on]\n", encoding="utf-8")

    first_project = load_config(first_config, tmp_path)
    second_project = load_config(second_config, tmp_path)

    assert first_project.config.docs_roots == ["docs"]
    assert second_project.config.docs_roots == ["on"]


@pytest.mark.parametrize("key", ["docs", "my-project.docs_v2", "A", "x" * 64])
def test_cache_key_accepts_safe_segments(tmp_path: Path, key: str):
    (tmp_path / ".game-lattice.yml").write_text(f"cache_key: {key}\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.config.cache_key == key


@pytest.mark.parametrize(
    "key",
    ["", ".hidden", "..", "a/b", "with space", "sub/dir", "x" * 65, "-leading", "_leading"],
)
def test_cache_key_rejects_unsafe_segments(tmp_path: Path, key: str):
    (tmp_path / ".game-lattice.yml").write_text(f'cache_key: "{key}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_cache_key_absent_defaults_to_none(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.cache_key is None
    assert project.config.cache_trust_stat is False


def test_trust_stat_without_cache_key_is_config_error(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text("cache_trust_stat: true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_trust_stat_with_cache_key_is_accepted(tmp_path: Path):
    (tmp_path / ".game-lattice.yml").write_text(
        "cache_key: docs\ncache_trust_stat: true\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.cache_key == "docs"
    assert project.config.cache_trust_stat is True
