"""Coverage refuses omissions independently of lattice discovery and exact exemptions."""

import errno
import os
from pathlib import Path

import pytest

from doc_lattice.config import CoverageExclusion, CoverageExemption, SidecarCoverage
from doc_lattice.error_types import CoverageError
from doc_lattice.sidecar_coverage import enforce_coverage


def _policy(select=("*.md",), exempt=(), exclude=()):
    return SidecarCoverage(
        select=list(select),
        **(
            {"exempt": [CoverageExemption(path=path, reason="foreign fixture") for path in exempt]}
            if exempt
            else {}
        ),
        **(
            {
                "exclude": [
                    CoverageExclusion(select=selector, reason="outside the covered corpus")
                    for selector in exclude
                ]
            }
            if exclude
            else {}
        ),
    )


@pytest.mark.parametrize(
    "selectors",
    [
        ["*.md", "**/*.md", "a*.md", "*.md"],
        ["a*.md", "*.md", "**/*.md"],
    ],
)
def test_complete_uncovered_diagnostics_are_sorted_and_include_all_selectors(tmp_path, selectors):
    for name in ("z.md", "a.md", "a/b.md"):
        file = tmp_path / name
        file.parent.mkdir(exist_ok=True)
        file.write_text("# Untracked\n")

    with pytest.raises(CoverageError) as info:
        enforce_coverage(tmp_path, _policy(selectors), set())

    remedy = (
        "; not enrolled in the loaded lattice; register this file or add an exact "
        "sidecar_coverage.exempt entry with a reason"
    )
    assert str(info.value) == (
        "sidecar coverage failed:\n"
        "  'a.md': selected by '**/*.md', '*.md', 'a*.md'" + remedy + "\n"
        "  'a/b.md': selected by '**/*.md'" + remedy + "\n"
        "  'z.md': selected by '**/*.md', '*.md'" + remedy
    )


