"""Tests for the release-notes extraction script."""

from pathlib import Path
from runpy import run_path

_SCRIPT = run_path(str(Path(__file__).parents[1] / "scripts" / "extract_release_notes.py"))
changelog_section = _SCRIPT["changelog_section"]

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
