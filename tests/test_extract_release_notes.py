"""Tests for the release-notes extraction script."""

import sys
from pathlib import Path
from runpy import run_path

import pytest

_SCRIPT = run_path(str(Path(__file__).parents[1] / "scripts" / "extract_release_notes.py"))
changelog_section = _SCRIPT["changelog_section"]
main = _SCRIPT["main"]


def _run_main(tmp_path, monkeypatch, changelog: str, version: str) -> None:
    """Run the script's entry point against a changelog written to `tmp_path`.

    `main` reads `CHANGELOG.md` relative to the `_REPO_ROOT` its own module computed from
    `__file__`, so redirect that global rather than the copy `run_path` handed back.
    """
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    monkeypatch.setitem(main.__globals__, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["extract_release_notes.py", version])
    main()


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


def test_main_exits_nonzero_when_the_section_is_empty(tmp_path, monkeypatch, capsys):
    # The release job runs this before pushing the tag, so a nonzero exit here is what keeps an
    # empty section from stranding an immutable tag with no notes to publish against it.
    changelog = "# Changelog\n\n## [1.2.3] - 2026-08-15\n\n## [1.2.2]\n\n- old\n"
    with pytest.raises(SystemExit) as raised:
        _run_main(tmp_path, monkeypatch, changelog, "1.2.3")
    assert raised.value.code == 1
    assert "empty" in capsys.readouterr().err


def test_main_exits_nonzero_when_the_section_is_missing(tmp_path, monkeypatch, capsys):
    changelog = "# Changelog\n\n## [1.2.2]\n\n- old\n"
    with pytest.raises(SystemExit) as raised:
        _run_main(tmp_path, monkeypatch, changelog, "1.2.3")
    assert raised.value.code == 1
    assert "no '## [1.2.3]' section" in capsys.readouterr().err


def test_main_writes_the_section_body_to_stdout(tmp_path, monkeypatch, capsys):
    changelog = "# Changelog\n\n## [1.2.3]\n\n### Added\n\n- a thing\n\n## [1.2.2]\n\n- old\n"
    _run_main(tmp_path, monkeypatch, changelog, "1.2.3")
    captured = capsys.readouterr()
    assert "- a thing" in captured.out
    assert "old" not in captured.out
    assert captured.err == ""


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


def test_changelog_section_does_not_truncate_on_a_bare_heading_marker():
    # A heading is one line. `##` alone above a line starting with `[` is not a section
    # boundary, and reading it as one truncates the notes where no Markdown reader ends
    # them. `release_gate.py` refuses a re-arm on this same reading, so a phantom boundary
    # there reports `## [Unreleased]` as empty and lets undocumented work ship in the tag.
    changelog = (
        "# Changelog\n\n"
        "## [0.6.0]\n\n"
        "- a bullet\n"
        "##\n"
        "[not a heading]\n"
        "- another bullet\n\n"
        "## [0.5.0]\n\n"
        "- old\n"
    )
    section = changelog_section(changelog, "0.6.0")
    assert section is not None
    assert "[not a heading]" in section
    assert "- another bullet" in section
    assert "old" not in section  # the next real section still bounds it
