"""Unit tests for the D3 floor-grammar scanner."""

from doc_lattice.github_ci.direct_marker_scanner import (
    DIRECT_MARKER_RE,
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
