"""The shared walk retains spellings while enforcing the consumer's traversal policy."""

import errno
import os
from pathlib import Path

import pytest

from doc_lattice import path_selection
from doc_lattice.error_types import ConfigError, CoverageError, UnreadableDocError
from doc_lattice.link_check import select_link_sources
from doc_lattice.path_selection import SelectedPath, SelectionPolicy, select_paths

_COVERAGE = SelectionPolicy(
    key="sidecar_coverage.select",
    purpose="coverage policy",
    error_type=CoverageError,
    refuse_symlink_directories=True,
)


def test_selection_retains_every_alias_and_sorted_unique_selectors(tmp_path):
    target = tmp_path / "b.md"
    target.write_text("# B\n")
    (tmp_path / "a.md").symlink_to(target)

    selected = select_paths(tmp_path, ["b.md", "*.md", "**/*.md", "*.md"])

    assert selected == (
        SelectedPath("a.md", ("**/*.md", "*.md")),
        SelectedPath("b.md", ("**/*.md", "*.md", "b.md")),
    )
    assert select_link_sources(tmp_path, ["*.md"]) == [tmp_path / "a.md"]


@pytest.mark.parametrize("selectors", [["*.md", "missing.md"], ["missing.md", "*.md"]])
def test_a_matching_selector_cannot_hide_an_empty_one(tmp_path, selectors):
    (tmp_path / "covered.md").write_text("# Covered\n")
    with pytest.raises(CoverageError, match=r"'missing\.md' matches no file"):
        select_paths(tmp_path, selectors, policy=_COVERAGE)


@pytest.mark.parametrize("selector", ["**/*.md", "*/SKILL.md", "linked/**/*.md", "**"])
def test_coverage_refuses_traversable_symlink_directories_with_covered_siblings(tmp_path, selector):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)

    with pytest.raises(CoverageError, match="symlinked directory") as info:
        select_paths(tmp_path, [selector], policy=_COVERAGE)

    assert "linked" in str(info.value)
    assert repr(selector) in " ".join(info.value.__notes__)
    if selector == "**":
        with pytest.raises(UnreadableDocError, match="not a regular file"):
            select_link_sources(tmp_path, [selector])
    elif selector != "linked/**/*.md":
        assert select_link_sources(tmp_path, [selector])
    else:
        with pytest.raises(ConfigError, match="matches no file"):
            select_link_sources(tmp_path, [selector])


def test_nontraversed_symlink_directory_does_not_widen_coverage(tmp_path):
    target = tmp_path / "covered.md"
    target.write_text("# Body\n")
    (tmp_path / "unrelated").symlink_to(tmp_path, target_is_directory=True)
    assert select_paths(tmp_path, ["*.md"], policy=_COVERAGE) == (
        SelectedPath("covered.md", ("*.md",)),
    )


def test_scan_failure_cannot_hide_a_subtree(tmp_path, monkeypatch):
    (tmp_path / "locked").mkdir()
    original = os.scandir

    def refuse(directory):
        if Path(directory) == tmp_path / "locked":
            raise OSError(errno.EACCES, "scan refused")
        return original(directory)

    monkeypatch.setattr(os, "scandir", refuse)
    with pytest.raises(CoverageError, match=r"could not scan.*locked"):
        select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE)


def test_directory_inspection_failure_keeps_the_coverage_error_type():
    class RefusingEntry:
        path = "/project/locked"

        def is_dir(self, *, follow_symlinks):
            assert follow_symlinks is False
            raise OSError(errno.EACCES, "inspection refused")

    with pytest.raises(CoverageError, match="could not inspect"):
        # DirEntry cannot be instantiated or subclassed; this stand-in forces its I/O failure.
        path_selection._is_directory(
            RefusingEntry(),  # ty: ignore[invalid-argument-type]
            _COVERAGE,
            traverse=True,
        )
