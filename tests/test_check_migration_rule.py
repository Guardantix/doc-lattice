"""Tests for the migration-subsection guard script."""

import json
from pathlib import Path
from runpy import run_path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = run_path(str(_ROOT / "scripts" / "check_migration_rule.py"))
check_migration_rule = _SCRIPT["check_migration_rule"]
compute_surfaces = _SCRIPT["compute_surfaces"]
normalize_pins = _SCRIPT["normalize_pins"]
render_baseline = _SCRIPT["render_baseline"]
parse_baseline = _SCRIPT["parse_baseline"]
BaseState = _SCRIPT["BaseState"]
CI_BRANCHES = _SCRIPT["CI_BRANCHES"]
SENTINEL_VERSION = _SCRIPT["SENTINEL_VERSION"]

_BASELINE_PATH = _ROOT / "scripts" / "migration_baseline.json"
_MANAGED_CI = (_ROOT / "MANAGED_CI.md").read_text(encoding="utf-8")
_REAL_CHANGELOG = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

# A small synthetic world, so no case depends on the shipped surface list.
_SURFACES = {"init.gitignore": "ignore\n", "init.ci[main]": "workflow\n"}
_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- thing\n\n## [1.2.0] - 2026-08-01\n"
_CHANGELOG_WITH_MIGRATION = (
    "# Changelog\n\n## [Unreleased]\n\n### Migration\n\nDo the thing.\n\n## [1.2.0] - 2026-08-01\n"
)


def _baseline(surfaces=None, version="1.2.0"):
    """Return baseline text for one snapshot, defaulting to the synthetic surfaces."""
    return render_baseline(version, _SURFACES if surfaces is None else surfaces)


# --- Offline: the working tree against the committed baseline ------------------------------


@pytest.mark.parametrize(
    "changelog",
    [
        pytest.param(_CHANGELOG, id="no-migration-subsection"),
        pytest.param(_CHANGELOG_WITH_MIGRATION, id="migration-subsection-present"),
    ],
)
def test_unchanged_output_passes_with_or_without_the_subsection(changelog):
    assert check_migration_rule(dict(_SURFACES), _baseline(), changelog) == []


def test_changed_output_without_the_subsection_fails_naming_the_surface():
    changed = {**_SURFACES, "init.ci[main]": "workflow, but different\n"}
    messages = check_migration_rule(changed, _baseline(), _CHANGELOG)
    assert len(messages) == 1
    assert "init.ci[main]" in messages[0]
    assert "### Migration" in messages[0]


def test_changed_output_with_the_subsection_under_unreleased_passes():
    changed = {**_SURFACES, "init.ci[main]": "workflow, but different\n"}
    assert check_migration_rule(changed, _baseline(), _CHANGELOG_WITH_MIGRATION) == []


def test_a_missing_baseline_key_counts_as_a_change():
    messages = check_migration_rule(
        dict(_SURFACES), _baseline({"init.gitignore": "ignore\n"}), _CHANGELOG
    )
    assert len(messages) == 1
    assert "init.ci[main]" in messages[0]


def test_an_extra_baseline_key_counts_as_a_change():
    extra = {**_SURFACES, "init.ci[trunk]": "workflow\n"}
    messages = check_migration_rule(dict(_SURFACES), _baseline(extra), _CHANGELOG)
    assert len(messages) == 1
    assert "init.ci[trunk]" in messages[0]


def test_a_stamp_that_is_not_the_latest_released_heading_fails():
    messages = check_migration_rule(dict(_SURFACES), _baseline(version="1.1.0"), _CHANGELOG)
    assert len(messages) == 1
    assert "1.1.0" in messages[0]
    assert "1.2.0" in messages[0]
    assert "--update" in messages[0]


