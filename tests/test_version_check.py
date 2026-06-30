"""Tests for version_check."""

from game_lattice.version_check import check_version_consistency

_PYPROJECT = '[project]\nname = "game-lattice"\nversion = "0.4.0"\n'
_CHANGELOG = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n### Added\n\n- thing\n"


def test_all_sources_agree_returns_empty():
    assert check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG) == []


def test_pyproject_disagrees_is_reported():
    pyproject = '[project]\nname = "game-lattice"\nversion = "0.3.0"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]
    assert "0.4.0" in messages[0]


def test_mismatch_message_names_both_found_and_expected():
    pyproject = '[project]\nname = "game-lattice"\nversion = "0.3.0"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "0.3.0" in messages[0]  # the value actually found in pyproject
    assert "0.4.0" in messages[0]  # the expected (canonical) value


def test_changelog_disagrees_is_reported():
    changelog = "# Changelog\n\n## [0.3.0] - 2026-06-28\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_both_disagree_returns_two_messages():
    pyproject = '[project]\nversion = "0.1.0"\n'
    changelog = "# Changelog\n\n## [0.2.0]\n"
    messages = check_version_consistency("0.4.0", pyproject, changelog)
    assert len(messages) == 2


def test_unreleased_heading_is_skipped():
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-01\n"
    assert check_version_consistency("0.4.0", _PYPROJECT, changelog) == []


def test_first_version_heading_wins_over_later_ones():
    # Two real release headings stacked newest-first; the TOP one is canonical.
    changelog = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n## [0.3.0] - 2026-06-28\n"
    # Top heading 0.4.0 agrees with init + _PYPROJECT (both 0.4.0) -> consistent.
    assert check_version_consistency("0.4.0", _PYPROJECT, changelog) == []
    # Make pyproject agree with 0.3.0 so ONLY the changelog can disagree; if the
    # function wrongly picked the bottom heading (0.3.0), this would be [].
    pyproject_030 = '[project]\nname = "game-lattice"\nversion = "0.3.0"\n'
    messages = check_version_consistency("0.3.0", pyproject_030, changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]
    assert "0.4.0" in messages[0]  # matched the TOP heading, not 0.3.0


def test_missing_pyproject_version_is_a_mismatch():
    pyproject = '[project]\nname = "game-lattice"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_pyproject_without_project_table_is_a_mismatch():
    pyproject = 'name = "game-lattice"\nversion = "0.4.0"\n'  # no [project] table
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_non_table_project_value_is_a_mismatch():
    # [project] parses to a string, not a table; must be reported, never crash.
    pyproject = 'project = "game-lattice"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_malformed_pyproject_is_a_mismatch_not_an_error():
    pyproject = "[project"  # unterminated table header, invalid TOML
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_changelog_without_version_heading_is_a_mismatch():
    changelog = "# Changelog\n\nNo releases yet.\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]
