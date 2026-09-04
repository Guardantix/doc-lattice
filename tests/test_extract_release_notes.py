"""Tests for the release-notes extraction script, its check mode, and that mode's wiring."""

import sys
from pathlib import Path
from runpy import run_path

import pytest
from workflow_helpers import _commands, _hook_invocations, _invocations, _invokes, _load_workflow

from doc_lattice import __version__

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = run_path(str(_ROOT / "scripts" / "extract_release_notes.py"))
changelog_section = _SCRIPT["changelog_section"]
main = _SCRIPT["main"]

_WORKFLOW = _load_workflow(_ROOT / ".github/workflows/ci.yml")
_SCRIPT_PATH = "scripts/extract_release_notes.py"


def _run_main(tmp_path, monkeypatch, changelog: str, version: str) -> None:
    """Run the script's entry point against a changelog written to `tmp_path`.

    `main` reads `CHANGELOG.md` relative to the `_REPO_ROOT` its own module computed from
    `__file__`, so redirect that global rather than the copy `run_path` handed back.
    """
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    monkeypatch.setitem(main.__globals__, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["extract_release_notes.py", version])
    main()


def _run_check(tmp_path, monkeypatch, changelog: str, *argv: str) -> None:
    """Run the script's ``--check`` mode against a changelog written to `tmp_path`.

    Args:
        tmp_path: The directory to write the changelog into and read it back from.
        monkeypatch: The fixture used to redirect the script's repository root and argv.
        changelog: The full ``CHANGELOG.md`` text to check.
        *argv: Any further arguments, so a case can name a version or omit it to exercise
            the default.
    """
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    monkeypatch.setitem(main.__globals__, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["extract_release_notes.py", "--check", *argv])
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


def test_check_mode_exits_nonzero_when_the_section_is_heading_only(tmp_path, monkeypatch, capsys):
    # The pre-merge counterpart of the release job's extraction. `check_version_sync.py` compares
    # the top heading against `__version__` and stops there, so a promoted heading with nothing
    # under it merges green and fails at the tag, which is the strand this mode exists to prevent.
    changelog = "# Changelog\n\n## [1.2.3] - 2026-08-15\n\n## [1.2.2]\n\n- old\n"

    with pytest.raises(SystemExit) as raised:
        _run_check(tmp_path, monkeypatch, changelog, "1.2.3")

    assert raised.value.code == 1
    assert "empty" in capsys.readouterr().err


def test_check_mode_exits_nonzero_when_the_section_is_missing(tmp_path, monkeypatch, capsys):
    # A missing section fails identically to an empty one. The release job's extraction refuses
    # both, so a pre-merge counterpart that only asked "does the section have a body" would let
    # exactly half of that refusal reach the tag.
    changelog = "# Changelog\n\n## [1.2.2]\n\n- old\n"

    with pytest.raises(SystemExit) as raised:
        _run_check(tmp_path, monkeypatch, changelog, "1.2.3")

    assert raised.value.code == 1
    assert "no '## [1.2.3]' section" in capsys.readouterr().err


def test_check_mode_exits_nonzero_on_a_bare_heading_marker_above_the_version(
    tmp_path, monkeypatch, capsys
):
    # The shape the two readers used to disagree about: `##` alone above `[1.2.3]`. This mode
    # reads the changelog through `changelog_section`, which does not join the two lines, so the
    # section is absent rather than empty and the check fails. `check_version_sync.py` now reads
    # the heading the same way, so the two agree about this file instead of one passing it to
    # the other. `tests/test_check_version_sync.py` holds that half.
    changelog = "# Changelog\n\n##\n[1.2.3] - 2026-08-15\n\n### Added\n\n- a thing\n"

    with pytest.raises(SystemExit) as raised:
        _run_check(tmp_path, monkeypatch, changelog, "1.2.3")

    assert raised.value.code == 1
    assert "no '## [1.2.3]' section" in capsys.readouterr().err


def test_check_mode_is_silent_when_the_section_has_a_body(tmp_path, monkeypatch, capsys):
    # Silent on success, unlike the extraction the release job runs: this mode is a gate in a
    # hook and a CI step, so printing the notes would put the whole section in every log.
    changelog = "# Changelog\n\n## [1.2.3]\n\n### Added\n\n- a thing\n\n## [1.2.2]\n\n- old\n"

    _run_check(tmp_path, monkeypatch, changelog, "1.2.3")

    assert capsys.readouterr() == ("", "")


def test_check_mode_defaults_to_the_declared_package_version(tmp_path, monkeypatch, capsys):
    # No version argument, because the hook and the CI step have no version to pass: the one
    # under release is whatever `__version__` declares, which is the same source version sync
    # holds the top heading to.
    changelog = f"# Changelog\n\n## [{__version__}]\n\n### Added\n\n- a thing\n"

    _run_check(tmp_path, monkeypatch, changelog)

    assert capsys.readouterr() == ("", "")


def test_check_mode_default_is_the_package_version_not_the_top_heading(
    tmp_path, monkeypatch, capsys
):
    # A changelog whose top section belongs to another version fails, which is what makes the
    # default a gate on the version under release rather than on whatever the document leads
    # with. Reading the top heading instead would pass every changelog with any nonempty
    # section, including the one this gate exists to refuse.
    changelog = "# Changelog\n\n## [0.0.1]\n\n- some other release\n"

    with pytest.raises(SystemExit) as raised:
        _run_check(tmp_path, monkeypatch, changelog)

    assert raised.value.code == 1
    assert f"no '## [{__version__}]' section" in capsys.readouterr().err


def test_the_pre_commit_hook_runs_the_changelog_body_check():
    # The parser tests above cannot tell whether the gate is wired, only that it would be right
    # if it ran. This is the hook half of "it runs where version sync already runs".
    invocations = _hook_invocations("check-changelog-body")

    assert [argv[-2:] for argv in invocations] == [[_SCRIPT_PATH, "--check"]]


def test_the_ci_code_quality_job_runs_the_changelog_body_check():
    # The CI half, and the authority of the two: the hook is installed per clone, so a
    # contributor who never ran `pre-commit install` reaches only this one.
    job = _WORKFLOW["jobs"]["code-quality"]
    invocations = [argv for step in job["steps"] for argv in _invocations(_commands(step))]

    checks = [argv for argv in invocations if _invokes(argv, _SCRIPT_PATH)]
    assert len(checks) == 1, f"expected one changelog-body check in code-quality, found {checks}"
    assert checks[0][-1] == "--check"


def test_the_release_job_still_extracts_rather_than_checks():
    # The pre-merge gate is an addition, not a move. The release job's own extraction has to keep
    # producing the notes file it publishes from, so a `--check` that spread to it would leave
    # `release-notes.md` empty while every gate stayed green.
    release = _WORKFLOW["jobs"]["release"]
    invocations = [argv for step in release["steps"] for argv in _invocations(_commands(step))]

    extractions = [argv for argv in invocations if _invokes(argv, _SCRIPT_PATH)]
    assert len(extractions) == 1, f"expected one extraction in release, found {extractions}"
    assert "--check" not in extractions[0]
