"""Tests for the init scaffold generators."""

import pytest
from ruamel.yaml import YAML

from game_lattice.config import Config
from game_lattice.scaffold import (
    GAME_LATTICE_REPO_URL,
    build_scaffold,
    render_config,
)


def _load(text: str) -> Config:
    parsed = YAML(typ="safe").load(text)
    return Config.model_validate(parsed)


def test_render_config_default_has_docs_active_and_keys_commented():
    text = render_config(("docs",), None)
    assert "docs_roots:" in text
    assert "- docs" in text
    assert "# ignore_globs:" in text
    assert "# linear_team: ENG" in text
    assert "# binding_layers: null" in text
    cfg = _load(text)
    assert cfg.docs_roots == ["docs"]
    assert cfg.linear_team is None


def test_render_config_lists_multiple_roots():
    text = render_config(("design", "lore"), None)
    assert _load(text).docs_roots == ["design", "lore"]


def test_render_config_bakes_linear_team_and_drops_comment():
    text = render_config(("docs",), "PC")
    assert "linear_team: PC" in text
    assert "# linear_team: ENG" not in text
    assert _load(text).linear_team == "PC"


@pytest.mark.parametrize("value", ["1.0", "#hash", "a: b", "*anchor", "true", "0755"])
def test_render_config_quotes_hostile_linear_team(value):
    cfg = _load(render_config(("docs",), value))
    assert cfg.linear_team == value


@pytest.mark.parametrize("root", ["1.0", "#hash", "weird:name"])
def test_render_config_quotes_hostile_docs_root(root):
    cfg = _load(render_config((root,), None))
    assert cfg.docs_roots == [root]


def test_snippets_pin_rev_url_and_python():
    s = build_scaffold(("docs",), None, "v0.2.0")
    for text in (s.precommit_text, s.ci_text):
        assert "@v0.2.0" in text
        assert GAME_LATTICE_REPO_URL in text
        assert "--python 3.14" in text
    assert "repo: local" in s.precommit_text
    assert "pass_filenames: false" in s.precommit_text
    assert "actions/checkout@v4" in s.ci_text
    assert "astral-sh/setup-uv@v6" in s.ci_text
    assert "linear" not in s.ci_text  # only check runs in the generated CI
