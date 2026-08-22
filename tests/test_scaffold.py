"""Tests for the init scaffold generators."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ruamel.yaml import YAML

import doc_lattice.scaffold as scaffold_module
from doc_lattice.config import Config
from doc_lattice.constants import (
    CHECKOUT_REF,
    CHECKOUT_VERSION,
    PERSISTENCE_TEMP_SUFFIX,
    RECONCILE_AFTER_IMAGE_INFIX,
    RECONCILE_BEFORE_IMAGE_INFIX,
    RECONCILE_JOURNAL_NAME,
    SETUP_UV_REF,
    SETUP_UV_VERSION,
)
from doc_lattice.linear_query import is_valid_team_key
from doc_lattice.scaffold import (
    build_scaffold,
    render_ci,
    render_config,
    render_gitignore,
)


def _load(text: str) -> Config:
    parsed = YAML(typ="safe").load(text)
    return Config.model_validate(parsed)


def test_render_config_includes_commented_cache_examples():
    text = render_config(("docs",), None)
    assert "# cache_key: my-project-docs" in text
    # cache_trust_stat is scaffolded because it is the one option whose fast path trades a read
    # for trust, so a reader should meet it in the file rather than only in the docs. It is
    # scaffolded at true so that uncommenting it opts in, which is what its comment claims.
    assert "# cache_trust_stat: true" in text


def test_render_gitignore_matches_reconcile_transaction_artifacts():
    exact = (
        ".doc-lattice-reconcile.json\n"
        ".doc-lattice-reconcile.json.*.tmp\n"
        ".*.doc-lattice-before.*.tmp\n"
        ".*.doc-lattice-after.*.tmp\n"
    )
    coupled = (
        f"{RECONCILE_JOURNAL_NAME}\n"
        f"{RECONCILE_JOURNAL_NAME}.*{PERSISTENCE_TEMP_SUFFIX}\n"
        f".*{RECONCILE_BEFORE_IMAGE_INFIX}*{PERSISTENCE_TEMP_SUFFIX}\n"
        f".*{RECONCILE_AFTER_IMAGE_INFIX}*{PERSISTENCE_TEMP_SUFFIX}\n"
    )

    assert render_gitignore() == exact == coupled


def test_render_gitignore_derives_patterns_from_shared_naming_constants(monkeypatch):
    monkeypatch.setattr(scaffold_module, "RECONCILE_JOURNAL_NAME", ".renamed-journal")
    monkeypatch.setattr(scaffold_module, "PERSISTENCE_TEMP_SUFFIX", ".stage")
    monkeypatch.setattr(scaffold_module, "RECONCILE_BEFORE_IMAGE_INFIX", ".before-image.")
    monkeypatch.setattr(scaffold_module, "RECONCILE_AFTER_IMAGE_INFIX", ".after-image.")

    assert render_gitignore() == (
        ".renamed-journal\n"
        ".renamed-journal.*.stage\n"
        ".*.before-image.*.stage\n"
        ".*.after-image.*.stage\n"
    )


def test_build_scaffold_includes_exact_gitignore_text():
    scaffold = build_scaffold(("docs",), None, "1.0.0", default_branch="main")

    assert scaffold.gitignore_text == render_gitignore()


def test_render_config_default_has_docs_active_and_keys_commented():
    text = render_config(("docs",), None)
    assert "docs_roots:" in text
    assert "- docs" in text
    assert "# ignore_globs:" in text
    assert "# linear_team: ENG" in text
    assert "binding_layers" not in text
    cfg = _load(text)
    assert cfg.docs_roots == ["docs"]
    assert cfg.linear_team is None


def test_commented_example_keys_stay_valid_against_config_schema():
    # The commented keys document the live schema; uncommenting them must still
    # produce a valid Config (strict + extra='forbid'), or the examples have rotted.
    lines = render_config(("docs",), None).splitlines()
    body = [line for line in lines if "configuration. See" not in line]  # drop header
    cfg = _load("\n".join(re.sub(r"^#\s?", "", line) for line in body))
    # Pin the whole set, not a sample: a newly scaffolded commented key would otherwise be
    # uncommented, loaded, and then silently unexamined by this test.
    assert cfg.model_fields_set == {
        "docs_roots",
        "ignore_globs",
        "cache_key",
        "cache_trust_stat",
        "linear_team",
    }
    assert cfg.ignore_globs == ["**/archive/**"]
    assert cfg.cache_key == "my-project-docs"
    assert cfg.cache_trust_stat is True
    # Config types linear_team as a bare str, so a placeholder example would load cleanly here
    # and only fail downstream in linear_query. Hold the example to the domain that owns it.
    assert cfg.linear_team is not None
    assert is_valid_team_key(cfg.linear_team)


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


def test_snippets_pin_pypi_version_and_python():
    scaffold = build_scaffold(("docs",), None, "0.2.0", default_branch="main")
    for text in (scaffold.precommit_text, scaffold.ci_text):
        assert "--from doc-lattice==0.2.0" in text
        assert "--python 3.13" in text
        assert "git+" not in text
    assert "repo: local" in scaffold.precommit_text
    assert "pass_filenames: false" in scaffold.precommit_text
    assert f"actions/checkout@{CHECKOUT_REF} # {CHECKOUT_VERSION}" in scaffold.ci_text
    assert f"astral-sh/setup-uv@{SETUP_UV_REF} # {SETUP_UV_VERSION}" in scaffold.ci_text
    assert "linear" not in scaffold.ci_text


def test_ci_snippet_pins_actions_by_full_commit_sha_not_a_floating_tag():
    # A floating tag re-resolves on every run, which is exactly what a commit pin exists to
    # avoid; the printed snippet has to match that posture.
    ci = build_scaffold(("docs",), None, "0.2.0", default_branch="main").ci_text

    assert "actions/checkout@v4" not in ci
    assert "astral-sh/setup-uv@v6" not in ci
    for line in ci.splitlines():
        if "uses:" in line:
            action, _, ref = line.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40} # v[0-9]+\.[0-9]+\.[0-9]+", ref), action


def test_ci_snippet_carries_the_documented_least_privilege_posture():
    # The snippet's docstring claims that posture, and the step that follows
    # checkout resolves and executes third-party packages. Without these settings the job's
    # token stays in .git/config and is writable while that happens, and a persistent cache any
    # other workflow on the repository can populate is restored into the gate job.
    ci = build_scaffold(("docs",), None, "0.2.0", default_branch="main").ci_text
    workflow = YAML(typ="safe").load(ci)
    steps = workflow["jobs"]["check"]["steps"]

    assert workflow["permissions"] == {"contents": "read"}
    assert steps[0]["uses"] == f"actions/checkout@{CHECKOUT_REF}"
    assert steps[0]["with"] == {"persist-credentials": False}
    assert steps[1]["uses"] == f"astral-sh/setup-uv@{SETUP_UV_REF}"
    assert steps[1]["with"] == {"enable-cache": False}


def test_invocation_installs_from_exact_pypi_requirement():
    scaffold = build_scaffold(("docs",), None, "0.2.0", default_branch="main")
    for text in (scaffold.precommit_text, scaffold.ci_text):
        assert "--from doc-lattice==0.2.0 doc-lattice check" in text
        assert "--from doc-lattice==0.2.0 doc-lattice lint" in text


def test_generated_gates_run_check_and_lint():
    scaffold = build_scaffold(("docs",), None, "0.3.0", default_branch="main")
    assert "id: doc-lattice-check" in scaffold.precommit_text
    assert "id: doc-lattice-lint" in scaffold.precommit_text
    assert "doc-lattice check" in scaffold.precommit_text
    assert "doc-lattice lint" in scaffold.precommit_text
    assert "doc-lattice check" in scaffold.ci_text
    assert "doc-lattice lint" in scaffold.ci_text


def test_ci_snippet_filters_both_triggers_on_the_requested_branch():
    # A repository on a non-main default branch previously installed a workflow that read as
    # correct, installed cleanly, and never triggered. Both filters have to move together.
    ci = render_ci("0.3.0", default_branch="trunk")
    workflow = YAML(typ="safe").load(ci)

    assert workflow["on"]["push"]["branches"] == ["trunk"]
    assert workflow["on"]["pull_request"]["branches"] == ["trunk"]
    assert "[main]" not in ci


def test_ci_snippet_keeps_the_exact_main_spelling_it_always_emitted():
    # main stays byte-identical so an established adopter diffing the regenerated workflow sees
    # no churn from parameterization alone.
    assert "  push:\n    branches: [main]\n  pull_request:\n    branches: [main]\n" in render_ci(
        "0.3.0", default_branch="main"
    )


@pytest.mark.parametrize("branch", ["release/2.x", "feature/a-b_c", "v1.0"])
def test_ci_snippet_round_trips_slashed_and_dotted_branches(branch):
    workflow = YAML(typ="safe").load(render_ci("0.3.0", default_branch=branch))

    assert workflow["on"]["push"]["branches"] == [branch]


@pytest.mark.parametrize("branch", ["on", "yes", "No", "OFF", "Y", "1.0", "0755"])
def test_ci_snippet_quotes_branches_a_yaml_reader_would_retype(branch):
    # Validation and output escaping are independent invariants. GitHub reads workflows under
    # YAML 1.1 boolean resolution, so an unquoted `on` would arrive as a boolean rather than a
    # branch name; the emitter, not hand-written interpolation, has to decide the quoting.
    text = render_ci("0.3.0", default_branch=branch)
    workflow = YAML(typ="safe").load(text)

    assert workflow["on"]["push"]["branches"] == [branch]
    assert workflow["on"]["pull_request"]["branches"] == [branch]
    # The round-trip above is loaded under YAML 1.2, where `on` and `yes` are already plain
    # strings, so it cannot see the hazard on its own. GitHub resolves them as booleans, so
    # assert on the emitted text that the scalar carries quotes rather than trusting a loader
    # that shares this emitter's YAML version.
    assert f"    branches: [{branch}]\n" not in text


def test_ci_snippet_keeps_a_long_branch_filter_on_one_line():
    # ruamel wraps flow sequences at 80 columns by default, which would split the filter across
    # lines and corrupt the hand-assembled text around it.
    branch = "release/" + "a" * 200
    ci = render_ci("0.3.0", default_branch=branch)

    assert f"    branches: [{branch}]\n" in ci
    assert YAML(typ="safe").load(ci)["on"]["push"]["branches"] == [branch]


def test_build_scaffold_requires_the_branch_as_a_keyword():
    # Required and keyword-only so no future caller can silently restore a hard-wired main.
    with pytest.raises(TypeError):
        build_scaffold(("docs",), None, "0.3.0")  # ty: ignore[missing-argument]


def test_ci_runs_both_commands_in_one_step():
    # A second GitHub Actions run step would be skipped after check exits nonzero,
    # so both commands share one step that captures each exit code and fails if
    # either failed.
    ci = build_scaffold(("docs",), None, "0.3.0", default_branch="main").ci_text
    assert ci.count("- run:") == 1
    assert "rc_check=$?" in ci
    assert "rc_lint=$?" in ci
    assert '[ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ]' in ci
