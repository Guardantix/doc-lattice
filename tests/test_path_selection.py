"""The shared walk returns spellings and consumer-neutral refusal records."""

import errno
import os
from contextlib import nullcontext
from pathlib import Path

import pytest

from doc_lattice.config import CoverageExclusion, SidecarCoverage
from doc_lattice.error_types import ConfigError, CoverageError, UnreadableDocError
from doc_lattice.link_check import select_link_sources
from doc_lattice.path_selection import (
    Exclusions,
    SelectedPath,
    SelectionPolicy,
    SelectionRefusal,
    select_paths,
)
from doc_lattice.sidecar_coverage import enforce_coverage

_COVERAGE = SelectionPolicy(refuse_symlink_directories=True)
_LINKS = SelectionPolicy()


def _exclude(*selectors):
    return Exclusions(selectors)


def _refusal(error: ValueError) -> SelectionRefusal:
    assert len(error.args) == 1
    refusal = error.args[0]
    assert isinstance(refusal, SelectionRefusal)
    return refusal


def _coverage(*selectors, exclude=()):
    return SidecarCoverage(
        select=list(selectors),
        **(
            {
                "exclude": [
                    CoverageExclusion(select=entry, reason="not in corpus") for entry in exclude
                ]
            }
            if exclude
            else {}
        ),
    )


class _RefusingEntry:
    """Stand-in for a directory entry whose kind the filesystem refuses to report."""

    path = "/project/locked"
    name = "locked"

    def is_dir(self, **_kwargs: bool) -> bool:
        raise OSError(errno.EACCES, "inspection refused")


def test_selection_retains_aliases_and_sorted_unique_selectors(tmp_path):
    target = tmp_path / "b.md"
    target.write_text("# B\n")
    (tmp_path / "a.md").symlink_to(target)
    result = select_paths(tmp_path, ["b.md", "*.md", "**/*.md", "*.md"], policy=_COVERAGE)
    assert result.paths == (
        SelectedPath("a.md", ("**/*.md", "*.md")),
        SelectedPath("b.md", ("**/*.md", "*.md", "b.md")),
    )
    assert result.refusals == ()
    assert select_link_sources(tmp_path, ["*.md"]) == [tmp_path / "a.md"]


@pytest.mark.parametrize("selectors", [["*.md", "missing.md"], ["missing.md", "*.md"]])
def test_a_matching_selector_cannot_hide_an_empty_one(tmp_path, selectors):
    (tmp_path / "covered.md").write_text("# Covered\n")
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, selectors, policy=_COVERAGE)
    refusal = _refusal(info.value)
    assert (refusal.kind, refusal.spelling) == ("no-match", "missing.md")


@pytest.mark.parametrize("selector", ["**/*.md", "*/SKILL.md", "linked/**/*.md", "**"])
def test_symlink_refusal_has_kind_spelling_and_selector(tmp_path, selector):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    result = select_paths(tmp_path, [selector], policy=_COVERAGE)
    expected = [("symlink-directory", "linked", selector)]
    if selector == "linked/**/*.md":
        expected.append(("no-match", selector, None))
    assert [(item.kind, item.spelling, item.selector) for item in result.refusals] == expected
    if selector == "**":
        with pytest.raises(UnreadableDocError, match="not a regular file"):
            select_link_sources(tmp_path, [selector])
    elif selector == "linked/**/*.md":
        with pytest.raises(ConfigError, match="matches no file"):
            select_link_sources(tmp_path, [selector])
    else:
        assert select_link_sources(tmp_path, [selector])


def test_nontraversed_symlink_has_no_refusal(tmp_path):
    (tmp_path / "covered.md").write_text("# Body\n")
    (tmp_path / "unrelated").symlink_to(tmp_path, target_is_directory=True)
    result = select_paths(tmp_path, ["*.md"], policy=_COVERAGE)
    assert result.paths == (SelectedPath("covered.md", ("*.md",)),)
    assert result.refusals == ()


def test_adjacent_recursive_segments_report_each_symlink_once(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)

    result = select_paths(tmp_path, ["**/**/SKILL.md"], policy=_COVERAGE)

    assert [(item.kind, item.spelling, item.selector) for item in result.refusals] == [
        ("symlink-directory", "linked", "**/**/SKILL.md")
    ]


def test_scan_failure_is_structured_and_consumers_keep_error_types(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    original = os.scandir

    def refuse(directory):
        if Path(directory) == locked:
            raise OSError(errno.EACCES, "scan refused")
        return original(directory)

    monkeypatch.setattr(os, "scandir", refuse)
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE)
    refusal = _refusal(info.value)
    assert (refusal.kind, refusal.selector) == ("scan-failed", "**/*.md")
    with pytest.raises(CoverageError, match="could not scan"):
        enforce_coverage(tmp_path, _coverage("**/*.md"), set())
    with pytest.raises(ConfigError, match="could not scan"):
        select_link_sources(tmp_path, ["**/*.md"])


def test_inspection_failure_is_structured_and_consumers_keep_error_types(tmp_path, monkeypatch):
    original = os.scandir

    def refuse(directory):
        if Path(directory) == tmp_path:
            return nullcontext(iter([_RefusingEntry()]))
        return original(directory)

    monkeypatch.setattr(os, "scandir", refuse)
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, ["*"], policy=_COVERAGE)
    refusal = _refusal(info.value)
    assert (refusal.kind, refusal.spelling) == ("inspect-failed", "/project/locked")
    with pytest.raises(CoverageError, match="could not inspect"):
        enforce_coverage(tmp_path, _coverage("*"), set())
    with pytest.raises(ConfigError, match="could not inspect"):
        select_link_sources(tmp_path, ["*"])


