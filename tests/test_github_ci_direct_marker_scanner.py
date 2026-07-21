"""Unit tests for the D3 floor-grammar scanner."""

import time

from doc_lattice.github_ci.direct_marker_scanner import (
    DIRECT_MARKER_RE,
    certified_command_words,
    scan_execution_source,
)
from doc_lattice.github_ci.model import BlockScan


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


def test_select_reserved_word_refuses():
    result = scan_execution_source("select doc-lattice")
    assert result.status == "uninspectable"
    assert result.reason_category == "control-flow-keyword"
    assert result.offset == 0


def test_in_reserved_word_refuses():
    result = scan_execution_source("in doc-lattice")
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
    # operator offset, keeping the invocation proven before it (monotonic evidence). run's dangling
    # check is the single point that refuses every variant where the right-hand command never
    # arrives, including the trailing-space and trailing-newline continuation forms.
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


def test_empty_command_between_operators_anchors_at_pending_operator():
    # bash -n rejects `cmd && || echo`: the second list operator closes an empty command whose
    # left operator already had no right-hand command. That pending && at offset 18 is the earlier
    # failure, so it outranks the second operator at 21, matching the `&& ;` and end-of-source
    # anchors. The check invocation proven before the pending operator is retained.
    result = scan_execution_source("doc-lattice check && || echo")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_empty_command_between_doubled_operators_anchors_at_pending_operator():
    # A second `&&` after a pending `&&` closes the same empty command; the earlier pending
    # operator at offset 18 wins over the second one at 21.
    result = scan_execution_source("doc-lattice check && && doc-lattice lint")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_completed_list_after_operator_still_certifies():
    result = scan_execution_source("doc-lattice check && doc-lattice lint")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_leading_semicolon_empty_statement_refuses():
    # bash rejects a leading `; cmd`; the empty statement it closes is refused at the semicolon.
    source = "; doc-lattice check"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == source.index(";")


def test_double_semicolon_empty_statement_refuses():
    # bash rejects `cmd;;`; the first `;` closes the command, the second closes an empty
    # statement that is refused at the second semicolon.
    source = "doc-lattice check;;"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == source.index(";") + 1


