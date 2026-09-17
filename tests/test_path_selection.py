"""The shared walk retains spellings while enforcing the consumer's traversal policy."""

import errno
import os
from pathlib import Path

import pytest

from doc_lattice import path_selection
from doc_lattice.config import SIDECAR_COVERAGE_EXCLUDE_KEY as _EXCLUDE_KEY
from doc_lattice.error_types import ConfigError, CoverageError, UnreadableDocError
from doc_lattice.link_check import _SELECTION_POLICY as _LINKS
from doc_lattice.link_check import select_link_sources
from doc_lattice.path_selection import (
    Exclusions,
    SelectedPath,
    SelectionPolicy,
    select_paths,
)
from doc_lattice.sidecar_coverage import _POLICY as _COVERAGE


def _exclude(*selectors):
    return Exclusions(_EXCLUDE_KEY, selectors)


class _RefusingEntry:
    """A directory entry whose kind the filesystem refuses to report.

    ``os.DirEntry`` has no public constructor and cannot be instantiated or subclassed, so the
    only way to reach ``_is_directory``'s refusal branch is to hand it a stand-in.
    """

    path = "/project/locked"
    name = "locked"

    def is_dir(self, **_kwargs: bool) -> bool:
        raise OSError(errno.EACCES, "inspection refused")


def test_selection_retains_every_alias_and_sorted_unique_selectors(tmp_path):
    target = tmp_path / "b.md"
    target.write_text("# B\n")
    (tmp_path / "a.md").symlink_to(target)

    selected = select_paths(tmp_path, ["b.md", "*.md", "**/*.md", "*.md"], policy=_COVERAGE)

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


@pytest.mark.parametrize(
    ("policy", "expected"),
    [(_COVERAGE, CoverageError), (_LINKS, ConfigError)],
    ids=["coverage", "links"],
)
def test_directory_inspection_failure_keeps_each_consumer_error_type(policy, expected):
    with pytest.raises(expected, match="could not inspect"):
        path_selection._is_directory(
            _RefusingEntry(),  # ty: ignore[invalid-argument-type]
            policy,
            "locked",
            traverse=True,
        )


def test_an_unresolvable_project_root_is_the_consumer_refusal(tmp_path, monkeypatch):
    def refuse(_self, *_args, **_kwargs):
        raise OSError(errno.EACCES, "resolve refused")

    monkeypatch.setattr(Path, "resolve", refuse)
    with pytest.raises(CoverageError, match=r"project root .* could not be resolved"):
        select_paths(tmp_path, ["*.md"], policy=_COVERAGE)


def test_a_refusing_policy_must_carry_the_note_that_holds_its_remedy():
    with pytest.raises(ValueError, match="requires selector_note"):
        SelectionPolicy(
            key="sidecar_coverage.select",
            purpose="coverage policy",
            error_type=CoverageError,
            refuse_symlink_directories=True,
        )


def test_an_exclusion_prunes_a_matched_file_and_leaves_its_siblings(tmp_path):
    (tmp_path / "keep.md").write_text("# Keep\n")
    (tmp_path / "drop.md").write_text("# Drop\n")

    selected = select_paths(
        tmp_path, ["*.md", "**/*.md"], policy=_COVERAGE, exclude=_exclude("drop.md")
    )

    assert selected == (SelectedPath("keep.md", ("**/*.md", "*.md")),)


@pytest.mark.parametrize("selector", ["**/*.md", "*/SKILL.md", "linked/**/*.md", "**"])
def test_an_exclusion_prunes_the_symlinked_directory_a_selector_would_refuse(tmp_path, selector):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)

    if selector == "linked/**/*.md":
        with pytest.raises(CoverageError, match=f"that {_EXCLUDE_KEY} did not prune"):
            select_paths(tmp_path, [selector], policy=_COVERAGE, exclude=_exclude("linked"))
        return
    assert select_paths(tmp_path, [selector], policy=_COVERAGE, exclude=_exclude("linked")) == (
        SelectedPath("real/SKILL.md", (selector,)),
    )