def test_unresolvable_root_is_structured_and_coverage_maps_it(tmp_path, monkeypatch):
    def refuse(_self, *_args, **_kwargs):
        raise OSError(errno.EACCES, "resolve refused")

    monkeypatch.setattr(Path, "resolve", refuse)
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, ["*.md"], policy=_COVERAGE)
    assert _refusal(info.value).kind == "root-unresolved"
    with pytest.raises(CoverageError, match=r"project root .* could not be resolved"):
        enforce_coverage(tmp_path, _coverage("*.md"), set())


def test_exclusions_prune_files_and_symlinked_directories(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    (tmp_path / "drop.md").write_text("# Drop\n")
    result = select_paths(
        tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("linked", "drop.md")
    )
    assert result.paths == (SelectedPath("real/SKILL.md", ("**/*.md",)),)
    assert result.refusals == ()


def test_exclusion_prunes_one_alias_spelling_only(tmp_path):
    target = tmp_path / "b.md"
    target.write_text("# B\n")
    (tmp_path / "a.md").symlink_to(target)
    result = select_paths(tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("a.md"))
    assert result.paths == (SelectedPath("b.md", ("*.md",)),)


def test_exclusion_precedes_scan_refusal(tmp_path, monkeypatch):
    (tmp_path / "keep.md").write_text("# Keep\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    original = os.scandir

    def refuse(directory):
        if Path(directory) == locked:
            raise OSError(errno.EACCES, "scan refused")
        return original(directory)

    monkeypatch.setattr(os, "scandir", refuse)
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE)
    assert _refusal(info.value).kind == "scan-failed"
    result = select_paths(tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("locked"))
    assert result.paths == (SelectedPath("keep.md", ("**/*.md",)),)


def test_pruning_every_match_refuses_the_selector(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("*.md"))
    refusal = _refusal(info.value)
    assert (refusal.kind, refusal.pruned) == ("no-match", True)


def test_interior_exclusion_prevents_descent(tmp_path):
    skills = tmp_path / "skills"
    (skills / "a").mkdir(parents=True)
    (skills / "a" / "SKILL.md").write_text("# A\n")
    modules = skills / "web" / "node_modules"
    modules.mkdir(parents=True)
    (modules / "lib").symlink_to(skills, target_is_directory=True)
    result = select_paths(tmp_path, ["skills/**/SKILL.md"], policy=_COVERAGE)
    assert [item.spelling for item in result.refusals] == ["skills/web/node_modules/lib"]
    pruned = select_paths(
        tmp_path,
        ["skills/**/SKILL.md"],
        policy=_COVERAGE,
        exclude=_exclude("skills/**/node_modules"),
    )
    assert pruned.paths == (SelectedPath("skills/a/SKILL.md", ("skills/**/SKILL.md",)),)
    assert pruned.refusals == ()


def test_contents_exclusion_does_not_prune_directory_itself(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_text("# Covered\n")
    (tmp_path / "node_modules").symlink_to(real, target_is_directory=True)
    result = select_paths(
        tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("**/node_modules/**")
    )
    assert [item.spelling for item in result.refusals] == ["node_modules"]
    pruned = select_paths(
        tmp_path, ["**/*.md"], policy=_COVERAGE, exclude=_exclude("**/node_modules")
    )
    assert pruned.refusals == ()


def test_invalid_exclusion_is_structured_and_coverage_names_its_key(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    with pytest.raises(ValueError, match="SelectionRefusal") as info:
        select_paths(tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("../x"))
    refusal = _refusal(info.value)
    assert (refusal.kind, refusal.source) == ("invalid-selector", "exclude")
    direct_policy = SidecarCoverage.model_construct(
        select=["*.md"],
        exclude=[CoverageExclusion.model_construct(select="../x", reason="not in corpus")],
    )
    with pytest.raises(CoverageError, match=r"sidecar_coverage.exclude entry '\.\./x'"):
        enforce_coverage(tmp_path, direct_policy, set())


def test_pruning_everything_names_exclusion_key(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    with pytest.raises(CoverageError, match=r"that sidecar_coverage\.exclude did not prune"):
        enforce_coverage(tmp_path, _coverage("*.md", exclude=("*.md",)), set())


def test_adjacent_recursive_segments_keep_pruning_consistent(tmp_path):
    for name in ("keep", "drop"):
        directory = tmp_path / "a" / name
        directory.mkdir(parents=True)
        (directory / "x.md").write_text("# X\n")
    result = select_paths(tmp_path, ["a/**/**/x.md"], policy=_COVERAGE, exclude=_exclude("a/drop"))
    assert result.paths == (SelectedPath("a/keep/x.md", ("a/**/**/x.md",)),)


def test_exclusion_that_prunes_nothing_is_accepted(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    result = select_paths(
        tmp_path, ["*.md"], policy=_COVERAGE, exclude=_exclude("absent/**/node_modules")
    )
    assert result.paths == (SelectedPath("a.md", ("*.md",)),)


def test_selection_policy_has_only_the_behavior_flag():
    assert SelectionPolicy() == _LINKS
    assert SelectionPolicy(refuse_symlink_directories=True) == _COVERAGE