@pytest.mark.parametrize(
    "baseline_text",
    [
        pytest.param(None, id="missing"),
        pytest.param("{not json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"surfaces": {}}', id="no-version"),
        pytest.param('{"version": "1.2.0"}', id="no-surfaces"),
        pytest.param('{"version": 12, "surfaces": {}}', id="version-not-a-string"),
        pytest.param('{"version": "1.2.0", "surfaces": []}', id="surfaces-not-an-object"),
        pytest.param('{"version": "1.2.0", "surfaces": {"a": 1}}', id="surface-not-a-string"),
    ],
)
def test_a_missing_or_malformed_baseline_fails_with_the_update_hint(baseline_text):
    messages = check_migration_rule(dict(_SURFACES), baseline_text, _CHANGELOG)
    assert len(messages) == 1
    assert "--update" in messages[0]
    assert "scripts/migration_baseline.json" in messages[0]


def test_a_changelog_with_no_released_heading_fails_the_stamp_check():
    messages = check_migration_rule(
        dict(_SURFACES), _baseline(), "# Changelog\n\n## [Unreleased]\n"
    )
    assert len(messages) == 1
    assert "None" in messages[0]


# --- Version-pin normalization -------------------------------------------------------------


def test_a_pin_only_difference_normalizes_away():
    """The routine per-release substitution is what step 4 exempts, so it cannot trip the rule."""
    block = "uvx --from doc-lattice==9.9.9 doc-lattice check\n"
    other = "uvx --from doc-lattice==5.0.0 doc-lattice check\n"
    assert normalize_pins(block) == normalize_pins(other)
    assert f"doc-lattice=={SENTINEL_VERSION}" in normalize_pins(block)


def test_the_rendered_surfaces_carry_only_the_sentinel_pin():
    """Renderer normalization by construction: nothing in the snapshot names a live version."""
    surfaces = compute_surfaces(_MANAGED_CI)
    pinned = [text for key, text in surfaces.items() if "doc-lattice==" in text]
    assert pinned, "the snapshot must carry install pins, or normalization proves nothing"
    for text in pinned:
        for occurrence in text.split("doc-lattice==")[1:]:
            assert occurrence.startswith(SENTINEL_VERSION), text


# --- The branch matrix ---------------------------------------------------------------------


def test_the_snapshot_covers_the_slashed_and_boolean_like_branch_names():
    surfaces = compute_surfaces(_MANAGED_CI)
    for branch in CI_BRANCHES:
        assert f"init.ci[{branch}]" in surfaces
    assert 'branches: ["on"]' in surfaces["init.ci[on]"]
    assert "branches: [release/2.x]" in surfaces["init.ci[release/2.x]"]


def test_a_change_confined_to_the_boolean_like_branch_is_detected():
    """The matrix earns its place only if a shape-specific change actually shows up."""
    surfaces = compute_surfaces(_MANAGED_CI)
    recorded = dict(surfaces)
    # The unquoted spelling a YAML 1.1 reader resolves to a boolean: every other surface is
    # untouched, so nothing but this branch's shape can raise the message.
    recorded["init.ci[on]"] = recorded["init.ci[on]"].replace('["on"]', "[on]")
    messages = check_migration_rule(surfaces, render_baseline("1.2.0", recorded), _CHANGELOG)
    assert len(messages) == 1
    assert "init.ci[on]" in messages[0]
    assert "init.ci[main]" not in messages[0]


# --- The config matrix ----------------------------------------------------------------------


def test_compute_surfaces_snapshots_four_generated_config_shapes():
    surfaces = compute_surfaces(_MANAGED_CI)

    assert surfaces["init.config[default]"].count("link_sources:\n  - docs/**/*.md\n") == 1
    assert "  - SPEC.md\n" in surfaces["init.config[file-root]"]
    assert "  - notes [[]draft]/**/*.md\n" in surfaces["init.config[metacharacter-root]"]
    assert "  - design/**/*.md\n  - lore/**/*.md\n" in surfaces["init.config[multiple-roots]"]


# --- The base-ref half ---------------------------------------------------------------------


def _promoted(version="1.3.0", *, migration: bool) -> str:
    """Return a changelog whose newest section is a promoted release, with or without Migration."""
    body = "### Migration\n\nDo the thing.\n" if migration else "### Added\n\n- thing\n"
    return (
        f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-08-20\n\n{body}\n"
        "## [1.2.0] - 2026-08-01\n"
    )


def _base(baseline_text, changelog=_CHANGELOG):
    return BaseState(baseline_text=baseline_text, changelog_text=changelog)


def test_a_baseline_content_change_without_a_promotion_fails_even_with_a_subsection():
    """The co-update case: renderer and baseline moved together, so offline reads as equal."""
    changed = {**_SURFACES, "init.ci[main]": "workflow, but different\n"}
    messages = check_migration_rule(
        changed,
        _baseline(changed),
        _CHANGELOG_WITH_MIGRATION,
        _base(_baseline(), _CHANGELOG_WITH_MIGRATION),
    )
    assert len(messages) == 1
    assert "without a version promotion" in messages[0]
    assert "revert the baseline edit" in messages[0]


def test_a_content_change_promoted_with_a_subsection_passes():
    changed = {**_SURFACES, "init.ci[main]": "workflow, but different\n"}
    head_changelog = _promoted(migration=True)
    messages = check_migration_rule(
        changed,
        _baseline(changed, version="1.3.0"),
        head_changelog,
        _base(_baseline()),
    )
    assert messages == []


def test_a_content_change_promoted_without_a_subsection_fails():
    changed = {**_SURFACES, "init.ci[main]": "workflow, but different\n"}
    messages = check_migration_rule(
        changed,
        _baseline(changed, version="1.3.0"),
        _promoted(migration=False),
        _base(_baseline()),
    )
    assert len(messages) == 1
    assert "1.3.0" in messages[0]
    assert "init.ci[main]" in messages[0]


def test_a_stamp_only_advance_is_a_pin_only_release_and_needs_no_subsection():
    messages = check_migration_rule(
        dict(_SURFACES),
        _baseline(version="1.3.0"),
        _promoted(migration=False),
        _base(_baseline()),
    )
    assert messages == []


def test_an_absent_base_baseline_skips_the_base_ref_half():
    """The guard's own introduction: there is nothing at the base ref to compare against."""
    assert check_migration_rule(dict(_SURFACES), _baseline(), _CHANGELOG, _base(None)) == []


def test_an_unchanged_baseline_is_not_a_promotion_requirement():
    assert check_migration_rule(dict(_SURFACES), _baseline(), _CHANGELOG, _base(_baseline())) == []


def test_a_malformed_head_baseline_is_reported_once_by_the_offline_half():
    messages = check_migration_rule(dict(_SURFACES), "{not json", _CHANGELOG, _base(_baseline()))
    assert len(messages) == 1
    assert "--update" in messages[0]


# --- Serialization -------------------------------------------------------------------------


def test_render_baseline_round_trips_through_the_parser():
    text = _baseline()
    assert text.endswith("\n")
    assert parse_baseline(text) == ("1.2.0", dict(_SURFACES))


def test_render_baseline_is_sorted_and_indented():
    text = render_baseline("1.2.0", {"b": "second", "a": "first"})
    assert (
        text
        == json.dumps(
            {"version": "1.2.0", "surfaces": {"a": "first", "b": "second"}},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


# --- Correspondence with the real repository ------------------------------------------------


def test_the_real_document_yields_every_surface_nonempty():
    surfaces = compute_surfaces(_MANAGED_CI)
    expected = {
        "init.gitignore",
        "init.precommit",
        "managed-ci.linear-workflow",
        "managed-ci.gh-environment",
        "managed-ci.gh-secret",
        *(f"init.ci[{branch}]" for branch in CI_BRANCHES),
        *(f"init.config[{name}]" for name in _SCRIPT["CONFIG_ROOTS"]),
    }
    assert set(surfaces) == expected
    for key, text in surfaces.items():
        assert text.strip(), f"{key} extracted empty"
    assert surfaces["managed-ci.linear-workflow"].startswith("name: doc-lattice Linear\n")
    assert "gh secret set DOC_LATTICE_LINEAR_API_KEY" in surfaces["managed-ci.gh-secret"]
    assert "deployment-branch-policies" in surfaces["managed-ci.gh-environment"]


def test_the_committed_baseline_matches_the_real_tree():
    """Keeps the shipped baseline honest, the way the version-sync suite pins its own manifest."""
    baseline_text = _BASELINE_PATH.read_text(encoding="utf-8")
    surfaces = compute_surfaces(_MANAGED_CI)
    assert check_migration_rule(surfaces, baseline_text, _REAL_CHANGELOG) == []


def test_a_missing_managed_ci_section_fails_loudly():
    stripped = _MANAGED_CI.replace("### 4. Set the environment secret", "### 4. Renamed")
    with pytest.raises(_SCRIPT["SurfaceError"]) as raised:
        compute_surfaces(stripped)
    assert "MANAGED_CI.md" in str(raised.value)


def test_a_missing_workflow_block_fails_loudly():
    stripped = _MANAGED_CI.replace("name: doc-lattice Linear\n", "name: something else\n")
    with pytest.raises(_SCRIPT["SurfaceError"]) as raised:
        compute_surfaces(stripped)
    assert "exactly one" in str(raised.value)
