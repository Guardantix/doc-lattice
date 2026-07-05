"""Tests for the init scaffold generators."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
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


def test_commented_example_keys_stay_valid_against_config_schema():
    # The commented keys document the live schema; uncommenting them must still
    # produce a valid Config (strict + extra='forbid'), or the examples have rotted.
    lines = render_config(("docs",), None).splitlines()
    body = [line for line in lines if "configuration. See" not in line]  # drop header
    cfg = _load("\n".join(re.sub(r"^#\s?", "", line) for line in body))
    assert cfg.ignore_globs == ["**/superpowers/plans/**"]
    assert cfg.linear_team == "ENG"
    assert cfg.binding_layers is None


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


# control chars are rejected by the cli before render_config sees them, so the
# round-trip contract only needs to hold for control-free, non-empty text.
_scalars = st.text(st.characters(blacklist_categories=("Cc", "Cs"))).filter(bool)


@given(team=_scalars)
def test_render_config_round_trips_any_linear_team(team):
    assert _load(render_config(("docs",), team)).linear_team == team


@given(root=_scalars)
def test_render_config_round_trips_any_docs_root(root):
    assert _load(render_config((root,), None)).docs_roots == [root]


def test_snippets_pin_rev_url_and_python():
    scaffold = build_scaffold(("docs",), None, "v0.2.0")
    for text in (scaffold.precommit_text, scaffold.ci_text):
        assert "@v0.2.0" in text
        assert GAME_LATTICE_REPO_URL in text
        assert "--python 3.13" in text
    assert "repo: local" in scaffold.precommit_text
    assert "pass_filenames: false" in scaffold.precommit_text
    assert "actions/checkout@v4" in scaffold.ci_text
    assert "astral-sh/setup-uv@v6" in scaffold.ci_text
    assert "linear" not in scaffold.ci_text  # the network command never runs in the generated CI


def test_invocation_installs_from_pinned_git_ref():
    # uvx only installs from the tag if the full `--from git+<url>@<rev>` shape is
    # intact; asserting url/rev/python as separate fragments misses a dropped `git+`.
    scaffold = build_scaffold(("docs",), None, "v0.2.0")
    for text in (scaffold.precommit_text, scaffold.ci_text):
        assert f"--from git+{GAME_LATTICE_REPO_URL}@v0.2.0 game-lattice check" in text
        assert f"--from git+{GAME_LATTICE_REPO_URL}@v0.2.0 game-lattice lint" in text


def test_generated_gates_run_check_and_lint():
    scaffold = build_scaffold(("docs",), None, "v0.3.0")
    assert "id: game-lattice-check" in scaffold.precommit_text
    assert "id: game-lattice-lint" in scaffold.precommit_text
    assert "game-lattice check" in scaffold.precommit_text
    assert "game-lattice lint" in scaffold.precommit_text
    assert "game-lattice check" in scaffold.ci_text
    assert "game-lattice lint" in scaffold.ci_text


def test_ci_runs_both_commands_in_one_step():
    # A second GitHub Actions run step would be skipped after check exits nonzero,
    # so both commands share one step that captures each exit code and fails if
    # either failed.
    ci = build_scaffold(("docs",), None, "v0.3.0").ci_text
    assert ci.count("- run:") == 1
    assert "rc_check=$?" in ci
    assert "rc_lint=$?" in ci
    assert '[ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]' in ci
