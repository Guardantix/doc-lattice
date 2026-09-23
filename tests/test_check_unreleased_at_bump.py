"""Tests for the bump guard that holds ``## [Unreleased]`` present and empty on a version bump.

The script is loaded rather than imported, and through `tests/script_loader.py`, because it
imports two sibling scripts and a bare `run_path` would not let it. The policy is driven through
`bump_messages` over synthetic changelogs; the entry point is driven against real Git
repositories, because what it reads is two commits and not a working tree. Its CI wiring is
pinned in `tests/test_release_workflow.py`, beside the migration guard that shares its fetch.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from script_loader import load_script, script_path

_SCRIPT_PATH = script_path("check_unreleased_at_bump.py")
bump_messages = load_script(_SCRIPT_PATH)["bump_messages"]

_EMPTY = "# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2026-01-02\n\n- shipped\n"
_PENDING = (
    "# Changelog\n\n## [Unreleased]\n\n- left behind\n\n## [1.1.0] - 2026-01-02\n\n- shipped\n"
)
_NO_HEADING = "# Changelog\n\n## [1.1.0] - 2026-01-02\n\n- shipped\n"


# --- The policy, over synthetic changelogs --------------------------------------------------


@pytest.mark.parametrize(
    "changelog",
    [
        pytest.param(_EMPTY, id="present-and-empty"),
        pytest.param(_PENDING, id="pending-entry"),
        pytest.param(_NO_HEADING, id="missing-heading"),
        pytest.param(None, id="missing-changelog"),
    ],
)
def test_a_change_that_is_not_a_bump_passes_whatever_unreleased_holds(changelog):
    # Entries accumulating under Unreleased between releases is the section's purpose, so the
    # guard has to be silent on every non-bump, including the shapes it refuses on a bump.
    assert bump_messages("1.1.0", "1.1.0", changelog) == []


def test_a_bump_that_leaves_the_heading_present_and_empty_passes():
    assert bump_messages("1.0.0", "1.1.0", _EMPTY) == []


def test_a_bump_that_leaves_an_entry_under_unreleased_fails_naming_the_new_section():
    messages = bump_messages("1.0.0", "1.1.0", _PENDING)

    assert len(messages) == 1
    assert "still has entries under '## [Unreleased]'" in messages[0]
    assert "'## [1.1.0]'" in messages[0]


@pytest.mark.parametrize(
    "changelog",
    [
        pytest.param(_NO_HEADING, id="heading-deleted"),
        pytest.param(None, id="changelog-deleted"),
        # A heading that merely starts with the name is a different heading. Matching it as a
        # prefix would read this bump as present-and-empty and pass it.
        pytest.param(
            "# Changelog\n\n## [Unreleased-notes]\n\n## [1.1.0] - 2026-01-02\n\n- shipped\n",
            id="heading-only-shares-a-prefix",
        ),
    ],
)
def test_a_bump_without_the_heading_fails_rather_than_passing_as_empty(changelog):
    # The parser answers None for a missing heading and "" for an empty one. The re-arm predicate
    # in `release_gate.py` reads both as "nothing pending"; this guard must not, since the heading
    # is the one the next cycle's entries land under.
    messages = bump_messages("1.0.0", "1.1.0", changelog)

    assert len(messages) == 1
    assert "has no '## [Unreleased]' heading" in messages[0]


def test_a_bare_heading_marker_does_not_end_the_section_early():
    # The section boundary is the notes extractor's, shared rather than re-derived: a bare `##`
    # above a line starting `[` is not a heading, so the entry below it still counts as pending.
    # A second reading that split on it would call this section empty and pass the bump.
    changelog = "# Changelog\n\n## [Unreleased]\n##\n[x] left behind\n\n## [1.1.0]\n\n- shipped\n"

    assert len(bump_messages("1.0.0", "1.1.0", changelog)) == 1


# --- The entry point, against real Git repositories ------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - controlled test Git arguments
        ("git", *args),  # noqa: S607 - Git is required by this test suite
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, *, version: str | None = None, changelog: str | None = None) -> str:
    if version is not None:
        path = repo / "src/doc_lattice/__init__.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    if changelog is not None:
        (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--allow-empty", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


def _run(repo: Path, base_ref: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - controlled script and arguments
        (sys.executable, str(_SCRIPT_PATH), "--base-ref", base_ref),
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "release-test@example.com")
    _git(tmp_path, "config", "user.name", "Release Test")
    return tmp_path


@pytest.fixture
def base(repo: Path) -> str:
    """Commit a released 1.0.0 with the heading present and empty, and return its sha."""
    return _commit(
        repo,
        version="1.0.0",
        changelog="# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n- first\n",
    )


def test_a_bump_with_a_pending_entry_fails_at_the_entry_point(repo: Path, base: str):
    _commit(repo, version="1.1.0", changelog=_PENDING)

    result = _run(repo, base)

    assert result.returncode == 1
    assert "still has entries under '## [Unreleased]'" in result.stderr


def test_a_bump_with_the_heading_present_and_empty_passes_at_the_entry_point(repo: Path, base: str):
    _commit(repo, version="1.1.0", changelog=_EMPTY)

    result = _run(repo, base)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_an_ordinary_change_with_a_pending_entry_passes_at_the_entry_point(repo: Path, base: str):
    _commit(repo, changelog="# Changelog\n\n## [Unreleased]\n\n- new work\n\n## [1.0.0]\n\n- x\n")

    result = _run(repo, base)

    assert result.returncode == 0, result.stderr


def test_an_entry_added_after_a_clean_bump_commit_still_fails(repo: Path, base: str):
    # The tag names the final landed commit, so the candidate is what is read. A guard that
    # inspected the commit that first changed the version would pass this pull request, and the
    # entry added after it would ship inside the tag with the notes silent about it.
    _commit(repo, version="1.1.0", changelog=_EMPTY)
    _commit(repo, changelog=_PENDING)

    result = _run(repo, base)

    assert result.returncode == 1
    assert "still has entries under '## [Unreleased]'" in result.stderr


@pytest.mark.usefixtures("base")
def test_an_unreadable_base_ref_fails_as_an_error_not_as_a_non_bump(repo: Path):
    # A base that cannot be read establishes nothing. Treating it as "not a bump" would pass the
    # exact pull request the guard exists for whenever the fetch it depends on went wrong.
    _commit(repo, version="1.1.0", changelog=_PENDING)

    result = _run(repo, "refs/heads/no-such-base")

    assert result.returncode == 1
    assert "could not establish whether this change is a version bump" in result.stderr


def test_a_malformed_base_version_fails_as_an_error(repo: Path):
    path = repo / "src/doc_lattice/__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text("__version__ = 1.0.0\n", encoding="utf-8")
    base = _commit(repo, changelog=_EMPTY)
    _commit(repo, version="1.1.0")

    result = _run(repo, base)

    assert result.returncode == 1
    assert "could not establish whether this change is a version bump" in result.stderr
    assert "base ref" in result.stderr