def test_single_trailing_semicolon_still_certifies():
    result = scan_execution_source("doc-lattice check;")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_semicolon_separated_commands_still_certify():
    result = scan_execution_source("doc-lattice check ; doc-lattice lint")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_blank_lines_still_certify():
    result = scan_execution_source("\ndoc-lattice check\n\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False),)


def test_semicolon_only_line_refuses():
    source = "doc-lattice check\n;\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == source.index(";")


def test_earliest_policy_refusal_beats_later_pipe():
    # D4: the unknown subcommand is a policy refusal at offset 12, earlier than the pipe at 23,
    # so the earliest syntax-or-policy failure wins over the later lexical error.
    result = scan_execution_source("doc-lattice frobnicate | cat")
    assert result.status == "uninspectable"
    assert result.reason_category == "policy-unresolvable"
    assert result.offset == 12


def test_earliest_assignment_prefix_beats_later_pipe():
    result = scan_execution_source("FOO=bar doc-lattice check | cat")
    assert result.status == "uninspectable"
    assert result.reason_category == "assignment-prefix"
    assert result.offset == 0


def test_earliest_unstable_first_word_beats_later_pipe():
    # An expansion in command position is an unstable first word refused at offset 0, earlier than
    # the later pipe. The env-var spelling carries the direct marker so the source is scanned.
    result = scan_execution_source('"$DOC_LATTICE" run | cat')
    assert result.status == "uninspectable"
    assert result.reason_category == "unstable-first-word"
    assert result.offset == 0


def test_clean_prefix_before_pipe_keeps_lexical_refusal():
    # A cleanly resolving prefix yields no earlier command-level refusal, so the later pipe stands.
    # The full BlockScan (work_charged=43 in particular) pins the pre-fix accounting: the pure
    # earliest-refusal path never charges the work meter or flushes an invocation.
    result = scan_execution_source("doc-lattice check | cat")
    assert result == BlockScan(
        "uninspectable",
        (),
        "unsupported-operator",
        "unsupported shell operator at offset 18",
        18,
        43,
    )


def test_empty_command_before_pipe_keeps_lexical_refusal():
    # A command with no scanned words (a leading pipe) is never intercepted by the command-level
    # derivation, so the pipe is reported at its own offset. The payload carries the marker.
    result = scan_execution_source("| doc-lattice")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 0
    assert result.invocations == ()


def test_mid_word_expansion_refusal_retains_partial_word_for_earlier_anchor():
    # The `#` that ends `$X#foo` is an unsupported operator at offset 20, but the partial word
    # `$X` read before it carries an unquoted expansion at offset 18. Retaining that partial word
    # lets the command-level pass anchor the earlier unquoted-expansion failure (spec D4).
    result = scan_execution_source("doc-lattice check $X#foo")
    assert result.status == "uninspectable"
    assert result.reason_category == "unquoted-expansion-in-command-word"
    assert result.offset == 18
    assert result.invocations == ()


def test_mid_word_unterminated_quote_yields_to_earlier_expansion():
    # The single quote at offset 20 is unterminated, but the partial word `$X` already carries an
    # unquoted expansion at offset 18, which is the earlier failure and wins over the quote.
    result = scan_execution_source("doc-lattice check $X'oops")
    assert result.status == "uninspectable"
    assert result.reason_category == "unquoted-expansion-in-command-word"
    assert result.offset == 18
    assert result.invocations == ()


def test_mid_word_refusal_in_first_word_anchors_unstable_at_word_start():
    # The partial first word `$DOC_LATTICE` is unstable (it carries an expansion, whose name also
    # supplies the direct marker so the source is scanned), which is an unstable first word
    # anchored at the word start (offset 0), earlier than the `#` operator that interrupts it.
    result = scan_execution_source("$DOC_LATTICE#foo")
    assert result.status == "uninspectable"
    assert result.reason_category == "unstable-first-word"
    assert result.offset == 0
    assert result.invocations == ()


def test_partial_word_with_no_earlier_failure_keeps_lexical_anchor():
    # The partial word `abc` forms `doc-lattice check abc`, which resolves cleanly, so no earlier
    # command-level failure exists and the lexical `#` operator at offset 21 stands.
    result = scan_execution_source("doc-lattice check abc#foo")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 21
    assert result.invocations == ()


def test_and_list_continues_across_newline():
    # bash -n accepts a newline after &&; the list continues to the command on the next line.
    result = scan_execution_source("doc-lattice check &&\ndoc-lattice lint\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_or_list_continues_across_newline():
    result = scan_execution_source("doc-lattice check ||\ndoc-lattice lint\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_and_list_continues_across_comment_after_operator():
    # A comment immediately after the operator is certifiable (spec comments rule); bash -n reads
    # the right-hand command from the following line.
    result = scan_execution_source("doc-lattice check && # gate two\ndoc-lattice lint\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_and_list_continues_across_blank_and_comment_lines():
    # bash -n reads the right-hand command past blank and comment lines following the operator.
    result = scan_execution_source("doc-lattice check &&\n\n# note\ndoc-lattice lint\n")
    assert result.status == "certified"
    assert result.invocations == (("check", False), ("lint", False))


def test_dangling_and_operator_before_newline_eof_refuses():
    # A newline continues the list, but no right-hand command ever arrives before end of source, so
    # run's dangling check refuses at the operator offset (bash -n rejects the dangling operator).
    result = scan_execution_source("doc-lattice check &&\n")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_dangling_and_operator_before_trailing_blank_lines_refuses():
    # Trailing whitespace and blank continuation lines still never supply the right-hand command.
    result = scan_execution_source("doc-lattice check && \n  \n")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_pending_operator_across_newline_outranks_later_semicolon():
    # The newline continues the list; the `;` then closes an empty continuation. The pending && at
    # offset 18 is the earlier failure, so it outranks the semicolon (as `&& ;` does inline).
    result = scan_execution_source("doc-lattice check &&\n;")
    assert result.status == "uninspectable"
    assert result.reason_category == "unsupported-operator"
    assert result.offset == 18
    assert result.invocations == (("check", False),)


def test_certified_command_words_spans_newline_continued_list():
    words = certified_command_words("doc-lattice check &&\ndoc-lattice lint\n")
    assert words == (("doc-lattice", "check"), ("doc-lattice", "lint"))


def test_uv_run_from_option_refuses_at_marker_offset():
    # uv run does not accept --from; the launcher policy refuses at the --from offset and the
    # scanner surfaces that as a policy-unresolvable refusal.
    source = "uv run --from doc-lattice check\n"
    result = scan_execution_source(source)
    assert result.status == "uninspectable"
    assert result.reason_category == "policy-unresolvable"
    assert result.offset == source.index("--from")
