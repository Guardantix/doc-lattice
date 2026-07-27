"""Tests for the differential shell taint fuzzer."""

import random
import shutil
from pathlib import Path
from runpy import run_path

import pytest

_FUZZER = run_path(str(Path(__file__).parents[1] / "scripts" / "fuzz_shell_taint.py"))
Case = _FUZZER["Case"]
Recipe = _FUZZER["Recipe"]
build_case = _FUZZER["build_case"]
execute = _FUZZER["execute"]
generate = _FUZZER["generate"]
load_baseline = _FUZZER["load_baseline"]
write_baseline = _FUZZER["write_baseline"]
_replace_dimension = _FUZZER["_replace_dimension"]
_trace_runs_marker = _FUZZER["_trace_runs_marker"]
_SELF_CHECK_CASES = _FUZZER["_SELF_CHECK_CASES"]

_BASH = shutil.which("bash")


def _seeded(seed: int) -> random.Random:
    """Return a seeded generator.

    Fuzz reproducibility requires a deterministic sequence rather than cryptographic entropy, so
    the standard generator is the correct choice here.
    """
    return random.Random(seed)  # noqa: S311


@pytest.mark.parametrize(
    "trace",
    [
        "+ doc-lattice reconcile",
        "++ doc-lattice reconcile",
        "+ /usr/bin/doc-lattice reconcile",
        "+ doc_lattice reconcile",
        "+ doc.lattice reconcile",
        "+ DOC-LATTICE reconcile",
        "+ V=x doc-lattice reconcile",
        "+ V=x W=y doc--lattice reconcile",
    ],
    ids=(
        "plain",
        "nested-depth",
        "absolute-path",
        "underscore",
        "dot",
        "uppercase",
        "assignment-prefix",
        "two-assignment-prefixes",
    ),
)
def test_trace_detects_a_marker_command(trace: str) -> None:
    assert _trace_runs_marker(trace)


@pytest.mark.parametrize(
    "trace",
    [
        "+ echo doc-lattice reconcile",
        "+ printf %s doc-lattice",
        "+ V=doc-lattice",
        "+ cat doc-lattice.txt",
        "",
        "+ doclattice reconcile",
        "+ doc-lattice-runner reconcile",
    ],
    ids=(
        "marker-as-argument",
        "marker-printed",
        "marker-assigned",
        "marker-in-filename",
        "empty",
        "no-separator",
        "longer-name",
    ),
)
def test_trace_ignores_a_command_that_is_not_the_marker(trace: str) -> None:
    assert not _trace_runs_marker(trace)


def test_generation_is_deterministic_for_a_seed() -> None:
    first = generate(_seeded(7), 40)
    second = generate(_seeded(7), 40)

    assert [case.script for case in first] == [case.script for case in second]


def test_generation_yields_distinct_recipes() -> None:
    cases = generate(_seeded(11), 60)

    assert len({case.recipe for case in cases}) == len(cases)


def test_generation_covers_both_marker_bearing_and_inert_bodies() -> None:
    cases = generate(_seeded(3), 120)
    bearing = {case.recipe.marker_bearing for case in cases}

    assert bearing == {True, False}


def test_build_case_leaves_no_unexpanded_placeholder() -> None:
    cases = generate(_seeded(5), 80)

    assert all("@" not in case.script for case in cases)


def test_build_case_composes_the_marker_across_producer_and_sink() -> None:
    recipe = Recipe(
        producer="plain",
        carrier="braced",
        sink="eval",
        wrapper="none",
        fragments="doc-|lattice",
        marker_bearing=True,
    )

    case = build_case(recipe, tag=0)

    assert case.script == 'v0=doc-\neval "${v0}lattice reconcile"'


def test_replace_dimension_changes_one_field_only() -> None:
    recipe = Recipe(
        producer="plain",
        carrier="braced",
        sink="eval",
        wrapper="none",
        fragments="doc-|lattice",
        marker_bearing=True,
    )

    replaced = _replace_dimension(recipe, "sink", "bash-c")

    assert replaced.sink == "bash-c"
    assert replaced.producer == recipe.producer
    assert replaced.carrier == recipe.carrier
    assert replaced.wrapper == recipe.wrapper
    assert replaced.fragments == recipe.fragments


def test_baseline_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "baseline.tsv"
    signatures = {("plain", "braced", "eval", "none"), ("declare", "direct", "bash-c", "function")}

    write_baseline(path, signatures)

    assert load_baseline(path) == signatures


def test_baseline_ignores_comments_and_returns_empty_for_a_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "absent.tsv"
    commented = tmp_path / "commented.tsv"
    commented.write_text("# header\n\nplain\tbraced\teval\tnone\n", encoding="utf-8")

    assert load_baseline(missing) == set()
    assert load_baseline(None) == set()
    assert load_baseline(commented) == {("plain", "braced", "eval", "none")}


@pytest.mark.skipif(_BASH is None, reason="bash is required for differential execution")
@pytest.mark.parametrize(
    ("_label", "script", "expected"),
    _SELF_CHECK_CASES,
    ids=[row[0] for row in _SELF_CHECK_CASES],
)
def test_execution_detector_matches_known_bash_behavior(
    _label: str,
    script: str,
    expected: bool,
) -> None:
    """The differential oracle must be right in both directions, including child processes."""
    placeholder = Recipe("", "", "", "", "", marker_bearing=True)

    executed, timed_out = execute(Case(script=script, recipe=placeholder), _BASH, 20)

    assert not timed_out
    assert executed is expected
