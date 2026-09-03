"""Behavior tests for the release gate against real Git repositories."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_GATE = _ROOT / "scripts/release_gate.py"
_VERSION_FILE = Path("src/doc_lattice/__init__.py")
_ATTEMPT_FILE = Path(".release-attempt")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - controlled test Git arguments
        ("git", *args),  # noqa: S607 - Git is required by this test suite
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_version(repo: Path, version: str) -> None:
    path = repo / _VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _write_attempt(repo: Path, text: str) -> None:
    (repo / _ATTEMPT_FILE).write_text(text, encoding="utf-8")


def _write_changelog(repo: Path, *, unreleased: str, released: str) -> None:
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n{unreleased}\n## [1.0.0] - 2026-01-01\n\n{released}",
        encoding="utf-8",
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "release-test@example.com")
    _git(tmp_path, "config", "user.name", "Release Test")
    return tmp_path


def _run_gate(
    repo: Path, *, tag: str, version: str, sha: str, before: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    output = repo / "github-output.txt"
    env = os.environ | {
        "TAG": tag,
        "VERSION": version,
        "GITHUB_SHA": sha,
        "GITHUB_OUTPUT": str(output),
    }
    if before is not None:
        env["GITHUB_BEFORE"] = before
    result = subprocess.run(  # noqa: S603 - controlled script and arguments
        (sys.executable, str(_GATE)),
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    return result, lines


def test_existing_tag_with_different_version_fails(repo: Path):
    _write_version(repo, "0.9.0")
    _commit(repo, "old version")
    _git(repo, "tag", "v1.0.0")
    _write_version(repo, "1.0.0")
    sha = _commit(repo, "current version")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha)

    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "tag v1.0.0 points at version '0.9.0', not 1.0.0" in result.stdout
    assert outputs == []


def test_existing_tag_at_current_commit_is_retry(repo: Path):
    _write_version(repo, "1.0.0")
    sha = _commit(repo, "release")
    _git(repo, "tag", "v1.0.0")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=false"]


def test_existing_tag_at_older_commit_is_ordinary_noop(repo: Path):
    _write_version(repo, "1.0.0")
    _commit(repo, "release")
    _git(repo, "tag", "v1.0.0")
    (repo / "README.md").write_text("later change\n", encoding="utf-8")
    sha = _commit(repo, "later change")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha)

    assert result.returncode == 0
    assert outputs == ["proceed=false", "create_tag=false"]


def test_absent_tag_with_same_version_before_push_is_ordinary_noop(repo: Path):
    # This is also the no-token re-arm case, and deliberately not a second test: with no
    # `.release-attempt` in the release commit, `_re_arm_attempt` returns before it parses
    # anything, so a token's absence and a version that did not change are one path.
    _write_version(repo, "1.0.0")
    before = _commit(repo, "release version without tag")
    (repo / "README.md").write_text("later change\n", encoding="utf-8")
    sha = _commit(repo, "later change")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=false", "create_tag=false"]


def test_absent_tag_with_different_version_before_push_is_new_release(repo: Path):
    _write_version(repo, "0.9.0")
    before = _commit(repo, "old version")
    _write_version(repo, "1.0.0")
    sha = _commit(repo, "release version")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=true"]


def test_absent_tag_with_bump_earlier_in_push_releases_final_commit(repo: Path):
    _write_version(repo, "0.9.0")
    before = _commit(repo, "old version")
    _write_version(repo, "1.0.0")
    _commit(repo, "release version")
    (repo / "README.md").write_text("follow-up\n", encoding="utf-8")
    sha = _commit(repo, "follow-up on the same push")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=true"]


def test_absent_tag_with_no_version_file_before_push_is_new_release(repo: Path):
    (repo / "README.md").write_text("before package\n", encoding="utf-8")
    before = _commit(repo, "before package")
    _write_version(repo, "1.0.0")
    sha = _commit(repo, "release version")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=true"]


def test_malformed_pre_push_version_source_fails(repo: Path):
    path = repo / _VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VERSION = unknown\n", encoding="utf-8")
    before = _commit(repo, "malformed pre-push source")
    _write_version(repo, "1.0.0")
    sha = _commit(repo, "introduce valid release version")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode != 0
    assert (
        "::error::pre-push source has a malformed version declaration "
        "in src/doc_lattice/__init__.py"
    ) in result.stdout
    assert outputs == []


def test_malformed_current_version_source_fails(repo: Path):
    _write_version(repo, "0.9.0")
    _commit(repo, "old version")
    path = repo / _VERSION_FILE
    path.write_text("VERSION = unknown\n", encoding="utf-8")
    sha = _commit(repo, "malformed release")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha)

    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "current source" in result.stdout
    assert outputs == []


def test_malformed_tagged_version_source_fails(repo: Path):
    path = repo / _VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("VERSION = unknown\n", encoding="utf-8")
    _commit(repo, "malformed tag")
    _git(repo, "tag", "v1.0.0")
    _write_version(repo, "1.0.0")
    sha = _commit(repo, "valid current source")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha)

    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "tagged source" in result.stdout
    assert outputs == []


def test_absent_tag_with_a_fresh_re_arm_token_starts_release_work(repo: Path):
    _write_version(repo, "1.0.0")
    before = _commit(repo, "release version whose run failed before the tag")
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    sha = _commit(repo, "fix the defect and re-arm")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=true"]
    assert "re-arm token" in result.stdout


def test_absent_tag_with_an_unchanged_re_arm_token_is_an_ordinary_noop(repo: Path):
    _write_version(repo, "1.0.0")
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    before = _commit(repo, "released version carrying a spent token")
    (repo / "README.md").write_text("later change\n", encoding="utf-8")
    sha = _commit(repo, "unrelated later change")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=false", "create_tag=false"]


def test_a_second_re_arm_for_the_same_version_starts_release_work(repo: Path):
    _write_version(repo, "1.0.0")
    _write_attempt(repo, "1.0.0 first-fix\n")
    before = _commit(repo, "first re-arm, whose run also failed")
    _write_attempt(repo, "1.0.0 second-fix\n")
    sha = _commit(repo, "second fix and second re-arm")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=true"]


def test_a_re_arm_token_removed_in_the_push_is_not_a_re_arm(repo: Path):
    _write_version(repo, "1.0.0")
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    before = _commit(repo, "release version carrying a token")
    (repo / _ATTEMPT_FILE).unlink()
    sha = _commit(repo, "remove the token")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=false", "create_tag=false"]


def test_a_changed_re_arm_token_naming_another_version_fails(repo: Path):
    _write_version(repo, "1.0.0")
    before = _commit(repo, "release version whose run failed before the tag")
    _write_attempt(repo, "0.9.0 fixture-fix\n")
    sha = _commit(repo, "re-arm the wrong version")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode != 0
    assert "::error::re-arm token names version '0.9.0', not 1.0.0" in result.stdout
    assert outputs == []


def test_a_malformed_changed_re_arm_token_fails(repo: Path):
    _write_version(repo, "1.0.0")
    before = _commit(repo, "release version whose run failed before the tag")
    _write_attempt(repo, "please release this\n")
    sha = _commit(repo, "re-arm with an unreadable token")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode != 0
    assert "::error::release commit has a malformed re-arm token in .release-attempt" in (
        result.stdout
    )
    assert outputs == []


def test_a_malformed_re_arm_token_left_unchanged_is_inert(repo: Path):
    _write_version(repo, "1.0.0")
    _write_attempt(repo, "please release this\n")
    before = _commit(repo, "release version carrying an unreadable token")
    (repo / "README.md").write_text("later change\n", encoding="utf-8")
    sha = _commit(repo, "later change that does not touch the token")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=false", "create_tag=false"]


def test_a_fresh_re_arm_token_never_recreates_an_existing_tag(repo: Path):
    _write_version(repo, "1.0.0")
    _commit(repo, "release")
    _git(repo, "tag", "v1.0.0")
    before = _git(repo, "rev-parse", "HEAD")
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    sha = _commit(repo, "re-arm a version that already shipped")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=false", "create_tag=false"]


def test_a_re_arm_with_pending_unreleased_entries_fails(repo: Path):
    _write_version(repo, "1.0.0")
    _write_changelog(repo, unreleased="", released="- the release this bump described\n")
    before = _commit(repo, "release version whose run failed before the tag")
    _write_changelog(
        repo,
        unreleased="- work that landed after the bump\n",
        released="- the release this bump described\n",
    )
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    sha = _commit(repo, "re-arm after unrelated work landed")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "CHANGELOG.md still has unreleased entries" in result.stdout
    assert outputs == []


def test_a_re_arm_with_an_empty_unreleased_heading_starts_release_work(repo: Path):
    _write_version(repo, "1.0.0")
    _write_changelog(repo, unreleased="", released="- the release this bump described\n")
    before = _commit(repo, "release version whose run failed before the tag")
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    sha = _commit(repo, "fix the defect and re-arm")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode == 0
    assert outputs == ["proceed=true", "create_tag=true"]


def test_a_re_arm_is_refused_when_a_bare_heading_marker_precedes_the_pending_entries(
    repo: Path,
):
    # The changelog reading is shared with the release-notes extractor, which is the point:
    # both have to agree on where a section ends. A bare `##` above a bracketed line is not a
    # boundary, so these entries are still pending and would ship inside the tag with the notes
    # silent about them. A reader that ended the section at the `##` would report Unreleased as
    # empty and permit the re-arm.
    _write_version(repo, "1.0.0")
    _write_changelog(repo, unreleased="", released="- the release this bump described\n")
    before = _commit(repo, "release version whose run failed before the tag")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n##\n[Notes]\n- work that landed after the bump\n\n"
        "## [1.0.0] - 2026-01-01\n\n- the release this bump described\n",
        encoding="utf-8",
    )
    _write_attempt(repo, "1.0.0 fixture-fix\n")
    sha = _commit(repo, "re-arm after unrelated work landed")

    result, outputs = _run_gate(repo, tag="v1.0.0", version="1.0.0", sha=sha, before=before)

    assert result.returncode != 0
    assert "CHANGELOG.md still has unreleased entries" in result.stdout
    assert outputs == []
