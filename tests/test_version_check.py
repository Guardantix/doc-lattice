"""Tests for version_check."""

from doc_lattice.version_check import changelog_section, check_version_consistency

_PYPROJECT = '[project]\nname = "doc-lattice"\nversion = "0.4.0"\n'
_CHANGELOG = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n### Added\n\n- thing\n"
_README = "# doc-lattice\n\nuvx --from doc-lattice==0.4.0 doc-lattice --help\n"


def test_all_sources_agree_returns_empty():
    assert check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, _README) == []


def test_pyproject_disagrees_is_reported():
    pyproject = '[project]\nname = "doc-lattice"\nversion = "0.3.0"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG, _README)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]
    assert "0.4.0" in messages[0]


def test_mismatch_message_names_both_found_and_expected():
    pyproject = '[project]\nname = "doc-lattice"\nversion = "0.3.0"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG, _README)
    assert len(messages) == 1
    assert "0.3.0" in messages[0]  # the value actually found in pyproject
    assert "0.4.0" in messages[0]  # the expected (canonical) value


def test_changelog_disagrees_is_reported():
    changelog = "# Changelog\n\n## [0.3.0] - 2026-06-28\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, changelog, _README)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_both_disagree_returns_two_messages():
    pyproject = '[project]\nversion = "0.1.0"\n'
    changelog = "# Changelog\n\n## [0.2.0]\n"
    messages = check_version_consistency("0.4.0", pyproject, changelog, _README)
    assert len(messages) == 2


def test_unreleased_heading_is_skipped():
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-01\n"
    assert check_version_consistency("0.4.0", _PYPROJECT, changelog, _README) == []


def test_first_version_heading_wins_over_later_ones():
    # Two real release headings stacked newest-first; the TOP one is canonical.
    changelog = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n## [0.3.0] - 2026-06-28\n"
    # Top heading 0.4.0 agrees with init + _PYPROJECT (both 0.4.0) -> consistent.
    assert check_version_consistency("0.4.0", _PYPROJECT, changelog, _README) == []
    # Make pyproject agree with 0.3.0 so ONLY the changelog can disagree; if the
    # function wrongly picked the bottom heading (0.3.0), this would be [].
    pyproject_030 = '[project]\nname = "doc-lattice"\nversion = "0.3.0"\n'
    readme_030 = "uvx --from doc-lattice==0.3.0 doc-lattice\n"
    messages = check_version_consistency("0.3.0", pyproject_030, changelog, readme_030)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]
    assert "0.4.0" in messages[0]  # matched the TOP heading, not 0.3.0


def test_missing_pyproject_version_is_a_mismatch():
    pyproject = '[project]\nname = "doc-lattice"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG, _README)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_pyproject_without_project_table_is_a_mismatch():
    pyproject = 'name = "doc-lattice"\nversion = "0.4.0"\n'  # no [project] table
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG, _README)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_non_table_project_value_is_a_mismatch():
    # [project] parses to a string, not a table; must be reported, never crash.
    pyproject = 'project = "doc-lattice"\n'
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG, _README)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_malformed_pyproject_is_a_mismatch_not_an_error():
    pyproject = "[project"  # unterminated table header, invalid TOML
    messages = check_version_consistency("0.4.0", pyproject, _CHANGELOG, _README)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_changelog_without_version_heading_is_a_mismatch():
    changelog = "# Changelog\n\nNo releases yet.\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, changelog, _README)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_readme_pypi_pin_matches_is_consistent():
    readme = "uvx --from doc-lattice==0.4.0 doc-lattice\n"
    assert check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, readme) == []


def test_readme_tagged_git_pin_matches_is_consistent():
    readme = "uvx --from git+https://github.com/Guardantix/doc-lattice@v0.4.0 doc-lattice\n"
    assert check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, readme) == []


def test_readme_stale_pypi_pin_is_reported():
    readme = "uvx --from doc-lattice==0.3.0 doc-lattice\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, readme)
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "0.3.0" in messages[0]
    assert "0.4.0" in messages[0]