def test_contained_aliases_of_enrolled_targets_are_covered(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("# Body\n")
    (tmp_path / "alias.md").symlink_to(target)

    enforce_coverage(tmp_path, _policy(), {target})


@pytest.mark.parametrize("exempt", ["a.md", "b.md"])
def test_exemption_cannot_transfer_between_aliases(tmp_path, exempt):
    target = tmp_path / "target.txt"
    target.write_text("# Body\n")
    for alias in ("a.md", "b.md"):
        (tmp_path / alias).symlink_to(target)

    with pytest.raises(CoverageError) as info:
        enforce_coverage(tmp_path, _policy(exempt=[exempt]), set())

    other = "b.md" if exempt == "a.md" else "a.md"
    assert f"'{other}': selected by" in str(info.value)
    assert f"'{exempt}': selected by" not in str(info.value)


def test_new_earlier_alias_and_future_file_receive_no_exemption(tmp_path):
    target = tmp_path / "b.md"
    target.write_text("# Body\n")
    policy = _policy(exempt=["b.md"])
    enforce_coverage(tmp_path, policy, set())
    (tmp_path / "a.md").symlink_to(target)
    (tmp_path / "future.md").write_text("# New\n")

    with pytest.raises(CoverageError) as info:
        enforce_coverage(tmp_path, policy, set())

    assert "'a.md': selected by" in str(info.value)
    assert "'future.md': selected by" in str(info.value)
    assert "'b.md': selected by" not in str(info.value)


@pytest.mark.parametrize("exempt", ["missing.md", "./real.md", "*.md", "elsewhere.md"])
def test_exemptions_are_literal_and_unmatched_entries_are_stale(tmp_path, exempt):
    target = tmp_path / "real.md"
    target.write_text("# Body\n")
    (tmp_path / "elsewhere.md").symlink_to(target)

    with pytest.raises(CoverageError, match="stale exemption") as info:
        enforce_coverage(tmp_path, _policy(["real.md"], [exempt]), {target})

    assert repr(exempt) in str(info.value)


def test_glob_metacharacters_in_an_exemption_name_are_literal(tmp_path):
    (tmp_path / "[x].md").write_text("# Literal\n")
    enforce_coverage(tmp_path, _policy(exempt=["[x].md"]), set())


@pytest.mark.parametrize("kind", ["escape", "dangling", "directory", "fifo", "loop"])
@pytest.mark.parametrize("exempt", [False, True])
def test_exemptions_cannot_waive_invalid_selected_paths(tmp_path, kind, exempt):
    root = tmp_path / "project"
    root.mkdir()
    selected = root / "invalid.md"
    if kind == "escape":
        target = tmp_path / "outside.md"
        target.write_text("# Outside\n")
        selected.symlink_to(target)
    elif kind == "dangling":
        selected.symlink_to(root / "missing.md")
    elif kind == "directory":
        target = root / "directory"
        target.mkdir()
        selected.symlink_to(target, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(selected)
    else:
        selected.symlink_to(selected)

    with pytest.raises(CoverageError) as info:
        enforce_coverage(root, _policy(exempt=["invalid.md"] if exempt else []), set())

    assert "'invalid.md': selected by '*.md'" in str(info.value)
    assert "exemptions cannot waive invalid paths" in str(info.value)


@pytest.mark.parametrize("operation", ["resolve", "stat"])
def test_selected_path_inspection_failure_is_a_coverage_error(tmp_path, monkeypatch, operation):
    target = tmp_path / "bad.md"
    target.write_text("# Body\n")
    original = getattr(Path, operation)

    def refuse(path, *args, **kwargs):
        if path == target:
            raise OSError(errno.EACCES, "refused for test")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, refuse)
    with pytest.raises(CoverageError, match="cannot resolve or inspect selected path"):
        enforce_coverage(tmp_path, _policy(exempt=["bad.md"]), {target})


def test_uncovered_and_stale_exemptions_are_reported_together(tmp_path):
    (tmp_path / "uncovered.md").write_text("# Uncovered\n")
    with pytest.raises(CoverageError) as info:
        enforce_coverage(tmp_path, _policy(exempt=["stale.md"]), set())
    assert "'uncovered.md': selected by" in str(info.value)
    assert "'stale.md' matches no selected path" in str(info.value)


def test_an_exclusion_removes_an_otherwise_uncovered_file(tmp_path):
    (tmp_path / "covered.md").write_text("# Covered\n")
    vendored = tmp_path / "vendor"
    vendored.mkdir()
    (vendored / "untracked.md").write_text("# Untracked\n")

    with pytest.raises(CoverageError, match="not enrolled in the loaded lattice"):
        enforce_coverage(tmp_path, _policy(["**/*.md"]), {tmp_path / "covered.md"})

    enforce_coverage(
        tmp_path,
        _policy(["**/*.md"], exclude=["vendor"]),
        {tmp_path / "covered.md"},
    )


@pytest.mark.parametrize("kind", ["escape", "dangling", "directory", "fifo", "loop"])
def test_an_exclusion_removes_an_invalid_selected_path(tmp_path, kind):
    root = tmp_path / "project"
    root.mkdir()
    (root / "covered.md").write_text("# Covered\n")
    selected = root / "invalid.md"
    if kind == "escape":
        target = tmp_path / "outside.md"
        target.write_text("# Outside\n")
        selected.symlink_to(target)
    elif kind == "dangling":
        selected.symlink_to(root / "missing.md")
    elif kind == "directory":
        target = root / "directory"
        target.mkdir()
        selected.symlink_to(target, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(selected)
    else:
        selected.symlink_to(selected)

    with pytest.raises(CoverageError, match="exemptions cannot waive invalid paths"):
        enforce_coverage(root, _policy(), {root / "covered.md"})

    enforce_coverage(root, _policy(exclude=["invalid.md"]), {root / "covered.md"})


def test_an_invalid_selected_path_offers_the_exclusion_remedy(tmp_path):
    (tmp_path / "invalid.md").symlink_to(tmp_path / "missing.md")

    with pytest.raises(CoverageError, match=r"prune it with sidecar_coverage\.exclude") as info:
        enforce_coverage(tmp_path, _policy(), set())

    assert "exemptions cannot waive invalid paths" in str(info.value)


def test_an_exclusion_prunes_the_symlinked_directory_that_refused_the_gate(tmp_path):
    skills = tmp_path / "skills"
    (skills / "a").mkdir(parents=True)
    covered = skills / "a" / "SKILL.md"
    covered.write_text("# A\n")
    modules = skills / "web" / "node_modules"
    modules.mkdir(parents=True)
    (modules / "lib").symlink_to(skills, target_is_directory=True)

    with pytest.raises(CoverageError, match="refuses to traverse symlinked directory") as info:
        enforce_coverage(tmp_path, _policy(["skills/**/SKILL.md"]), {covered})

    notes = " ".join(info.value.__notes__)
    assert "prune it with sidecar_coverage.exclude" in notes

    enforce_coverage(
        tmp_path,
        _policy(["skills/**/SKILL.md"], exclude=["skills/**/node_modules"]),
        {covered},
    )
