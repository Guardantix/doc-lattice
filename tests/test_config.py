"""Tests for config loading."""

from pathlib import Path

import pytest

import doc_lattice.config as config_module
from doc_lattice.config import Config, load_config
from doc_lattice.error_types import ConfigError


def test_absent_config_uses_defaults(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["docs"]
    assert project.project_root == tmp_path.resolve()
    assert project.resolved_roots == (tmp_path.resolve() / "docs",)


def test_loads_and_resolves_roots(tmp_path: Path):
    (tmp_path / "design").mkdir()
    (tmp_path / ".doc-lattice.yml").write_text(
        "docs_roots: [design]\nignore_globs: ['**/x/**']\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.ignore_globs == ["**/x/**"]
    assert project.resolved_roots == (tmp_path.resolve() / "design",)


def test_load_config_reuses_safe_yaml_loader(monkeypatch, tmp_path: Path):
    original_loader = config_module._LOADER
    calls: list[str] = []

    class TrackingLoader:
        def load(self, text: str):
            calls.append(text)
            return original_loader.load(text)

    monkeypatch.setattr(config_module, "_LOADER", TrackingLoader())
    projects = [tmp_path / "first", tmp_path / "second"]
    for project in projects:
        project.mkdir()
        (project / ".doc-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
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
    (tmp_path / ".doc-lattice.yml").write_text("# only a comment\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["docs"]
    assert project.resolved_roots == (tmp_path.resolve() / "docs",)


def test_multiple_roots_resolved_in_order(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [c, a, b]\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.resolved_roots == (
        tmp_path.resolve() / "c",
        tmp_path.resolve() / "a",
        tmp_path.resolve() / "b",
    )


def test_empty_docs_roots_yields_no_resolved_roots(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: []\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.resolved_roots == ()


def test_optional_fields_default_to_none(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.linear_team is None


def test_binding_layers_is_rejected_with_the_migration_sentence_and_accepted_keys(tmp_path: Path):
    # `binding_layers` is caught only by the blanket extra="forbid", so the diagnostic is the
    # only place a 1.x config learns the key is gone. The accepted-key list stays alongside it.
    (tmp_path / ".doc-lattice.yml").write_text(
        "binding_layers: [binding, derived]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)

    message = str(exc.value)
    assert "binding_layers has been unsupported since 2.0" in message
    assert "there is no replacement" in message
    assert "accepted keys: cache_key, cache_trust_stat, docs_roots, ignore_globs, linear_team" in (
        message
    )


def test_config_error_names_the_file_and_omits_pydantic_url_and_input(tmp_path: Path):
    # str(ValidationError) leaks a pydantic.dev URL and echoes the rejected input back. Both
    # are noise to a user editing YAML, and the config file the sibling messages name was
    # missing entirely.
    source = tmp_path / ".doc-lattice.yml"
    source.write_text("cache_key: 'a/b'\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)

    message = str(exc.value)
    assert str(source) in message
    assert "pydantic.dev" not in message
    assert "Input should be" not in message
    assert "input_value" not in message


def test_multiple_config_errors_render_one_line_each(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text(
        "bogus: 1\ncache_key: 'a/b'\ndocs_roots: 5\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)

    detail_lines = [line for line in str(exc.value).splitlines()[1:] if line.strip()]
    assert len(detail_lines) == 3
    assert [line.split(":")[0].strip() for line in detail_lines] == [
        "docs_roots",
        "cache_key",
        "bogus",
    ]


def test_unknown_key_lists_the_accepted_keys(tmp_path: Path):
    # The list is derived from Config.model_fields so a new config field cannot leave a stale
    # accepted-key list behind in this diagnostic.
    (tmp_path / ".doc-lattice.yml").write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)

    accepted = ", ".join(sorted(Config.model_fields))
    assert f"accepted keys: {accepted}" in str(exc.value)


def test_model_level_validator_error_renders_a_config_marker_not_a_field(tmp_path: Path):
    # The cache_trust_stat check runs on the whole model, so pydantic reports an empty
    # location. Naming a field there would invent one the user never wrote.
    (tmp_path / ".doc-lattice.yml").write_text("cache_trust_stat: true\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)

    message = str(exc.value)
    assert "  <config>: " in message
    assert "cache_trust_stat requires cache_key to be set" in message


def test_root_escaping_project_is_rejected(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['../outside']\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert "resolves outside the project root" in str(exc.value)


def test_absolute_outside_root_is_rejected(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['/etc']\n", encoding="utf-8")
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
    (project / ".doc-lattice.yml").write_text("docs_roots: [design]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, project)
    assert exc.value.code == "CONFIG_ERROR"
    assert "resolves outside the project root" in str(exc.value)


def test_existing_markdown_file_root_is_accepted(tmp_path: Path):
    # A docs_roots entry may name a single .md file, not only a directory.
    (tmp_path / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [AGENTS.md]\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.resolved_roots == (tmp_path.resolve() / "AGENTS.md",)


def test_existing_non_markdown_file_root_is_rejected(tmp_path: Path):
    # Discovery cannot walk a non-.md file, so accepting it here would silently drop the entry.
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [notes.txt]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert exc.value.code == "CONFIG_ERROR"
    assert "'notes.txt'" in str(exc.value)  # the offending entry is named
    assert "directory" in str(exc.value)


def test_missing_root_entry_is_tolerated(tmp_path: Path):
    # Classification only rejects entries that exist; a missing root stays discovery's problem.
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [gone, gone.md]\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.resolved_roots == (
        tmp_path.resolve() / "gone",
        tmp_path.resolve() / "gone.md",
    )


def test_symlinked_file_root_escaping_project_is_rejected(tmp_path: Path):
    # Containment is checked before classification, so an escaping .md symlink is still rejected.
    outside = tmp_path / "outside-target"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("secret content", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "linked.md").symlink_to(secret)
    (project / ".doc-lattice.yml").write_text("docs_roots: [linked.md]\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, project)
    assert exc.value.code == "CONFIG_ERROR"
    assert "resolves outside the project root" in str(exc.value)


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("bogus: 1\n", encoding="utf-8")
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
    (tmp_path / ".doc-lattice.yml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert "invalid config" in str(exc.value)


def test_missing_explicit_config_path_raises(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.yml", tmp_path)
    assert "config file not found" in str(exc.value)


def test_non_utf8_config_raises_config_error(tmp_path: Path):
    # A non-UTF-8 file trips the read arm of _read_yaml (UnicodeDecodeError), not the parse arm.
    (tmp_path / ".doc-lattice.yml").write_bytes(b"\xff\xfe docs_roots: [docs]")
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
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert exc.value.code == "CONFIG_ERROR"
    assert "cannot parse config" in str(exc.value)


def test_config_with_an_unconstructible_tagged_scalar_raises_config_error(tmp_path: Path):
    # A tagged scalar its type cannot accept fails inside the constructor rather than the
    # parser, so it raises a bare builtin. The config boundary catches that family too, so
    # the same typo reports the file it is in instead of reaching the CLI as a traceback.
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: !!int oops\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(None, tmp_path)
    assert exc.value.code == "CONFIG_ERROR"
    assert "cannot parse config" in str(exc.value)


def test_safe_yaml_loader_recovers_after_malformed_config(tmp_path: Path):
    config_path = tmp_path / ".doc-lattice.yml"
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
    (tmp_path / ".doc-lattice.yml").write_text(f"cache_key: {key}\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.config.cache_key == key


@pytest.mark.parametrize(
    "key",
    ["", ".hidden", "..", "a/b", "with space", "sub/dir", "x" * 65, "-leading", "_leading"],
)
def test_cache_key_rejects_unsafe_segments(tmp_path: Path, key: str):
    (tmp_path / ".doc-lattice.yml").write_text(f'cache_key: "{key}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_cache_key_absent_defaults_to_none(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.cache_key is None
    assert project.config.cache_trust_stat is False


def test_trust_stat_without_cache_key_is_config_error(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("cache_trust_stat: true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_trust_stat_with_cache_key_is_accepted(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text(
        "cache_key: docs\ncache_trust_stat: true\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.cache_key == "docs"
    assert project.config.cache_trust_stat is True