def test_readme_stale_tagged_git_pin_is_reported():
    readme = "uvx --from git+https://github.com/Guardantix/doc-lattice@v0.3.0 doc-lattice\n"
    messages = check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, readme)
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "0.3.0" in messages[0]


def test_readme_duplicate_stale_version_across_pin_syntaxes_yields_one_message():
    readme = (
        "uvx --from doc-lattice==0.3.0 doc-lattice init\n"
        "uvx --from git+https://github.com/Guardantix/"
        "doc-lattice@v0.3.0 doc-lattice --help\n"
    )
    messages = check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, readme)
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "0.3.0" in messages[0]


def test_readme_without_pin_is_consistent():
    readme = "# doc-lattice\n\nNo install instructions here.\n"
    assert check_version_consistency("0.4.0", _PYPROJECT, _CHANGELOG, readme) == []


_NOTES_CHANGELOG = (
    "# Changelog\n\n"
    "## [Unreleased]\n\n"
    "### Added\n\n"
    "- unreleased thing\n\n"
    "## [0.6.0] - 2026-07-05\n\n"
    "### Changed\n\n"
    "- lowered the Python floor to 3.13\n"
    "- another change\n\n"
    "## [0.5.0] - 2026-07-01\n\n"
    "### Added\n\n"
    "- github-slug anchors\n"
)


def test_changelog_section_returns_body_for_the_named_version():
    section = changelog_section(_NOTES_CHANGELOG, "0.6.0")
    assert section is not None
    assert "### Changed" in section
    assert "- lowered the Python floor to 3.13" in section
    assert "- another change" in section


def test_changelog_section_stops_at_the_next_heading():
    section = changelog_section(_NOTES_CHANGELOG, "0.6.0")
    assert section is not None
    # The 0.5.0 section that follows must not bleed in.
    assert "github-slug anchors" not in section
    assert "0.5.0" not in section
    # Nor the Unreleased section that precedes it.
    assert "unreleased thing" not in section


def test_changelog_section_is_trimmed_of_edge_blank_lines():
    section = changelog_section(_NOTES_CHANGELOG, "0.6.0")
    assert section is not None
    assert not section.startswith("\n")
    assert not section.endswith("\n")


def test_changelog_section_targets_a_lower_section_too():
    section = changelog_section(_NOTES_CHANGELOG, "0.5.0")
    assert section is not None
    assert "github-slug anchors" in section
    assert "lowered the Python floor" not in section


def test_changelog_section_unknown_version_returns_none():
    assert changelog_section(_NOTES_CHANGELOG, "9.9.9") is None


def test_changelog_section_present_but_empty_returns_empty_string():
    changelog = "# Changelog\n\n## [0.6.0] - 2026-07-05\n\n## [0.5.0]\n\n- old\n"
    assert changelog_section(changelog, "0.6.0") == ""


def test_changelog_section_last_section_runs_to_end_of_file():
    changelog = "# Changelog\n\n## [0.6.0]\n\n### Added\n\n- only release\n"
    section = changelog_section(changelog, "0.6.0")
    assert section is not None
    assert "- only release" in section


def test_changelog_section_does_not_match_a_version_that_is_a_substring():
    changelog = "# Changelog\n\n## [10.6.0]\n\n- ten\n"
    assert changelog_section(changelog, "0.6.0") is None


def test_changelog_section_does_not_truncate_on_a_code_comment_line():
    # A fenced code block whose content starts with '## ' must not be treated as a
    # section boundary; only real '## [heading]' lines delimit sections.
    changelog = (
        "# Changelog\n\n"
        "## [0.6.0]\n\n"
        "### Added\n\n"
        "- a shell example:\n\n"
        "```bash\n"
        "## step one\n"
        "run --thing\n"
        "```\n\n"
        "- trailing bullet after the block\n\n"
        "## [0.5.0]\n\n"
        "- old\n"
    )
    section = changelog_section(changelog, "0.6.0")
    assert section is not None
    assert "## step one" in section  # the code comment survives, not truncated
    assert "- trailing bullet after the block" in section
    assert "old" not in section  # the next real section still bounds it
