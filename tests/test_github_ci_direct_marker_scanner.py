"""Unit tests for the D3 floor-grammar scanner."""

import time

from doc_lattice.github_ci.direct_marker_scanner import (
    DIRECT_MARKER_RE,
    certified_command_words,
    scan_execution_source,
)


def test_marker_gate_not_applicable():
    result = scan_execution_source("echo hello\nexit 1\n")
    assert result.status == "not_applicable"
    assert result.invocations == ()
    assert result.work_charged == len("echo hello\nexit 1\n")


def test_marker_matches_spelling_variants():
    for text in ("doc-lattice", "DOC-LATTICE", "doc_lattice", "doc.lattice", "doc-_lattice"):
        assert DIRECT_MARKER_RE.search(text)
    assert not DIRECT_MARKER_RE.search('doc-"lattice"')


def test_simple_command_certifies():
    result = scan_execution_source("doc-lattice check\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_marker_in_comment_scans_whole_block():
    result = scan_execution_source("# doc-lattice notes\necho fine\n")
    assert result.status == "certified"
    assert result.invocations == ()


def test_list_scans_both_sides():
    result = scan_execution_source("doc-lattice check && doc-lattice lint\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_or_list_scans_both_sides():
    result = scan_execution_source("doc-lattice lint || exit 1\n")
    assert result.status == "certified"
    assert result.invocations == (("lint", False),)


def test_assignment_statement_certifies():
    result = scan_execution_source("CFG=doc-lattice.yml\ndoc-lattice check\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_assignment_prefix_refuses():
    result = scan_execution_source("CFG=x doc-lattice check\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "assignment-prefix"


def test_append_assignment_prefix_refuses():
    result = scan_execution_source("PATH+=:/tools doc-lattice check\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "assignment-prefix"


def test_append_assignment_statement_certifies():
    result = scan_execution_source("FLAGS+=x\ndoc-lattice check\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_unquoted_pipe_refuses_at_offset():
    source = "doc-lattice check | cat\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == source.index("|")


def test_command_substitution_refuses():
    result = scan_execution_source("OUT=$(doc-lattice check)\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-expansion"


def test_control_flow_refuses():
    result = scan_execution_source("if doc-lattice check; then echo ok; fi\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "control-flow-keyword"
    assert result.offset == 0


def test_quoted_expansion_in_argument_certifies():
    result = scan_execution_source('doc-lattice check --config "$CFG"\n')
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_unquoted_expansion_in_command_word_refuses():
    source = "doc-lattice check $CFG\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unquoted-expansion-in-command-word"


def test_unstable_first_word_refuses():
    result = scan_execution_source('"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear\n')
    assert result.status == "uninspectable"
    assert result.reason_category == "unstable-first-word"
    assert result.offset == 0


def test_carriage_return_refuses():
    result = scan_execution_source("doc-lattice check\r\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "control-character"


def test_quote_spanning_newline_refuses():
    result = scan_execution_source('doc-lattice check "a\nb"\n')
    assert result.status == "uninspectable"
    assert result.reason_category == "quote-spans-newline"


def test_unterminated_quote_refuses():
    result = scan_execution_source("doc-lattice check 'oops\n")
    assert result.status in {"uninspectable"}
    assert result.reason_category in {"quote-spans-newline", "unterminated-quote"}


def test_monotonic_evidence_retains_prior_statements():
    source = "doc-lattice check\ndoc-lattice lint | cat\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.invocations == (("check", False),)


def test_mid_command_failure_drops_that_invocation():
    source = "doc-lattice lint | cat\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.invocations == ()


def test_source_cap_refuses():
    result = scan_execution_source("doc-lattice " + "a" * 1_100_000)
    assert result.status == "uninspectable"
    assert result.reason_category == "cap-exceeded"
    assert result.offset == 0


def test_work_charged_is_linear():
    source = "doc-lattice check\n" * 100
    result = scan_execution_source(source)
    assert result.status == "certified"
    assert result.work_charged <= min(4_194_304, 4 * len(source) + 4_096)


def test_tokenization_is_linear_in_word_count():
    # A single marker-bearing command with many words stresses the per-word tokenizer path.
    # This was O(word_count ** 2) before delimiter classification stopped copying the word
    # list on every word start. At 80,000 words the quadratic path measured ~9.3s, which blows
    # the 5.0s bound; the linear implementation measured ~136ms at the same size, so the bound
    # only passes on the linear path.
    source = "doc-lattice check " + "a " * 80_000 + "\n"
    start = time.perf_counter()
    result = scan_execution_source(source)
    elapsed = time.perf_counter() - start
    assert result.status == "certified"
    assert result.invocations == (("check", False),)
    assert elapsed < 5.0


def test_certified_command_words_exposes_structure():
    words = certified_command_words('doc-lattice check --config "$CFG" && doc-lattice lint\n')
    assert words == (("doc-lattice", "check", "--config", "$CFG"), ("doc-lattice", "lint"))
    assert certified_command_words("doc-lattice check | cat\n") == ()


def test_certified_command_words_skips_assignment_only_statements():
    words = certified_command_words('FLAG=--dry-run; doc-lattice reconcile "$FLAG"\n')
    assert words == (("doc-lattice", "reconcile", "$FLAG"),)


def test_certified_command_words_reports_non_candidate_commands():
    words = certified_command_words("false && doc-lattice linear\n")
    assert words == (("false",), ("doc-lattice", "linear"))


def test_exe_executable_head_certifies_end_to_end():
    result = scan_execution_source(".venv/Scripts/doc-lattice.exe linear")
    assert result.status == "certified"
    assert result.invocations == (("linear", False),)


def test_exe_executable_head_carries_reconcile_dry_run_flag():
    # The .exe head routes through the same subcommand resolution, so reconcile carries its
    # dry-run flag exactly as a bare doc-lattice head does.
    plain = scan_execution_source("doc-lattice.exe reconcile")
    assert plain.status == "certified"
    assert plain.invocations == (("reconcile", False),)
    dry = scan_execution_source("doc-lattice.exe reconcile --dry-run")
    assert dry.status == "certified"
    assert dry.invocations == (("reconcile", True),)


def test_dangling_and_operator_at_eof_refuses():
    # A source ending immediately after && is rejected by bash -n; the recognizer refuses at the
    # operator offset, keeping the invocation proven before it (monotonic evidence), matching the
    # trailing-space variant that already refuses through _commit_command.
    result = scan_execution_source("doc-lattice check &&")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_dangling_or_operator_at_eof_refuses():
    result = scan_execution_source("doc-lattice check ||")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_completed_list_after_operator_still_certifies():
    result = scan_execution_source("doc-lattice check && doc-lattice lint")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))
