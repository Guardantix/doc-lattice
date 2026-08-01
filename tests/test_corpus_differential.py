"""Behavior tests for the recurring checkpoint corpus differential."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "scripts/corpus_differential.py"
_INVENTORY = _ROOT / "tests/fixtures/github_ci_checkpoint/replay_inventory.json"
_ACKNOWLEDGEMENTS = _ROOT / "tests/fixtures/corpus_differential_acknowledgements.json"

_TARGETED_OPTION = "--split-string="
_EARLY_REFUSAL_ANCHOR = "    def scan(self) -> tuple[_Invocation, ...]:\n"
_EARLY_REFUSAL = (
    '        if "--split-string=" in self.source:\n'
    "            raise _ShellScanIncomplete(\n"
    '                GuardRefusal("scanner.round-six.early-refusal", "targeted early refusal")\n'
    "            )\n"
)


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("corpus_differential", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _case(case_id: str, source: str) -> object:
    return tool.CorpusCase(case_id=case_id, digest=tool.digest_of(source), source=source)


def _scored(source: str, verdict: str, case_id: str = "case-1") -> dict[str, str]:
    return {
        "id": case_id,
        "sha256": tool.digest_of(source),
        "source": source,
        "verdict": verdict,
    }


_BASE_SCANNER = "/somewhere/base/src/doc_lattice/github_ci/shell_scanner.py"
_CANDIDATE_SCANNER = "/somewhere/candidate/src/doc_lattice/github_ci/shell_scanner.py"


def _record_document(
    cases: list[dict[str, str]],
    corpus: str = "corpus-digest",
    source: str = _BASE_SCANNER,
) -> dict:
    """Return a synthetic record carrying the provenance and the scale a comparison expects."""
    return {
        "schema": tool.SCHEMA,
        "scanner_source": source,
        "seeds": list(tool.SEEDS),
        "iterations": tool.ITERATIONS,
        "corpus_sha256": corpus,
        "count": len(cases),
        "cases": cases,
    }


def _candidate_document(cases: list[dict[str, str]], corpus: str = "corpus-digest") -> dict:
    return _record_document(cases, corpus, source=_CANDIDATE_SCANNER)


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _revision(tmp_path: Path, name: str, *, mutate: bool) -> Path:
    """Copy the guard package into a private root, optionally carrying the round-6 edit."""
    root = tmp_path / name
    root.mkdir(parents=True)
    shutil.copytree(
        _ROOT / "src",
        root / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if mutate:
        scanner = root / "src/doc_lattice/github_ci/shell_scanner.py"
        text = scanner.read_text(encoding="utf-8")
        assert _EARLY_REFUSAL_ANCHOR in text
        scanner.write_text(
            text.replace(_EARLY_REFUSAL_ANCHOR, _EARLY_REFUSAL_ANCHOR + _EARLY_REFUSAL, 1),
            encoding="utf-8",
        )
    return root


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - controlled tool path and arguments
        [sys.executable, str(_TOOL), *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=_ROOT,
    )


def _replay(root: Path, out: Path, *, seeds: str = "", iterations: int = 0) -> Path:
    completed = _run(
        "record",
        "--scanner-root",
        str(root),
        "--out",
        str(out),
        "--inventory",
        str(_INVENTORY),
        "--seeds",
        seeds,
        "--iterations",
        str(iterations),
    )
    assert completed.returncode == 0, completed.stderr
    return out


def test_inventory_cases_name_every_frozen_entry():
    document = json.loads(_INVENTORY.read_text(encoding="utf-8"))

    cases = tool.inventory_cases(_INVENTORY)

    assert len(cases) == document["count"]
    assert [case.case_id for case in cases] == [entry["id"] for entry in document["entries"]]


def test_inventory_cases_refuse_an_entry_whose_digest_does_not_match_its_source(tmp_path):
    inventory = _write(
        tmp_path / "inventory.json",
        {"count": 1, "entries": [{"id": "replay-0001", "sha256": "0" * 64, "source": "true"}]},
    )

    with pytest.raises(ValueError, match="replay-0001"):
        tool.inventory_cases(inventory)


def test_fuzz_cases_are_fixed_by_the_seeds_they_are_drawn_with():
    generate = tool.fuzz_generator()

    first = tool.fuzz_cases(generate, (1,), 5)
    again = tool.fuzz_cases(generate, (1,), 5)
    other = tool.fuzz_cases(generate, (2,), 5)

    assert [case.digest for case in first] == [case.digest for case in again]
    assert [case.digest for case in first] != [case.digest for case in other]
    assert [case.case_id for case in first] == [f"fuzz-0001-{index:05d}" for index in range(1, 6)]


def test_deduplicate_keeps_the_first_name_for_a_repeated_script():
    cases = [_case("replay-0001", "true"), _case("fuzz-0001-00001", "true"), _case("b", "false")]

    unique = tool.deduplicate(cases)

    assert [case.case_id for case in unique] == ["replay-0001", "b"]


def test_corpus_digest_binds_the_order_scripts_are_replayed_in():
    first = _case("a", "true")
    second = _case("b", "false")

    assert tool.corpus_digest([first, second]) != tool.corpus_digest([second, first])


def test_the_default_corpus_is_the_scale_the_one_off_run_covered():
    # The one-off differential run during PR #179's review covered roughly twenty thousand
    # scripts. The generated half saturates on distinct verdict labels within a few hundred draws,
    # measured at seven labels and five guard origins from two hundred draws onwards, so what the
    # scale buys is sensitivity per script: a targeted refusal keyed on a rare shape has to meet a
    # script carrying that shape, and every draw dropped is a script that cannot report it.
    assert tool.SEEDS == (1, 2, 3, 4)
    assert tool.ITERATIONS == 5000
    assert len(tool.SEEDS) * tool.ITERATIONS >= 20000


def test_build_corpus_puts_the_frozen_inventory_ahead_of_the_generated_bodies(tmp_path):
    inventory = _write(
        tmp_path / "inventory.json",
        {
            "count": 1,
            "entries": [
                {"id": "replay-0001", "sha256": tool.digest_of("true"), "source": "true"},
            ],
        },
    )

    corpus = tool.build_corpus(
        inventory,
        generate=tool.fuzz_generator(),
        seeds=(1,),
        iterations=3,
    )

    assert next(case.case_id for case in corpus) == "replay-0001"
    assert len(corpus) == 4


def test_verdict_label_carries_the_refusing_guard_identity():
    result = SimpleNamespace(
        guard_id="scanner.env-prefix.split-string-long-option",
        incomplete_reason="env split-string option cannot be scanned safely",
        invocations=(),
    )

    assert tool.verdict_label(result) == "guard:scanner.env-prefix.split-string-long-option"


def test_verdict_label_separates_the_analysis_refusal_from_a_guard_refusal():
    result = SimpleNamespace(guard_id=None, incomplete_reason="marker flow", invocations=())

    assert tool.verdict_label(result) == "marker-detected"


def test_verdict_label_records_which_invocations_were_certified():
    result = SimpleNamespace(
        guard_id=None,
        incomplete_reason=None,
        invocations=(("check", True), ("lint", False)),
    )

    assert tool.verdict_label(result) == 'certified[["check", "True"],["lint", "False"]]'


def test_verdict_label_keeps_two_invocation_shapes_apart_when_a_part_carries_a_separator():
    # The label is the only thing a comparison reads, so two different invocation tuples that
    # spelled the same label would hide the transition between them rather than report it.
    split = SimpleNamespace(
        guard_id=None, incomplete_reason=None, invocations=(("check",), ("lint",))
    )
    joined = SimpleNamespace(
        guard_id=None, incomplete_reason=None, invocations=(('check"],["lint',),)
    )

    assert tool.verdict_label(split) != tool.verdict_label(joined)


def test_check_projection_refuses_a_result_missing_part_of_the_public_surface():
    with pytest.raises(ValueError, match="guard_id"):
        tool.check_projection(SimpleNamespace(invocations=(), incomplete_reason=None))


def test_replay_refuses_an_empty_corpus_rather_than_scoring_nothing():
    # A corpus that reached zero scripts never reaches the projection check either, so a revision
    # that dropped part of the public surface would be reported as a clean differential.
    with pytest.raises(ValueError, match="holds no scripts"):
        tool.replay([], lambda _source: SimpleNamespace())


def test_load_scanner_refuses_a_root_that_holds_no_guard_package(tmp_path):
    with pytest.raises(ValueError, match=r"shell_scanner\.py"):
        tool.load_scanner(tmp_path)


def test_read_entries_names_the_file_rather_than_raising_the_missing_key(tmp_path):
    # A bare KeyError reports the key alone, which names neither the file that was malformed nor
    # what was wrong with it, and the sibling field reader already refuses the same shape by name.
    path = _write(tmp_path / "acknowledged.json", {"acknowledgments": []})

    with pytest.raises(ValueError, match="records no acknowledgements"):
        tool.load_acknowledgements(path)


def test_record_refuses_a_revision_whose_public_entry_point_moved(tmp_path):
    # A renamed entry point is a refusal naming the name, not an AttributeError out of the middle
    # of a twenty-thousand script replay, where it reads like the divergence the gate reports.
    root = _revision(tmp_path, "renamed", mutate=False)
    scanner = root / "src/doc_lattice/github_ci/shell_scanner.py"
    scanner.write_text(
        scanner.read_text(encoding="utf-8").replace(
            "def scan_doc_lattice_invocations(", "def scan_invocations_renamed(", 1
        ),
        encoding="utf-8",
    )

    completed = _run(
        "record",
        "--scanner-root",
        str(root),
        "--out",
        str(tmp_path / "verdicts.json"),
        "--inventory",
        str(_INVENTORY),
        "--seeds",
        "",
        "--iterations",
        "0",
    )

    assert completed.returncode == tool.EXIT_REFUSED
    assert "scan_doc_lattice_invocations" in completed.stderr
    assert "corpus differential refused" in completed.stderr


def test_align_refuses_records_that_scored_different_corpora():
    base = _record_document([_scored("true", "certified[]")], corpus="a" * 64)
    candidate = _candidate_document([_scored("true", "certified[]")], corpus="b" * 64)

    with pytest.raises(ValueError, match="different corpora"):
        tool.align(base, candidate)


def test_check_distinct_revisions_refuses_one_revision_recorded_twice():
    # `record` proves which file it scored, and dropping that at comparison time would let two
    # runs against the same checkout report a clean differential for a change nothing replayed.
    base = _record_document([_scored("true", "certified[]")])
    candidate = _record_document([_scored("true", "certified[]")])

    with pytest.raises(ValueError, match="replayed twice"):
        tool.check_distinct_revisions(base, candidate)


def test_check_distinct_revisions_accepts_two_revisions_scored_where_they_live():
    base = _record_document([_scored("true", "certified[]")])
    candidate = _candidate_document([_scored("true", "certified[]")])

    tool.check_distinct_revisions(base, candidate)


def test_check_corpus_scale_refuses_a_record_drawn_below_the_pin():
    # The scale is a command line argument on `record`, so pinning the module constants does not
    # reach a diff that shrinks it; both sides would agree over a corpus too small to carry the
    # shape the same diff withdrew.
    base = _record_document([_scored("true", "certified[]")])
    candidate = _candidate_document([_scored("true", "certified[]")])
    candidate["iterations"] = 1

    with pytest.raises(ValueError, match="rather than the pinned"):
        tool.check_corpus_scale(base, candidate)


def test_check_corpus_scale_refuses_a_record_drawn_with_other_seeds():
    base = _record_document([_scored("true", "certified[]")])
    base["seeds"] = [1]
    candidate = _candidate_document([_scored("true", "certified[]")])

    with pytest.raises(ValueError, match="rather than the pinned"):
        tool.check_corpus_scale(base, candidate)


def test_check_corpus_scale_refuses_a_record_that_names_no_scale():
    base = _record_document([_scored("true", "certified[]")])
    del base["seeds"]
    candidate = _candidate_document([_scored("true", "certified[]")])

    with pytest.raises(ValueError, match="does not name the corpus scale"):
        tool.check_corpus_scale(base, candidate)


def test_check_corpus_scale_refuses_a_record_that_holds_fewer_cases_than_it_counts():
    # The count is what a run reports and what a reader takes the corpus to have been; a record
    # naming one number and carrying another describes a corpus it did not score.
    base = _record_document([_scored("true", "certified[]")])
    candidate = _candidate_document([_scored("true", "certified[]")])
    candidate["count"] = 20580

    with pytest.raises(ValueError, match="not the corpus it scored"):
        tool.check_corpus_scale(base, candidate)


def test_check_corpus_drawn_refuses_a_seed_that_drew_nothing():
    # The scale a record names is what the run was asked for, not what it drew. A generator that
    # collapses to the first seed leaves both records declaring the pin over a corpus that carries
    # a quarter of it, and both sides collapse alike, so the comparison agrees with itself.
    cases = [_case(f"fuzz-0001-{index:05d}", f"echo {index}") for index in range(1, 11)]

    with pytest.raises(ValueError, match="seed"):
        tool.check_corpus_drawn(cases, (1, 2), 5)


def test_check_corpus_drawn_refuses_a_generated_half_below_one_seeds_draws():
    cases = [
        _case("fuzz-0001-00001", "echo one"),
        _case("fuzz-0002-00001", "echo two"),
    ]

    with pytest.raises(ValueError, match="below one seed's worth"):
        tool.check_corpus_drawn(cases, (1, 2), 5)


def test_check_corpus_drawn_accepts_the_scale_a_record_run_actually_draws():
    cases = [_case("replay-0001", "true")] + [
        _case(f"fuzz-{seed:04d}-{index:05d}", f"echo {seed} {index}")
        for seed in (1, 2)
        for index in range(1, 6)
    ]

    tool.check_corpus_drawn(cases, (1, 2), 5)


def test_check_corpus_retained_refuses_a_candidate_that_dropped_a_frozen_script(tmp_path):
    candidate = _record_document([_scored("true", "certified[]")])
    inventory = _write(
        tmp_path / "inventory.json",
        {
            "count": 2,
            "entries": [
                {"id": "replay-0001", "sha256": tool.digest_of("true"), "source": "true"},
                {"id": "replay-0002", "sha256": tool.digest_of("false"), "source": "false"},
            ],
        },
    )

    with pytest.raises(ValueError, match="replay-0002"):
        tool.check_corpus_retained(candidate, inventory)


def test_check_corpus_retained_accepts_a_corpus_that_only_grew(tmp_path):
    candidate = _record_document(
        [_scored("true", "certified[]"), _scored("false", "certified[]", case_id="case-2")]
    )
    inventory = _write(
        tmp_path / "inventory.json",
        {
            "count": 1,
            "entries": [{"id": "replay-0001", "sha256": tool.digest_of("true"), "source": "true"}],
        },
    )

    tool.check_corpus_retained(candidate, inventory)


def test_divergences_pair_each_script_with_both_verdicts():
    base = _record_document([_scored("true", "certified[]"), _scored("false", "guard:a", "c2")])
    candidate = _candidate_document([_scored("true", "guard:b"), _scored("false", "guard:a", "c2")])

    found = tool.divergences(base, candidate)

    assert [(item.case_id, item.base, item.candidate) for item in found] == [
        ("case-1", "certified[]", "guard:b")
    ]


def test_transition_counts_group_a_systematic_change_into_one_row():
    found = [
        tool.Divergence("a", "d1", "s1", "certified[]", "guard:x"),
        tool.Divergence("b", "d2", "s2", "certified[]", "guard:x"),
        tool.Divergence("c", "d3", "s3", "guard:y", "guard:x"),
    ]

    assert tool.transition_counts(found) == [
        (("certified[]", "guard:x"), 2),
        (("guard:y", "guard:x"), 1),
    ]


def test_report_covers_only_the_transition_an_acknowledgement_names(capsys):
    found = [tool.Divergence("a", "d1", "s1", "certified[]", "guard:x")]
    matching = tool.Acknowledgement("d1", "certified[]", "guard:x", "intended by issue #182")
    other = tool.Acknowledgement("d1", "certified[]", "guard:z", "a different transition")

    assert tool.report(found, [matching])[0] == []
    assert tool.report(found, [other])[0] == found
    assert "unacknowledged" in capsys.readouterr().out


def test_report_names_an_acknowledgement_nothing_diverged_the_way_it_describes(capsys):
    stale = tool.Acknowledgement("d9", "certified[]", "guard:x", "landed two releases ago")

    unacknowledged, unmatched = tool.report([], [stale])

    assert unacknowledged == []
    assert unmatched == [stale]
    assert "stale acknowledgement" in capsys.readouterr().out


def test_load_acknowledgements_refuses_an_entry_that_declares_no_reason(tmp_path):
    path = _write(
        tmp_path / "acknowledged.json",
        {
            "acknowledgements": [
                {
                    "sha256": "d1",
                    "base_verdict": "certified[]",
                    "candidate_verdict": "guard:x",
                    "reason": "   ",
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="no reason"):
        tool.load_acknowledgements(path)


def test_load_acknowledgements_refuses_a_reason_that_is_not_text(tmp_path):
    path = _write(
        tmp_path / "acknowledged.json",
        {
            "acknowledgements": [
                {
                    "sha256": "d1",
                    "base_verdict": "certified[]",
                    "candidate_verdict": "guard:x",
                    "reason": 5,
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="not as text"):
        tool.load_acknowledgements(path)


def test_load_acknowledgements_refuses_an_entry_that_is_not_an_object(tmp_path):
    path = _write(tmp_path / "acknowledged.json", {"acknowledgements": ["d1"]})

    with pytest.raises(ValueError, match="where an object carrying"):
        tool.load_acknowledgements(path)


def test_load_acknowledgements_refuses_an_entry_that_names_no_digest(tmp_path):
    path = _write(
        tmp_path / "acknowledged.json",
        {"acknowledgements": [{"base_verdict": "certified[]", "candidate_verdict": "guard:x"}]},
    )

    with pytest.raises(ValueError, match="carrying no sha256"):
        tool.load_acknowledgements(path)


def test_compare_refuses_a_malformed_input_as_a_refusal_rather_than_as_divergence(tmp_path, capsys):
    # A file of the wrong shape means the comparison could not run. Reporting that as the exit
    # status divergence uses would read as "the candidate moved a verdict" in the job log.
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "certified[]")])
    )
    acknowledged = _write(tmp_path / "acknowledged.json", [])

    status = tool.main(
        [
            "compare",
            "--base",
            str(base),
            "--candidate",
            str(candidate),
            "--no-corpus-floor",
            "--acknowledged",
            str(acknowledged),
        ]
    )

    assert status == tool.EXIT_REFUSED
    assert "corpus differential refused" in capsys.readouterr().err


def test_load_record_refuses_a_case_list_of_the_wrong_shape(tmp_path):
    document = _record_document([])
    document["cases"] = "every case"
    path = _write(tmp_path / "base.json", document)

    with pytest.raises(ValueError, match="cases"):
        tool.load_record(path)


def test_write_acknowledgements_drafts_every_divergence_with_the_reason_left_to_the_author(
    tmp_path,
):
    # A scanner fix that legitimately moves thousands of verdicts is not transcribed by hand, and
    # a gate nobody can satisfy for an intended change is a gate that gets switched off. The draft
    # still refuses on read until a reason is written, so it declares nothing on its own.
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "guard:x")])
    )
    draft = tmp_path / "draft.json"

    status = tool.main(
        [
            "compare",
            "--base",
            str(base),
            "--candidate",
            str(candidate),
            "--no-corpus-floor",
            "--write-acknowledgements",
            str(draft),
        ]
    )

    assert status == tool.EXIT_DIVERGED
    written = json.loads(draft.read_text(encoding="utf-8"))["acknowledgements"]
    assert [entry["base_verdict"] for entry in written] == ["certified[]"]
    assert [entry["candidate_verdict"] for entry in written] == ["guard:x"]
    assert [entry["reason"] for entry in written] == [""]
    with pytest.raises(ValueError, match="no reason"):
        tool.load_acknowledgements(draft)


def test_a_written_draft_keeps_the_reason_already_on_file_and_drops_a_stale_entry():
    found = [tool.Divergence("a", "d1", "s1", "certified[]", "guard:x")]
    current = tool.Acknowledgement("d1", "certified[]", "guard:x", "intended by issue #182")
    stale = tool.Acknowledgement("d9", "certified[]", "guard:x", "landed two releases ago")

    document = tool.acknowledgement_document(found, [current, stale])

    assert document == {
        "acknowledgements": [
            {
                "sha256": "d1",
                "base_verdict": "certified[]",
                "candidate_verdict": "guard:x",
                "reason": "intended by issue #182",
            }
        ]
    }


def test_a_written_draft_keeps_an_unmatched_entry_when_the_corpus_was_shrunk():
    # The comparison does not call an entry stale under a shrunken corpus, because the script it
    # names may simply not have been drawn. Dropping it from the file it rewrites would delete the
    # reviewer's reason on the strength of exactly that replay.
    found = [tool.Divergence("a", "d1", "s1", "certified[]", "guard:x")]
    current = tool.Acknowledgement("d1", "certified[]", "guard:x", "intended by issue #182")
    undrawn = tool.Acknowledgement("d9", "certified[]", "guard:y", "covered by a script not drawn")

    document = tool.acknowledgement_document(found, [current, undrawn], drop_stale=False)

    assert document["acknowledgements"] == [
        {
            "sha256": "d1",
            "base_verdict": "certified[]",
            "candidate_verdict": "guard:x",
            "reason": "intended by issue #182",
        },
        {
            "sha256": "d9",
            "base_verdict": "certified[]",
            "candidate_verdict": "guard:y",
            "reason": "covered by a script not drawn",
        },
    ]


def test_check_write_target_refuses_rewriting_a_file_the_comparison_did_not_read(tmp_path):
    # The written document carries only the reasons the comparison was handed, so a rewrite of a
    # file nobody read replaces every reason with an empty one, and the next read then refuses the
    # file for carrying none.
    existing = _write(tmp_path / "acknowledged.json", {"acknowledgements": []})

    with pytest.raises(ValueError, match="did not read"):
        tool.check_write_target("", str(existing))


def test_check_write_target_accepts_the_regeneration_the_documented_command_spells(tmp_path):
    existing = _write(tmp_path / "acknowledged.json", {"acknowledgements": []})

    tool.check_write_target(str(existing), str(existing))
    tool.check_write_target("", str(tmp_path / "draft.json"))


def test_compare_refuses_to_blank_the_reasons_on_a_file_it_was_not_given(tmp_path, capsys):
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "guard:x")])
    )
    acknowledged = _write(
        tmp_path / "acknowledged.json",
        {
            "acknowledgements": [
                {
                    "sha256": tool.digest_of("true"),
                    "base_verdict": "certified[]",
                    "candidate_verdict": "guard:x",
                    "reason": "written by a reviewer",
                }
            ]
        },
    )

    status = _compare(base, candidate, "--write-acknowledgements", str(acknowledged))

    assert status == tool.EXIT_REFUSED
    assert "did not read" in capsys.readouterr().err
    assert "written by a reviewer" in acknowledged.read_text(encoding="utf-8")


def test_the_repository_acknowledgements_read_as_the_comparison_requires():
    """Whatever the repository declares today, the comparison has to be able to read it.

    The fixture is empty while nothing has intentionally moved, but the pull request that moves
    verdicts on purpose is exactly the one that fills it, and the comparison only passes there once
    it is filled. Pinning it empty here would fail that pull request's test job for taking the
    documented path, so what this holds is the shape rather than the count.
    """
    for entry in tool.load_acknowledgements(_ACKNOWLEDGEMENTS):
        assert entry.digest
        assert entry.base
        assert entry.candidate
        assert entry.reason


def _compare(base: Path, candidate: Path, *arguments: str) -> int:
    return tool.main(
        [
            "compare",
            "--base",
            str(base),
            "--candidate",
            str(candidate),
            "--no-corpus-floor",
            *arguments,
        ]
    )


def test_compare_returns_zero_when_both_revisions_score_the_corpus_alike(tmp_path):
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "certified[]")])
    )

    assert _compare(base, candidate) == 0


def test_compare_reports_divergence_as_a_failure(tmp_path, capsys):
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "guard:x")])
    )

    status = _compare(base, candidate)

    assert status == tool.EXIT_DIVERGED
    captured = capsys.readouterr()
    assert "certified[] -> guard:x" in captured.out
    assert "acknowledge each intentional transition" in captured.err


def test_compare_refuses_a_comparison_that_names_no_base_owned_corpus_floor(tmp_path, capsys):
    # Without the base's own inventory nothing stops the scored corpus from having shrunk, so a
    # comparison that omits it is refused rather than reporting a count it cannot stand behind.
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "certified[]")])
    )

    status = tool.main(["compare", "--base", str(base), "--candidate", str(candidate)])

    assert status == tool.EXIT_REFUSED
    assert "no base-owned corpus floor" in capsys.readouterr().err


def test_compare_refuses_one_revision_recorded_twice(tmp_path, capsys):
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _record_document([_scored("true", "certified[]")])
    )

    status = _compare(base, candidate)

    assert status == tool.EXIT_REFUSED
    assert "replayed twice" in capsys.readouterr().err


def test_compare_refuses_records_drawn_below_the_pinned_corpus_scale(tmp_path, capsys):
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    shrunk = _candidate_document([_scored("true", "certified[]")])
    shrunk["seeds"] = [1]
    shrunk["iterations"] = 5
    candidate = _write(tmp_path / "candidate.json", shrunk)

    assert _compare(base, candidate) == tool.EXIT_REFUSED
    assert "rather than the pinned" in capsys.readouterr().err
    assert _compare(base, candidate, "--allow-shrunk-corpus") == tool.EXIT_OK


def test_compare_fails_on_an_acknowledgement_that_matches_no_divergence(tmp_path, capsys):
    # An entry left on file once its transition landed in the base is a standing authorization to
    # make that exact move again, so it has to be removed rather than read out into a green log.
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "certified[]")])
    )
    acknowledged = _write(
        tmp_path / "acknowledged.json",
        {
            "acknowledgements": [
                {
                    "sha256": tool.digest_of("true"),
                    "base_verdict": "certified[]",
                    "candidate_verdict": "guard:x",
                    "reason": "landed two releases ago",
                }
            ]
        },
    )

    status = _compare(base, candidate, "--acknowledged", str(acknowledged))

    assert status == tool.EXIT_DIVERGED
    captured = capsys.readouterr()
    assert "stale acknowledgements: 1" in captured.out
    assert "match no divergence" in captured.err


def test_a_shrunk_corpus_does_not_call_an_acknowledgement_stale(tmp_path):
    # The script an entry names may simply not have been drawn, so a partial replay is no evidence
    # that its transition stopped happening.
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    shrunk = _candidate_document([_scored("true", "certified[]")])
    shrunk["seeds"] = [1]
    shrunk["iterations"] = 5
    candidate = _write(tmp_path / "candidate.json", shrunk)
    acknowledged = _write(
        tmp_path / "acknowledged.json",
        {
            "acknowledgements": [
                {
                    "sha256": "d9",
                    "base_verdict": "certified[]",
                    "candidate_verdict": "guard:x",
                    "reason": "covered by a script this run never drew",
                }
            ]
        },
    )

    status = _compare(base, candidate, "--allow-shrunk-corpus", "--acknowledged", str(acknowledged))

    assert status == tool.EXIT_OK


def test_compare_refuses_rather_than_reports_when_the_corpora_disagree(tmp_path, capsys):
    base = _write(
        tmp_path / "base.json", _record_document([_scored("true", "certified[]")], corpus="a" * 64)
    )
    candidate = _write(
        tmp_path / "candidate.json",
        _candidate_document([_scored("true", "guard:x")], corpus="b" * 64),
    )

    status = _compare(base, candidate)

    assert status == tool.EXIT_REFUSED
    assert "different corpora" in capsys.readouterr().err


def test_compare_refuses_a_record_written_under_another_schema(tmp_path, capsys):
    document = _record_document([_scored("true", "certified[]")])
    document["schema"] = tool.SCHEMA + 1
    base = _write(tmp_path / "base.json", document)
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "certified[]")])
    )

    status = _compare(base, candidate)

    assert status == tool.EXIT_REFUSED
    assert "schema" in capsys.readouterr().err


def test_compare_refuses_when_an_input_it_was_pointed_at_is_not_there(tmp_path, capsys):
    # A base revision whose worktree carries no frozen inventory is a refusal with a readable
    # line, not a traceback out of the middle of the comparison.
    base = _write(tmp_path / "base.json", _record_document([_scored("true", "certified[]")]))
    candidate = _write(
        tmp_path / "candidate.json", _candidate_document([_scored("true", "certified[]")])
    )

    status = tool.main(
        [
            "compare",
            "--base",
            str(base),
            "--candidate",
            str(candidate),
            "--base-inventory",
            str(tmp_path / "absent.json"),
        ]
    )

    assert status == tool.EXIT_REFUSED
    assert "corpus differential refused" in capsys.readouterr().err


def test_a_targeted_early_refusal_diverges_every_corpus_script_carrying_the_option(tmp_path):
    """The round-6 demonstration, replayed through the gate this issue asks for.

    An input-specific refusal inserted at the top of `_ShellScanner.scan` withdraws a frozen guard
    with every static gate green. It cannot hide here: it moves the verdict of the corpus scripts
    carrying the targeted option, and the differential reports the transition by script.
    """
    base_record = _replay(_revision(tmp_path, "base", mutate=False), tmp_path / "base.json")
    candidate_record = _replay(
        _revision(tmp_path, "candidate", mutate=True), tmp_path / "candidate.json"
    )

    completed = _run(
        "compare",
        "--base",
        str(base_record),
        "--candidate",
        str(candidate_record),
        "--base-inventory",
        str(_INVENTORY),
        "--allow-shrunk-corpus",
        "--acknowledged",
        str(_ACKNOWLEDGEMENTS),
    )

    assert completed.returncode == tool.EXIT_DIVERGED, completed.stdout
    diverged = {
        case["sha256"]
        for case in json.loads(candidate_record.read_text(encoding="utf-8"))["cases"]
        if case["verdict"] == "guard:scanner.round-six.early-refusal"
    }
    targeted = {
        case.digest for case in tool.inventory_cases(_INVENTORY) if _TARGETED_OPTION in case.source
    }
    assert targeted
    assert diverged == targeted
    assert "guard:scanner.round-six.early-refusal" in completed.stdout


def test_an_unchanged_revision_replays_the_corpus_clean(tmp_path):
    # The control for the witness above: the same corpus scored twice against unmodified copies
    # of the guard package reports nothing, so the divergence there is the edit and not the tool.
    base_record = _replay(_revision(tmp_path, "base", mutate=False), tmp_path / "base.json")
    candidate_record = _replay(
        _revision(tmp_path, "candidate", mutate=False), tmp_path / "candidate.json"
    )

    completed = _run(
        "compare",
        "--base",
        str(base_record),
        "--candidate",
        str(candidate_record),
        "--base-inventory",
        str(_INVENTORY),
        "--allow-shrunk-corpus",
        "--acknowledged",
        str(_ACKNOWLEDGEMENTS),
    )

    assert completed.returncode == 0, completed.stdout
    assert "corpus divergences: 0" in completed.stdout


def test_a_record_run_scores_the_generated_half_of_the_corpus_too(tmp_path):
    record = _replay(
        _revision(tmp_path, "base", mutate=False),
        tmp_path / "base.json",
        seeds="1",
        iterations=5,
    )

    document = json.loads(record.read_text(encoding="utf-8"))

    assert document["count"] == len(tool.inventory_cases(_INVENTORY)) + 5
    assert any(case["id"].startswith("fuzz-") for case in document["cases"])
    assert document["scanner_source"].startswith(str(tmp_path))