def test_an_exclusion_on_an_interior_directory_prevents_descent(tmp_path):
    skills = tmp_path / "skills"
    (skills / "a").mkdir(parents=True)
    (skills / "a" / "SKILL.md").write_text("# A\n")
    modules = skills / "web" / "node_modules"
    modules.mkdir(parents=True)
    (modules / "lib").symlink_to(skills, target_is_directory=True)

    with pytest.raises(CoverageError, match="symlinked directory"):
        select_paths(tmp_path, ["skills/**/SKILL.md"], policy=_COVERAGE)

    assert select_paths(
        tmp_path,
        ["skills/**/SKILL.md"],
        policy=_COVERAGE,
        exclude=_exclude("skills/**/node_modules"),
    ) == (SelectedPath("skills/a/SKILL.md", ("skills/**/SKILL.md",)),)


def test_an_exclusion_precedes_the_scan_refusal_it_prunes(tmp_path, monkeypatch):
    (tmp_path / "keep.md").write_text("# Keep\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    original = os.scandir

    def refuse(directory):
        if Path(directory) == locked:
            raise OSError(errno.EACCES, "scan refused")
        return original(directory)

    monkeypatch.setattr(os, "scandir", refuse)
    with pytest.raises(CoverageError, match="could not scan"):
        select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE)

    assert select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("locked")) == (
        SelectedPath("keep.md", ("**/*.md",)),
    )


def test_a_contents_shaped_exclusion_cannot_prune_the_directory_itself(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "node_modules").symlink_to(real, target_is_directory=True)

    with pytest.raises(CoverageError, match="symlinked directory"):
        select_paths(
            tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("**/node_modules/**")
        )

    assert select_paths(
        tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("**/node_modules")
    ) == (SelectedPath("real/SKILL.md", ("**/*.md",)),)


def test_an_exclusion_prunes_one_spelling_and_not_its_alias(tmp_path):
    target = tmp_path / "b.md"
    target.write_text("# B\n")
    (tmp_path / "a.md").symlink_to(target)

    assert select_paths(tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("a.md")) == (
        SelectedPath("b.md", ("*.md",)),
    )


@pytest.mark.parametrize(
    ("policy", "expected"),
    [(_COVERAGE, CoverageError), (_LINKS, ConfigError)],
    ids=["coverage", "links"],
)
def test_an_invalid_exclusion_names_the_key_that_carried_it(tmp_path, policy, expected):
    (tmp_path / "a.md").write_text("# A\n")
    with pytest.raises(expected, match=f"{_EXCLUDE_KEY} entry '\\.\\./x'"):
        select_paths(tmp_path, ["*.md"], policy=policy, exclude=_exclude("../x"))


def test_pruning_everything_a_selector_matched_names_the_exclusion_key(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    with pytest.raises(CoverageError, match=f"that {_EXCLUDE_KEY} did not prune"):
        select_paths(tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("*.md"))


def test_excluding_everything_empties_selection_without_pruning_the_root(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    (tmp_path / "sub").mkdir()

    with pytest.raises(CoverageError, match="matches no file"):
        select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("**"))


def test_an_exclusion_prunes_identically_through_adjacent_recursive_segments(tmp_path):
    for name in ("keep", "drop"):
        directory = tmp_path / "a" / name
        directory.mkdir(parents=True)
        (directory / "x.md").write_text("# X\n")

    assert select_paths(
        tmp_path, ["a/**/**/x.md"], policy=_COVERAGE, exclude=_exclude("a/drop")
    ) == (SelectedPath("a/keep/x.md", ("a/**/**/x.md",)),)


def test_an_exclusion_that_prunes_nothing_is_accepted(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")

    assert select_paths(
        tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("absent/**/node_modules")
    ) == (SelectedPath("a.md", ("*.md",)),)
