"""Tests for the bounded, non-executing doc-lattice shell invocation scanner."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.core import TyperGroup
from typer.main import get_command

from doc_lattice.cli.application import create_app
from doc_lattice.error_types import ConfigError, ProjectError
from doc_lattice.github_ci import shell_scanner
from doc_lattice.github_ci.shell_scanner import (
    _DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS,
    _DOC_LATTICE_ROOT_OPTIONS,
    _RECONCILE_FLAGS,
    _RECONCILE_OPTIONS_WITH_ARGUMENTS,
    _CommandScanState,
    _effective_executable_evidence,
    _ExecutableCandidate,  # noqa: F401 - private evidence API under test
    _ScanBudget,
    _ShellScanIncomplete,
    _ShellScanner,
    _ShellWord,
    _uv_requirement_executable_name,
    _uv_requirement_is_path,
    _wheel_distribution_name,
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)
from doc_lattice.github_ci.shell_taint import (
    TAINT_REFUSAL_REASON,
    ChoiceOutput,
    LiteralTransfer,
    RepeatOutput,
    SequenceOutput,
)

NONE = ()
LINEAR = (("linear", False),)
RECONCILE = (("reconcile", False),)
RECONCILE_DRY = (("reconcile", True),)
CHECK = (("check", False),)
LINEAR_LINT = (("linear", False), ("lint", False))
INCOMPLETE = object()

PHASE_TWO_RUNTIME_REFUSALS = [
    (
        "file-handoff",
        "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; bash task.sh",
        {},
    ),
    (
        "dot-file-handoff",
        "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; bash ./task.sh",
        {},
    ),
    (
        "heredoc-handoff",
        "cat > task.sh <<'EOF'\ndoc-lattice reconcile\nEOF\nbash task.sh",
        {},
    ),
    (
        "variable-eval",
        "X=doc-; X+='lattice reconcile'; eval \"$X\"",
        {},
    ),
    (
        "pipeline",
        "printf '%s%s\\n' doc- 'lattice reconcile' | bash",
        {},
    ),
    (
        "herestring",
        "X=doc-; X+='lattice reconcile'; bash <<< \"$X\"",
        {},
    ),
    (
        "input-process-substitution",
        "bash < <(printf '%s%s\\n' doc- 'lattice reconcile')",
        {},
    ),
    (
        "multi-command-substitution",
        "eval \"$(printf doc-; printf 'lattice reconcile')\"",
        {},
    ),
    (
        "compound-group",
        "{ printf doc-; printf 'lattice reconcile'; } > task.sh; bash task.sh",
        {},
    ),
    (
        "parameter-default",
        'unset X; eval "${X:-doc-}lattice reconcile"',
        {},
    ),
    (
        "parameter-assign-default",
        'unset X; eval "${X:=doc-}lattice"',
        {},
    ),
    (
        "parameter-assigned-later",
        'unset X; : "${X:=doc-}"; X+=lattice; eval "$X"',
        {},
    ),
    (
        "brace-eval",
        "eval doc-{lattice,noop}",
        {},
    ),
    (
        "brace-pipeline",
        "printf %s {doc-,lattice} | bash",
        {},
    ),
    (
        "for-binding",
        'for X in doc- lattice; do printf %s "$X"; done | bash',
        {},
    ),
    (
        "case-fallthrough",
        "case a in a) printf doc- ;& *) printf lattice ;; esac | bash",
        {},
    ),
    (
        "static-stdin-read",
        "printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; bash < task.sh",
        {},
    ),
    (
        "builtin-eval",
        'X=doc-; X+=lattice; builtin eval "$X"',
        {},
    ),
    (
        "uv-run-shell",
        'X=doc-; X+=lattice; uv run bash -c "$X"',
        {},
    ),
    (
        "ambiguous-selector",
        'X=doc-; X+=\'lattice reconcile\'; bash "$OPT" "$X"',
        {"OPT": "-c"},
    ),
    (
        "substitution-newline-strip",
        "eval \"$(cat <<'EOF'\ndoc-\nEOF\n)lattice reconcile\"",
        {},
    ),
    (
        "while-test-list",
        (
            "i=0; P='#\\n'; "
            'while { printf %b "$P"; test "$i" -lt 1; }; '
            "do printf doc-; P=lattice; i=1; done | bash"
        ),
        {},
    ),
    (
        "final-descriptor-binding",
        ("printf '%s%s\\n' doc- 'lattice reconcile' > /dev/null > task.sh; bash task.sh"),
        {},
    ),
    (
        "direct-path",
        ("printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; chmod +x task.sh; ./task.sh"),
        {},
    ),
    (
        "source",
        ("printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; source ./task.sh"),
        {},
    ),
    (
        "dot-source",
        ("printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; . ./task.sh"),
        {},
    ),
    (
        "resource-append",
        ("printf %s doc- > task.sh; printf %s lattice >> task.sh; bash task.sh"),
        {},
    ),
]


def _static_word(literal: str, *, assignment: bool = False) -> _ShellWord:
    return _ShellWord(literal=literal, shell_assignment=assignment)


@pytest.mark.parametrize(
    ("words", "expected_name", "expected_external_lookup"),
    [
        ([_static_word("eval"), _static_word("$X")], "eval", False),
        ([_static_word("command"), _static_word("eval"), _static_word("$X")], "eval", False),
        ([_static_word("env"), _static_word("eval"), _static_word("$X")], "eval", True),
        ([_static_word("exec"), _static_word("eval"), _static_word("$X")], "eval", True),
        (
            [
                _static_word("uv"),
                _static_word("run"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            "bash",
            True,
        ),
        (
            [_static_word("uvx"), _static_word("bash@5.2"), _static_word("-c"), _static_word("$X")],
            "bash",
            True,
        ),
        ([_static_word("/bin/bash"), _static_word("-c"), _static_word("$X")], "bash", False),
        ([_static_word("./task.sh")], "task.sh", False),
    ],
    ids=(
        "builtin-eval",
        "command-eval",
        "env-eval",
        "exec-eval",
        "uv-run-shell",
        "uvx-versioned-shell",
        "path-shell",
        "direct-path",
    ),
)
def test_effective_executable_evidence_resolves_launcher_payload(
    words: list[_ShellWord], expected_name: str, expected_external_lookup: bool
) -> None:
    evidence = _effective_executable_evidence(words, _ScanBudget())

    assert evidence is not None
    assert evidence.name == expected_name
    assert evidence.external_lookup is expected_external_lookup


def test_effective_executable_evidence_marks_prefix_ambiguity() -> None:
    evidence = _effective_executable_evidence(
        [
            _ShellWord(literal="", dynamic=True, unquoted_dynamic=True),
            _static_word("bash"),
            _static_word("-c"),
            _static_word("$X"),
        ],
        _ScanBudget(),
    )

    assert evidence is not None
    assert evidence.name == "bash"
    assert evidence.ambiguous is True


@pytest.mark.parametrize(
    ("words", "expected_index", "expected_name"),
    [
        (
            [
                _static_word("exec"),
                _static_word("command"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            1,
            "command",
        ),
        (
            [
                _static_word("exec"),
                _static_word("builtin"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            1,
            "builtin",
        ),
        (
            [
                _static_word("exec"),
                _static_word("exec"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            1,
            "exec",
        ),
        (
            [
                _static_word("command"),
                _static_word("exec"),
                _static_word("exec"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            2,
            "exec",
        ),
    ],
    ids=("command", "builtin", "exec", "command-exec"),
)
def test_effective_executable_evidence_stops_at_external_wrapper(
    words: list[_ShellWord], expected_index: int, expected_name: str
) -> None:
    evidence = _effective_executable_evidence(
        words,
        _ScanBudget(),
    )

    assert evidence is not None
    assert (evidence.argv_index, evidence.name, evidence.external_lookup) == (
        expected_index,
        expected_name,
        True,
    )
    assert evidence.alternates == ()


def test_effective_executable_evidence_ignores_non_sink_builtin_target() -> None:
    evidence = _effective_executable_evidence(
        [_static_word("builtin"), _static_word("bash"), _static_word("-c"), _static_word("$X")],
        _ScanBudget(),
    )

    assert evidence is None


def test_effective_executable_evidence_preserves_builtin_eval_target() -> None:
    evidence = _effective_executable_evidence(
        [_static_word("builtin"), _static_word("eval"), _static_word("$X")], _ScanBudget()
    )

    assert evidence is not None
    assert (evidence.argv_index, evidence.name, evidence.external_lookup) == (1, "eval", False)


@pytest.mark.parametrize(
    ("words", "primary_name", "primary_index", "expected_alternates"),
    [
        (
            [
                _static_word("uv"),
                _ShellWord(literal="$UV", dynamic=True),
                _static_word("run"),
                _static_word("bash"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            "bash",
            3,
            ((0, "uv", False), (2, "run", True)),
        ),
        (
            [
                _static_word("uvx"),
                _ShellWord(literal="$UVX", dynamic=True),
                _static_word("bash@5.2"),
                _static_word("-c"),
                _static_word("$X"),
            ],
            "bash",
            2,
            ((0, "uvx", False),),
        ),
    ],
    ids=("uv", "uvx"),
)
def test_dynamic_launcher_evidence_has_unique_ordered_alternates(
    words: list[_ShellWord],
    primary_name: str,
    primary_index: int,
    expected_alternates: tuple[tuple[int, str, bool], ...],
) -> None:
    evidence = _effective_executable_evidence(words, _ScanBudget())

    assert evidence is not None
    assert (evidence.argv_index, evidence.name, evidence.ambiguous) == (
        primary_index,
        primary_name,
        True,
    )
    assert [
        (alternate.argv_index, alternate.name, alternate.ambiguous)
        for alternate in evidence.alternates
    ] == list(expected_alternates)


def assert_marker_refusal(script: str) -> None:
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None
    with pytest.raises(ConfigError, match=r"shell scan incomplete"):
        direct_doc_lattice_invocations(script)


def assert_taint_refusal(script: str) -> None:
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == TAINT_REFUSAL_REASON
    with pytest.raises(
        ConfigError,
        match=r"shell scan incomplete: authored marker flow reaches an execution sink",
    ):
        direct_doc_lattice_invocations(script)


def test_split_pipeline_stdout_reaches_shell_stdin():
    assert_taint_refusal("printf '%s%s\\n' doc- 'lattice reconcile' | bash")


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- 'lattice reconcile' |& bash",
        "printf '%s%s\\n' doc- 'lattice reconcile' 2>/dev/null |& bash",
        "printf '%s%s\\n' doc- 'lattice reconcile' |& { bash; }",
    ],
    ids=("command", "stderr-rebound-before-implicit-copy", "compound-consumer"),
)
def test_combined_pipeline_output_reaches_shell_stdin(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- lattice >/dev/null |& bash",
        "printf '%s%s\\n' doc- lattice |& bash <<<'true'",
        "printf '%s%s\\n' doc- lattice |& cat",
    ],
    ids=("stdout-rebound", "stdin-override", "non-sink-consumer"),
)
def test_combined_pipeline_preserves_redirection_and_sink_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_combined_pipeline_lastpipe_consumer_updates_parent_environment():
    assert_taint_refusal(
        'shopt -s lastpipe; S=\': ${X:=doc-}\'; printf x |& eval "$S"; eval "$X"lattice'
    )


@pytest.mark.parametrize(
    "prefix",
    ["", "shopt -u lastpipe; "],
    ids=("default", "explicitly-disabled"),
)
def test_combined_pipeline_consumer_keeps_isolated_environment_without_lastpipe(prefix: str):
    result = scan_doc_lattice_invocations(
        f'{prefix}S=\': ${{X:=doc-}}\'; printf x |& eval "$S"; eval "$X"lattice'
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_marker_free_unresolved_pipeline_stays_certified():
    result = scan_doc_lattice_invocations("curl https://example.invalid/script | bash")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_later_herestring_rebinds_pipeline_stdin():
    result = scan_doc_lattice_invocations(
        "printf '%s%s\\n' doc- 'lattice reconcile' | bash <<<'true'"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_input_process_substitution_redirection_reaches_stdin():
    assert_taint_refusal("bash < <(printf '%s%s\\n' doc- 'lattice reconcile')")


def test_input_process_substitution_script_operand_reaches_sink():
    assert_taint_refusal("bash <(printf '%s%s\\n' doc- 'lattice reconcile')")


def test_process_substitution_read_by_non_sink_is_not_overconnected():
    result = scan_doc_lattice_invocations("grep x <(printf '%s%s' doc- lattice)")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_output_process_substitution_routes_writer_to_consumer_stdin():
    assert_taint_refusal("printf '%s%s\\n' doc- 'lattice reconcile' > >(bash)")


def test_multi_command_substitution_scope_sequences_stdout():
    assert_taint_refusal("eval \"$(printf doc-; printf 'lattice reconcile')\"\n")


def test_compound_group_stdout_reaches_written_resource():
    assert_taint_refusal("{ printf doc-; printf 'lattice reconcile'; } > task.sh\nbash task.sh\n")


def test_command_substitution_strips_trailing_newline_before_splice():
    assert_taint_refusal("eval \"$(cat <<'EOF'\ndoc-\nEOF\n)lattice reconcile\"\n")


def test_arithmetic_subshell_fallback_stdout_reaches_pipeline_stdin():
    assert_taint_refusal("((printf '%s%s\\n' doc- 'lattice reconcile') ) | bash")


def test_arithmetic_subshell_fallback_stdout_reaches_written_resource():
    assert_taint_refusal("((printf '%s%s\\n' doc- 'lattice reconcile') ) > task.sh\nbash task.sh")


ACCEPTANCE_CASES = [
    # Literal executable identity and control syntax.
    ("ansi-c executable", "$'doc-lattice' linear", LINEAR),
    ("concatenated quoted words", 'doc-"lattice" l"inear"', LINEAR),
    (
        "elif condition",
        "if false; then :; elif doc-lattice linear; then :; fi",
        LINEAR,
    ),
    (
        "while condition",
        "while doc-lattice check; do break; done",
        CHECK,
    ),
    (
        "until condition",
        "until doc-lattice check; do break; done",
        CHECK,
    ),
    ("time reserved word", "time doc-lattice linear", LINEAR),
    (
        "coproc reserved word",
        'coproc doc-lattice linear; p=$COPROC_PID; wait "$p"',
        LINEAR,
    ),
    ("case arm", "case x in x) doc-lattice linear;; esac", LINEAR),
    (
        "runtime-unreachable command remains conservative",
        "false && doc-lattice linear",
        LINEAR,
    ),
    # Modern command substitutions.
    ("double-quoted substitution", 'echo "$(doc-lattice linear)"', LINEAR),
    (
        "assignment-only substitution",
        'value="$(doc-lattice reconcile --all)"',
        RECONCILE,
    ),
    (
        "nested substitution",
        'echo "$(printf %s "$(doc-lattice linear)")"',
        LINEAR,
    ),
    ("locale-quoted substitution", 'echo $"$(doc-lattice linear)"', LINEAR),
    (
        "escaped substitution literal",
        r'echo "\$(doc-lattice linear)"',
        INCOMPLETE,
    ),
    (
        "single-quoted substitution literal",
        "echo '$(doc-lattice linear)'",
        INCOMPLETE,
    ),
    (
        "inner single-quoted substitution literal",
        """echo "$(printf '%s' '$(doc-lattice linear)')\"""",
        INCOMPLETE,
    ),
    (
        "comment then active command",
        'echo "$(true # harmless\ndoc-lattice linear)"',
        LINEAR,
    ),
    (
        "backticks inside substitution comment",
        'echo "$(true # `doc-lattice linear`\nprintf done)"',
        NONE,
    ),
    (
        "comment line then active command",
        'echo "$(\n# doc-lattice linear\ndoc-lattice check\n)"',
        CHECK,
    ),
    (
        "trailing comment backslash does not continue the comment",
        "# harmless \\\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "even trailing comment backslashes do not continue the comment",
        "echo before # harmless \\\\\ndoc-lattice linear",
        LINEAR,
    ),
    # Legacy backtick substitutions.
    (
        "nested legacy substitution",
        "echo `printf '%s' \\`doc-lattice linear\\``",
        LINEAR,
    ),
    (
        "legacy substitution comment literal",
        "echo `true # doc-lattice linear\nprintf done`",
        NONE,
    ),
    (
        "legacy substitution command after comment",
        "echo `true # harmless\ndoc-lattice linear`",
        LINEAR,
    ),
    # Parameter and arithmetic contexts.
    (
        "parameter default substitution",
        'unset x; echo "${x:-$(doc-lattice linear)}"',
        LINEAR,
    ),
    (
        "nested parameter substitution",
        'unset x y; echo "${x:-${y:-$(doc-lattice linear)}}"',
        LINEAR,
    ),
    (
        "parameter parenthesis does not close substitution",
        'echo "$(printf %s ${x:-)}; doc-lattice linear)"',
        LINEAR,
    ),
    (
        "hash inside parameter expansion is not a comment",
        'unset x; echo "${x:-# $(doc-lattice linear)}"',
        LINEAR,
    ),
    (
        "single-quoted parameter expansion literal",
        "echo '${x:-$(doc-lattice linear)}'",
        INCOMPLETE,
    ),
    (
        "parameter text resembling heredoc",
        "echo ${x:-<<EOF}\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "parameter arithmetic shift",
        "x=abcdef; echo ${x:1<<2}\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "arithmetic expansion shift",
        "echo $((1 << 2))\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "arithmetic command shift",
        "((x = 1 << 2))\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "legacy arithmetic shift",
        "echo $[1 << 2]\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "modern substitution in arithmetic",
        "echo $(( $(doc-lattice check) + 1 ))",
        CHECK,
    ),
    (
        "legacy substitution in arithmetic",
        "echo $(( `doc-lattice check` + 1 ))",
        CHECK,
    ),
    (
        "substitution in legacy arithmetic",
        "echo $[ $(doc-lattice check) + 1 ]",
        CHECK,
    ),
    (
        "unbalanced dollar-arithmetic runs a command-substitution subshell",
        "x=$((doc-lattice linear) )",
        LINEAR,
    ),
    (
        "unbalanced arithmetic command runs a nested subshell",
        "((doc-lattice linear) )",
        LINEAR,
    ),
    (
        "unbalanced dollar-arithmetic subshell without an invocation",
        "echo out $((true INNER) )",
        NONE,
    ),
    (
        "balanced dollar-arithmetic is not a command",
        "echo $((rc_audit + 1))",
        NONE,
    ),
    (
        "balanced dollar-arithmetic assignment is not a command",
        "x=$((1 + 2))",
        NONE,
    ),
    (
        "nested balanced dollar-arithmetic is not a command",
        "echo $(( (rc_audit + 1) * 2 ))",
        NONE,
    ),
    (
        "unterminated dollar-arithmetic yields no command",
        "echo $((1 + 2",
        NONE,
    ),
    # Heredocs, here-strings, and process substitutions.
    (
        "plain heredoc body is data",
        "cat <<EOF\ndoc-lattice linear\nEOF\ndoc-lattice check",
        CHECK,
    ),
    (
        "unquoted heredoc expands modern substitution",
        "cat <<EOF\n$(doc-lattice linear)\nEOF",
        LINEAR,
    ),
    (
        "quote characters do not quote unquoted heredoc body",
        "cat <<EOF\n'$(doc-lattice linear)'\nEOF",
        LINEAR,
    ),
    (
        "escaped dollar in unquoted heredoc",
        "cat <<EOF\n\\$(doc-lattice linear)\nEOF",
        NONE,
    ),
    (
        "quoted heredoc suppresses modern substitution",
        "cat <<'EOF'\n$(doc-lattice linear)\nEOF",
        NONE,
    ),
    (
        "unquoted heredoc delimiter word removes continuation",
        "cat <<E\\\nOF\nharmless\nEOF\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "double-quoted heredoc delimiter word removes continuation",
        'cat <<"E\\\nOF"\nharmless\nEOF\ndoc-lattice linear',
        LINEAR,
    ),
    (
        "single-quoted heredoc delimiter word preserves continuation",
        "cat <<'E\\\nOF'\nharmless\nEOF\ndoc-lattice linear",
        NONE,
    ),
    (
        "ansi-quoted heredoc suppresses substitution",
        "cat <<$'EOF'\n$(doc-lattice linear)\nEOF",
        NONE,
    ),
    (
        "unquoted heredoc expands backticks",
        "cat <<EOF\n`doc-lattice linear`\nEOF",
        LINEAR,
    ),
    (
        "quoted heredoc suppresses backticks",
        "cat <<'EOF'\n`doc-lattice linear`\nEOF",
        NONE,
    ),
    (
        "hash does not comment unquoted heredoc expansion",
        "cat <<EOF\n# $(doc-lattice linear)\nEOF",
        LINEAR,
    ),
    (
        "nested unquoted heredoc",
        'echo "$(cat <<EOF\n$(doc-lattice linear)\nEOF\n)"',
        LINEAR,
    ),
    (
        "nested quoted heredoc",
        "echo \"$(cat <<'EOF'\n$(doc-lattice linear)\nEOF\n)\"",
        NONE,
    ),
    (
        "multiple heredocs retain expansion policy and ordering",
        (
            "cat <<A <<'B'\n"
            "$(doc-lattice linear)\n"
            "A\n"
            "$(doc-lattice reconcile --all)\n"
            "B\n"
            "doc-lattice lint"
        ),
        LINEAR_LINT,
    ),
    (
        "unquoted heredoc continuation suppresses physical delimiter",
        "cat <<EOF\nbody \\\nEOF\ndoc-lattice linear",
        NONE,
    ),
    (
        "unquoted heredoc continuation forms delimiter",
        "cat <<EOF\nEO\\\nF\ndoc-lattice linear",
        LINEAR,
    ),
    (
        "unquoted heredoc continuation forms command substitution",
        "cat <<EOF\n$\\\n(doc-lattice linear)\nEOF",
        LINEAR,
    ),
    (
        "here-string substitution",
        'cat <<< "$(doc-lattice linear)"',
        LINEAR,
    ),
    ("here-string literal", "cat <<< 'doc-lattice linear'", NONE),
    (
        "input process substitution",
        "cat <(doc-lattice linear) >/dev/null",
        LINEAR,
    ),
    (
        "output process substitution",
        "printf x > >(doc-lattice linear)",
        LINEAR,
    ),
    (
        "process substitution literal argument",
        "cat <(printf '%s' 'doc-lattice linear') >/dev/null",
        INCOMPLETE,
    ),
    # Redirection placement and dry-run accounting.
    (
        "named-fd redirection before executable",
        "{fd}>/dev/null doc-lattice linear",
        LINEAR,
    ),
    (
        "redirection before subcommand",
        "doc-lattice >/dev/null linear",
        LINEAR,
    ),
    (
        "redirection before uv payload",
        "uv run >/dev/null doc-lattice linear",
        LINEAR,
    ),
    (
        "dry-run is redirection target",
        "doc-lattice reconcile > --dry-run",
        RECONCILE,
    ),
    (
        "dry-run is here-string redirection word",
        "doc-lattice reconcile <<< --dry-run",
        RECONCILE,
    ),
    (
        "quoted dry-run remains an argv token",
        "doc-lattice reconcile '--dry-run'",
        RECONCILE_DRY,
    ),
    (
        "dynamically expanded dry-run is not a distinct lexical token",
        'FLAG=--dry-run; doc-lattice reconcile "$FLAG"',
        RECONCILE,
    ),
    (
        "substitution in redirection target executes",
        'printf x > "$(doc-lattice check)"',
        CHECK,
    ),
    # Literal multiline and malformed-fragment boundaries.
    (
        "multiline double-quoted literal",
        'printf "%s" "doc-lattice linear\nuv run doc-lattice reconcile"',
        INCOMPLETE,
    ),
    (
        "multiline single-quoted literal",
        "printf '%s' 'doc-lattice linear\nuv run doc-lattice reconcile'",
        INCOMPLETE,
    ),
    (
        "complete command before malformed substitution",
        'doc-lattice check; echo "$(',
        CHECK,
    ),
    # Issue #102 live-baseline launcher corrections. These cases must remain after the frozen
    # first 78 rows consumed by the issue #100 candidate-evaluation checkpoint.
    (
        "uv tool short option before selector is intentional exit 2",
        "uv tool -q run doc-lattice linear",
        INCOMPLETE,
    ),
    (
        "uv tool long option before selector is intentional exit 2",
        "uv tool --quiet run doc-lattice linear",
        INCOMPLETE,
    ),
    (
        "uv tool value option before selector is intentional exit 2",
        "uv tool --directory /tmp run doc-lattice linear",
        INCOMPLETE,
    ),
    (
        "uv tool option before non-run selector is intentional exit 2",
        "uv tool -q install doc-lattice",
        INCOMPLETE,
    ),
    (
        "uv tool dynamic value option before selector is intentional exit 2",
        'OPT=--directory; uv tool "$OPT" /tmp run doc-lattice linear',
        INCOMPLETE,
    ),
    ("bare uv tool install remains non-candidate", "uv tool install doc-lattice", INCOMPLETE),
    (
        "uvx no-sync is intentional exit 2",
        "uvx --no-sync doc-lattice linear",
        INCOMPLETE,
    ),
    (
        "uv tool run no-sync is intentional exit 2",
        "uv tool run --no-sync doc-lattice linear",
        INCOMPLETE,
    ),
    ("uv run no-sync remains certified", "uv run --no-sync doc-lattice linear", LINEAR),
]


@pytest.mark.parametrize(
    ("_description", "script", "expected"),
    ACCEPTANCE_CASES,
    ids=[case[0] for case in ACCEPTANCE_CASES],
)
def test_direct_doc_lattice_acceptance_corpus(_description, script, expected):
    if expected is INCOMPLETE:
        result = scan_doc_lattice_invocations(script)
        assert result.invocations == NONE
        assert result.incomplete_reason is not None
        with pytest.raises(ConfigError, match=r"shell scan incomplete"):
            direct_doc_lattice_invocations(script)
        return
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("doc-lattice linear --exit-code", (("linear", False),)),
        (
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code',
            (("linear", False),),
        ),
        (
            "uvx --from doc-lattice==2.1.0 doc-lattice reconcile target",
            (("reconcile", False),),
        ),
        (
            "uv run doc-lattice reconcile --all --dry-run",
            (("reconcile", True),),
        ),
        ("echo 'doc-lattice linear'", INCOMPLETE),
        ('printf "%s\\n" "doc-lattice reconcile --all"', INCOMPLETE),
        (
            "set +e\ndoc-lattice check\nrc_check=$?\ndoc-lattice lint\nrc_lint=$?\n",
            (("check", False), ("lint", False)),
        ),
        ("if doc-lattice linear; then printf ok; fi", (("linear", False),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_documented_forms(script, expected):
    if expected is INCOMPLETE:
        assert_marker_refusal(script)
        return
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        './"$TOOLS"/doc-lattice linear',
        'tools/"$OS"/doc-lattice reconcile --all',
        'tools/"$OS"doc-lattice linear',
        'env ./"$TOOLS"/doc-lattice linear',
        'uv run tools/"$OS"/doc-lattice reconcile --all',
    ],
    ids=["dot-relative", "nested-relative", "dynamic-basename", "env", "uv-run"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_dynamic_relative_executable_paths(
    script,
):
    with pytest.raises(ConfigError, match=r"shell scan.*dynamic"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("doc-lattice --no-color linear", LINEAR),
        ("doc-lattice --no-color reconcile --all", RECONCILE),
        (
            "uvx --from doc-lattice==2.1.0 doc-lattice --no-color linear",
            LINEAR,
        ),
        ("{ doc-lattice linear; }", LINEAR),
        ("{ doc-lattice reconcile --all; }", RECONCILE),
        ("time -p doc-lattice linear", LINEAR),
        ("time -- doc-lattice linear", LINEAR),
        ("time -p -- doc-lattice reconcile --all", RECONCILE),
        (r"\time -p doc-lattice linear", LINEAR),
        ("'time' -- doc-lattice linear", LINEAR),
        ("command time -p doc-lattice linear", LINEAR),
        ("exec time -p -- doc-lattice reconcile --all", RECONCILE),
        ("coproc DL doc-lattice reconcile --all", RECONCILE),
        (
            "coproc DL uvx --from doc-lattice==2.1.0 doc-lattice linear",
            LINEAR,
        ),
        ("coproc DL uv run doc-lattice reconcile --all", RECONCILE),
        ("coproc DL env X=1 doc-lattice linear", LINEAR),
        ("coproc DL command doc-lattice reconcile --all", RECONCILE),
        (
            "coproc uvx --from doc-lattice==2.1.0 doc-lattice linear",
            LINEAR,
        ),
        ("coproc uv run doc-lattice reconcile --all", RECONCILE),
        ("coproc env X=1 doc-lattice linear", LINEAR),
        ("coproc command doc-lattice reconcile --all", RECONCILE),
        ("uv run env X=1 doc-lattice linear", LINEAR),
        ("uv tool run env X=1 doc-lattice reconcile --all", RECONCILE),
        ("uv run time doc-lattice linear", LINEAR),
        ("uvx /usr/bin/time -p doc-lattice linear", LINEAR),
        ("uv run env X=1 time doc-lattice linear", LINEAR),
        ("uv run uvx doc-lattice linear", LINEAR),
        ("uvx uv@0.8.0 run doc-lattice linear", LINEAR),
        ("uv tool run uvx@0.8.0 doc-lattice reconcile --all", RECONCILE),
        ("uvx ./dist/doc_lattice-2.0.0-py3-none-any.whl reconcile", RECONCILE),
        ("uvx doc_lattice-2.0.0-py3-none-any.whl reconcile", RECONCILE),
        ("/usr/bin/time doc-lattice linear", LINEAR),
        ("env /usr/bin/time -p doc-lattice linear", LINEAR),
        ("env time -- doc-lattice linear", LINEAR),
        ("command env time -- doc-lattice linear", LINEAR),
        ("exec env time -- doc-lattice linear", LINEAR),
        ("time env time -- doc-lattice linear", LINEAR),
        ("env env time -- doc-lattice linear", LINEAR),
    ],
)
def test_direct_doc_lattice_invocations_handles_root_options_and_compound_grammar(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_fails_closed_on_exec_coproc_marker():
    assert_marker_refusal("exec coproc doc-lattice reconcile")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("bash-1.0.0-py3-none-any.whl", "bash"),
        ("bash-1.0.0-py2.py3-none-any.whl", "bash"),
        ("./dist/doc_lattice-2.0.0-py3-none-any.whl", "doc_lattice"),
        ("bash-1.0.0-1-py3-none-any.whl", "bash"),
        (".\\dist\\bash-1.0.0-py3-none-any.whl", "bash"),
        ("bash-1.0.0-py3-none-any.WHL", "bash"),
        ("bash-1.0.0-py3-none.whl", None),
        ("bash-1.0.0-1-extra-py3-none-any.whl", None),
        ("café-1.0.0-py3-none-any.whl", None),
        ("-1.0.0-py3-none-any.whl", None),
        ("bash-1.0.0.tar.gz", None),
        ("bash", None),
    ],
)
def test_wheel_distribution_name_parses_pep427_filenames(value, expected):
    assert _wheel_distribution_name(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (".", True),
        ("..", True),
        ("./tools/shellkit", True),
        (".\\dist\\bash", True),
        ("bash-1.0.0-py3-none-any.whl", True),
        ("bash-1.0.0.tar.gz", True),
        ("bash-1.0.0.ZIP", True),
        ("bash", False),
        ("bash@1.0", False),
    ],
)
def test_uv_requirement_is_path_recognizes_paths_and_archives(value, expected):
    assert _uv_requirement_is_path(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("bash@1.0", "bash"),
        ("./bash-1.0.0-py3-none-any.whl", "bash"),
        ("./doc-lattice", "doc-lattice"),
        ("./bash-1.0.0.tar.gz", None),
        ("./tools/shellkit", None),
        (".", None),
    ],
)
def test_uv_requirement_executable_name_resolves_paths_and_names(value, expected):
    assert _uv_requirement_executable_name(value) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("TOKEN=value doc-lattice linear", (("linear", False),)),
        ("env TOKEN=value doc-lattice linear", (("linear", False),)),
        ("! doc-lattice reconcile --all --dry-run", (("reconcile", True),)),
        (
            "if true; then doc-lattice check; fi; while false; do doc-lattice lint; done",
            (("check", False), ("lint", False)),
        ),
        (
            "uvx --python 3.13 --from doc-lattice==2.1.0 doc-lattice check",
            (("check", False),),
        ),
        ("uv run --isolated -- doc-lattice lint", (("lint", False),)),
        (
            "doc-lattice check && (doc-lattice lint || doc-lattice reconcile --dry-run); "
            "doc-lattice linear",
            (
                ("check", False),
                ("lint", False),
                ("reconcile", True),
                ("linear", False),
            ),
        ),
        ("doc-lattice rec\\\noncile --all --dry-run", (("reconcile", True),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_shell_prefixes_and_boundaries(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("doc-lattice \\\n  linear", LINEAR),
        ("doc-lattice \\\n  reconcile --all", RECONCILE),
    ],
    ids=["linear", "mutating-reconcile"],
)
def test_direct_doc_lattice_invocations_handles_indented_command_continuations(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_direct_doc_lattice_invocations_does_not_continue_after_escaped_backslash(newline):
    script = "doc-lattice rec" + "\\\\" + newline + "oncile --dry-run"

    assert direct_doc_lattice_invocations(script) == (("rec" + "\\", False),)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("echo foo\r#notcomment; doc-lattice linear", LINEAR),
        ("echo\r doc-lattice linear", INCOMPLETE),
    ],
    ids=["hash-remains-word-text", "carriage-return-remains-command-text"],
)
def test_direct_doc_lattice_invocations_preserves_lone_carriage_returns(script, expected):
    if expected is INCOMPLETE:
        assert_marker_refusal(script)
        return
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("PATH+=:/tools doc-lattice linear --exit-code", (("linear", False),)),
        ('PATH+="$PATH_SUFFIX" doc-lattice linear', (("linear", False),)),
        (
            "FLAGS+=x uv run doc-lattice reconcile --all",
            (("reconcile", False),),
        ),
        (
            "FLAGS+=x uv run doc-lattice reconcile --all --dry-run",
            (("reconcile", True),),
        ),
    ],
)
def test_direct_doc_lattice_invocations_handles_bash_append_assignments(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_detects_long_assignment_prefix_run():
    # A long run of assignment-shaped words must not degrade command-position tracking into a
    # per-word rescan, and the trailing command must still be detected.
    assignments = " ".join(f"A{index}={index}" for index in range(5_000))
    script = f"{assignments} doc-lattice linear"

    assert direct_doc_lattice_invocations(script) == LINEAR


@pytest.mark.parametrize(
    "script",
    [
        "args=(doc-lattice linear)",
        "declare -a args=(doc-lattice reconcile --all)",
        "args=([1+(2)]=doc-lattice linear)",
        "args=(doc-lattice linear)\ndoc-lattice check",
    ],
    ids=["indexed", "declare-indexed", "arithmetic-subscript", "before-real-invocation"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_marker_bearing_array_literals(script):
    # An array literal such as ``cmds=(doc-lattice reconcile)`` feeds a later dynamic execution
    # (``"${cmds[@]}"``) the scanner cannot follow, so its retained marker fails closed exactly
    # like the scalar ``X=doc-lattice`` assignment.
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("args=($(doc-lattice linear))", LINEAR),
        ("args=(<(doc-lattice reconcile --all))", RECONCILE),
        ("files=(a.md b.md)\ndoc-lattice check", CHECK),
    ],
    ids=["command-substitution", "process-substitution", "marker-free-then-command"],
)
def test_direct_doc_lattice_invocations_scans_executable_array_contexts(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        "other-doc-lattice linear",
        "doc-lattice-helper linear",
        "$RUNNER_TEMP/doc-lattice-helper linear",
        "echo doc-lattice linear",
        "printf doc-lattice reconcile",
        "runner doc-lattice linear",
        "+=x doc-lattice linear",
        "FLAGS++=x doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_unresolved_marker_commands(script):
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    "script",
    ["doc-lattice --version linear", "doc-lattice --no-color --version linear"],
)
def test_direct_doc_lattice_invocations_keeps_resolved_nonexecuting_forms(script):
    assert direct_doc_lattice_invocations(script) == NONE


def test_direct_doc_lattice_invocations_fails_closed_on_unresolved_braced_marker_head():
    assert_marker_refusal("{doc-lattice linear")


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile --dry-runner",
        "doc-lattice reconcile '--dry-run value'",
    ],
)
def test_direct_doc_lattice_invocations_requires_a_distinct_dry_run_token(script):
    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile pc-design --config --dry-run",
        "doc-lattice reconcile pc-design --ref --dry-run",
        "doc-lattice reconcile pc-design --format --dry-run",
        "doc-lattice reconcile pc-design -- --dry-run",
        'doc-lattice reconcile pc-design "$OPTION" --dry-run',
        "doc-lattice reconcile pc-design --config $CONFIG --dry-run",
        "doc-lattice reconcile {pc-design,--config} --dry-run",
        "shopt -s nullglob; doc-lattice reconcile --config no-match-* --dry-run",
    ],
)
def test_direct_doc_lattice_invocations_requires_dry_run_to_be_an_effective_option(script):
    assert direct_doc_lattice_invocations(script) == RECONCILE


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile pc-design --config=.doc-lattice.yml --dry-run",
        "doc-lattice reconcile pc-design --config .doc-lattice.yml --dry-run",
        "doc-lattice reconcile pc-design --ref spec#section --dry-run",
        "doc-lattice reconcile pc-design --format human --dry-run",
        "doc-lattice reconcile pc-design --all --dry-run",
        "doc-lattice reconcile pc-design --dry-run --config .doc-lattice.yml",
    ],
)
def test_direct_doc_lattice_invocations_accepts_unconsumed_reconcile_dry_run_option(script):
    assert direct_doc_lattice_invocations(script) == RECONCILE_DRY


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice linear --help",
        "doc-lattice linear target --format human --indent 2 --help",
        "doc-lattice linear --exit-code --warn-exit --help",
        "doc-lattice reconcile --help",
        "doc-lattice reconcile pc-design --format human --help",
    ],
)
def test_direct_doc_lattice_invocations_ignores_effective_command_help(script):
    assert direct_doc_lattice_invocations(script) == NONE


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice linear --from --help",
        "doc-lattice linear --config --help",
        "doc-lattice linear --format --help",
        "doc-lattice linear --indent --help",
        "doc-lattice linear -- --help",
        'doc-lattice linear "$OPTION" --help',
    ],
)
def test_direct_doc_lattice_invocations_does_not_widen_consumed_linear_help(script):
    assert direct_doc_lattice_invocations(script) == LINEAR


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile pc-design --config --help",
        "doc-lattice reconcile -- --help",
    ],
)
def test_direct_doc_lattice_invocations_does_not_widen_consumed_reconcile_help(script):
    assert direct_doc_lattice_invocations(script) == RECONCILE


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile 'pc[1]' --dry-run",
        'doc-lattice reconcile "pc*" --dry-run',
        r"doc-lattice reconcile pc\? --dry-run",
        "doc-lattice reconcile $'pc[1]' --dry-run",
        "doc-lattice reconcile '{pc,design}' --dry-run",
        r"doc-lattice reconcile \{pc,design\} --dry-run",
        "doc-lattice reconcile {a}x,{b} --dry-run",
    ],
)
def test_direct_doc_lattice_invocations_ignores_protected_or_inactive_argv_metacharacters(script):
    assert direct_doc_lattice_invocations(script) == RECONCILE_DRY


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice linea{r,}",
        "doc-lattice {linear,reconcile}",
        "doc-lattice reconcil{e,}",
        "doc-lattice chec*",
        "doc-lattice li[n]ear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_brace_or_glob_expanded_subcommand(script):
    # A subcommand carrying active argv expansion (for example "linea{r,}") expands to a
    # different word at runtime (Bash runs "linear"), so the scanner cannot certify which
    # subcommand runs. Declining would silently approve the workflow, so the scan must fail
    # closed the same way it does for an unresolved uv or root option.
    with pytest.raises(ConfigError, match=r"shell scan.*brace or glob expansion"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice linea{r,}",
        "doc-lattice chec*",
    ],
)
def test_scan_doc_lattice_invocations_reports_incomplete_on_expanded_subcommand(script):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "subcommand word uses brace or glob expansion"


def test_scan_doc_lattice_invocations_fails_closed_on_mixed_dynamic_expanded_subcommand():
    result = scan_doc_lattice_invocations("Xlinear=linear; X=; doc-lattice $X{linear,}")

    assert result.invocations == NONE
    assert result.incomplete_reason == "subcommand word uses brace or glob expansion"


def test_scan_doc_lattice_invocations_fails_closed_on_expanded_uv_launcher_word():
    result = scan_doc_lattice_invocations("uv {run,doc-lattice} linear")

    assert result.invocations == NONE
    assert result.incomplete_reason == "uv command word uses brace or glob expansion"


def test_scan_doc_lattice_invocations_fails_closed_on_expanded_uv_tool_run_word():
    result = scan_doc_lattice_invocations("uv tool {run,doc-lattice} linear")

    assert result.invocations == NONE
    assert result.incomplete_reason == "uv command word uses brace or glob expansion"


@pytest.mark.parametrize("operator", ["?", "*", "+", "@", "!"])
def test_scan_doc_lattice_invocations_fails_closed_on_extglob_operator(operator):
    result = scan_doc_lattice_invocations(
        f"shopt -s extglob\ndoc-lattice {operator}(reconcile) --all"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason == "extglob expansion cannot be scanned safely"


def test_direct_doc_lattice_invocations_keeps_quoted_extglob_text_literal():
    assert direct_doc_lattice_invocations("doc-lattice '@(reconcile)' --all") == (
        ("@(reconcile)", False),
    )


@pytest.mark.parametrize(
    "script",
    [
        "{doc-lattice,} linear",
        "command {doc-lattice,} linear",
        "exec {doc-lattice,} linear",
        "builtin exec {doc-lattice,} linear",
        "time {doc-lattice,} linear",
        "coproc {doc-lattice,} linear",
        "coproc worker {doc-lattice,} linear",
        "uv run {doc-lattice,} linear",
        "uvx {doc-lattice,} linear",
    ],
)
def test_scan_doc_lattice_invocations_fails_closed_on_expanded_executable(script):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "executable word uses brace or glob expansion"


@pytest.mark.parametrize(
    "escape",
    [r"\0", r"\400", r"\x00", r"\u0000", r"\U00000000", r"\c@"],
)
@pytest.mark.parametrize(
    "template",
    [
        "$'doc-lattice{escape}suffix' linear",
        "doc-lattice $'linear{escape}suffix'",
    ],
    ids=["executable", "subcommand"],
)
def test_scan_doc_lattice_invocations_rejects_ansi_c_nul_escape(escape, template):
    result = scan_doc_lattice_invocations(template.format(escape=escape))

    assert result.invocations == NONE
    assert result.incomplete_reason == "ANSI-C quoted word decodes to NUL"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("doc-lattice reconcile {a,b}", RECONCILE),
        ("doc-lattice check {a,b}", CHECK),
        ("doc-lattice linear pc*", LINEAR),
    ],
)
def test_direct_doc_lattice_invocations_keeps_literal_subcommand_with_expanded_arguments(
    script,
    expected,
):
    # A brace or glob expansion in an argument position does not taint the literal subcommand,
    # which is still classified as usual.
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_keeps_dry_run_scoped_to_one_command():
    script = "doc-lattice reconcile --all; doc-lattice check --dry-run"

    assert direct_doc_lattice_invocations(script) == (
        ("reconcile", False),
        ("check", True),
    )


def test_direct_doc_lattice_invocations_discards_only_malformed_fragment():
    script = "doc-lattice check; echo 'unterminated"

    assert direct_doc_lattice_invocations(script) == (("check", False),)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("uvx --with requests doc-lattice linear", (("linear", False),)),
        (
            "uvx --index https://packages.example/simple doc-lattice linear",
            (("linear", False),),
        ),
        (
            "uvx --index=https://packages.example/simple -w requests doc-lattice check",
            (("check", False),),
        ),
        (
            "uv run --group dev doc-lattice reconcile --all",
            (("reconcile", False),),
        ),
        (
            "uv run --group=dev --with requests doc-lattice reconcile --dry-run",
            (("reconcile", True),),
        ),
        ("command -p doc-lattice linear", (("linear", False),)),
        ("command -- doc-lattice check", (("check", False),)),
        ("exec -a lattice doc-lattice reconcile --all", (("reconcile", False),)),
        ("exec -c doc-lattice lint", (("lint", False),)),
        ("2>/dev/null doc-lattice linear", (("linear", False),)),
        ("</dev/null 3>&1 command doc-lattice check", (("check", False),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_supported_wrappers_and_redirections(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("exec -ca fake doc-lattice linear", LINEAR),
        ("exec -la fake doc-lattice reconcile --all", RECONCILE),
        ("exec -cafake doc-lattice linear", LINEAR),
    ],
)
def test_direct_doc_lattice_invocations_consumes_clustered_exec_argv0(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_fails_closed_on_unsupported_static_exec_option():
    with pytest.raises(ConfigError, match=r"shell scan.*unsupported exec option"):
        direct_doc_lattice_invocations("exec -z ignored doc-lattice linear")


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("builtin exec doc-lattice linear", LINEAR),
        ("builtin command doc-lattice reconcile --all", RECONCILE),
        ("builtin -- exec -ca fake doc-lattice linear", LINEAR),
        ("builtin builtin command doc-lattice linear", LINEAR),
    ],
)
def test_direct_doc_lattice_invocations_follows_supported_builtin_targets(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_fails_closed_on_dynamic_builtin_target():
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations('builtin "$TARGET" doc-lattice linear')


@pytest.mark.parametrize(
    "script",
    [
        "builtin doc-lattice linear",
        "builtin env doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_unsupported_builtin_marker(script):
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "env -S 'doc-lattice linear'",
        "env -S'doc-lattice linear'",
        "env -iS 'doc-lattice linear'",
        "env -iS'doc-lattice linear'",
        "env --split-string 'doc-lattice linear'",
        "env --split-string='doc-lattice reconcile --all'",
    ],
    ids=[
        "short-separate-value",
        "short-attached-value",
        "short-cluster-separate-value",
        "short-cluster-attached-value",
        "long-separate-value",
        "long-equals-value",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_env_split_string(script):
    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "command env -S 'doc-lattice linear'",
        "exec env -S 'doc-lattice linear'",
        "/usr/bin/env -S 'doc-lattice linear'",
        "uv run env -S 'doc-lattice linear'",
        "uvx env -S 'doc-lattice linear'",
    ],
    ids=["command-wrapper", "exec-wrapper", "path-qualified", "uv-run", "uvx"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_wrapped_env_split_string(script):
    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "uv run /usr/bin/time -f '%e' doc-lattice linear",
        "/usr/bin/time -f '%e' doc-lattice linear",
        "env time -f '%e' doc-lattice linear",
        r"\time -f '%e' doc-lattice linear",
        "command time -f '%e' doc-lattice linear",
        "exec time -f '%e' doc-lattice linear",
    ],
    ids=[
        "nested",
        "path-qualified",
        "env-prefix",
        "escaped",
        "command-wrapper",
        "exec-wrapper",
    ],
)
def test_direct_doc_lattice_fails_closed_on_unknown_external_time_option(script):
    with pytest.raises(ConfigError, match=r"shell scan.*external time option"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        'uv run time "$*" doc-lattice linear',
        'uv run /usr/bin/time "$(printf -- -p)" doc-lattice linear',
        '/usr/bin/time "$*" doc-lattice linear',
        'env time "$*" doc-lattice linear',
    ],
    ids=["nested-time", "nested-path", "path-qualified", "env-prefix"],
)
def test_direct_doc_lattice_fails_closed_on_dynamic_external_time_prefix(script):
    with pytest.raises(ConfigError, match=r"shell scan.*dynamic external time prefix"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "FOO=\"$VALUE\" env -S 'doc-lattice linear'",
        "FOO=\"$VALUE\" command env -S 'doc-lattice linear'",
        "FOO=\"$VALUE\" exec env -S 'doc-lattice linear'",
        "FOO=\"$VALUE\" /usr/bin/env -S 'doc-lattice linear'",
    ],
    ids=["bare-env", "command-wrapper", "exec-wrapper", "path-qualified"],
)
def test_direct_doc_lattice_fails_closed_on_dynamic_assignment_before_env_split_string(
    script,
):
    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "$(true) env -S 'doc-lattice linear'",
        "$EMPTY env -S 'doc-lattice linear'",
        "$@ env -S 'doc-lattice linear'",
        "command $(true) env -S 'doc-lattice linear'",
        "exec $(true) env -S 'doc-lattice linear'",
        "time $(true) env -S 'doc-lattice linear'",
        "shopt -s nullglob; no-match-* env -S 'doc-lattice linear'",
        "{$EMPTY,} env -S 'doc-lattice linear'",
    ],
    ids=[
        "top-level-command-substitution",
        "top-level-empty-variable",
        "top-level-positional-at",
        "command-wrapper",
        "exec-wrapper",
        "time-prefix",
        "active-glob",
        "active-brace",
    ],
)
def test_direct_doc_lattice_fails_closed_on_erasable_boundary_before_env_split_string(
    script,
):
    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "\"$@\" env -S 'doc-lattice linear'",
        "\"${@}\" env -S 'doc-lattice linear'",
        "\"${@:1}\" env -S 'doc-lattice linear'",
        "\"${items[@]}\" env -S 'doc-lattice linear'",
        "\"${!DOES_NOT_EXIST@}\" env -S 'doc-lattice linear'",
        "declare -a items=(); declare -n VALUE='items[@]'; \"$VALUE\" env -S 'doc-lattice linear'",
        "command \"$@\" env -S 'doc-lattice linear'",
        "exec \"${@}\" env -S 'doc-lattice linear'",
        "time \"${@:1}\" env -S 'doc-lattice linear'",
        "coproc \"${items[@]}\" env -S 'doc-lattice linear'",
        "\"$@\" /usr/bin/env -S 'doc-lattice linear'",
    ],
    ids=[
        "positional-at",
        "braced-positional-at",
        "positional-at-offset",
        "array-at",
        "indirect-name-at",
        "unbraced-nameref",
        "command-wrapper",
        "exec-wrapper",
        "time-prefix",
        "coproc-prefix",
        "path-qualified-env",
    ],
)
def test_direct_doc_lattice_fails_closed_on_quoted_zero_field_boundary_before_env_split_string(
    script,
):
    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "$(true) doc-lattice linear",
        "command $(true) doc-lattice linear",
        "exec $(true) doc-lattice linear",
        "time $(true) doc-lattice linear",
        "shopt -s nullglob; no-match-* doc-lattice linear",
    ],
    ids=["top-level", "command-wrapper", "exec-wrapper", "time-prefix", "active-glob"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_erasable_command_boundary_before_payload(
    script,
):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        '"$@" doc-lattice linear',
        'command "${@}" doc-lattice linear',
        'exec "${items[@]}" doc-lattice linear',
        'time "${@:1}" doc-lattice linear',
        'coproc "${!DOES_NOT_EXIST@}" doc-lattice linear',
        "declare -a items=(); declare -n VALUE='items[@]'; \"$VALUE\" doc-lattice linear",
        "declare -a items=(); declare -n NAME='items[@]'; coproc \"$NAME\" doc-lattice linear",
    ],
    ids=[
        "top-level",
        "command-wrapper",
        "exec-wrapper",
        "time-prefix",
        "coproc-prefix",
        "unbraced-nameref",
        "coproc-unbraced-nameref",
    ],
)
def test_direct_doc_lattice_fails_closed_on_quoted_zero_field_boundary_before_payload(script):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        '"$(true)" doc-lattice linear',
        '"$*" doc-lattice linear',
        '"${items[*]}" doc-lattice linear',
        "declare -a items=(); declare -n VALUE='items[@]'; \"${VALUE}\" doc-lattice linear",
        "declare -a items=(); declare -n VALUE='items[@]'; \"prefix$VALUE\" doc-lattice linear",
    ],
    ids=[
        "command-substitution",
        "positional-star",
        "array-star",
        "braced-nameref",
        "static-literal-with-nameref",
    ],
)
def test_direct_doc_lattice_fails_closed_on_quoted_dynamic_head_with_marker(script):
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("command \"$OPT\" env -S 'doc-lattice linear'", "env split-string"),
        ("exec \"$OPT\" env -S 'doc-lattice linear'", "env split-string"),
        ("command -p \"$OPT\" env -S 'doc-lattice linear'", "env split-string"),
        ("exec -a label \"$OPT\" env -S 'doc-lattice linear'", "env split-string"),
        ('command "$OPT" doc-lattice linear', "command-position expansion"),
        ('exec "$OPT" doc-lattice linear', "command-position expansion"),
        ('command -p "$OPT" doc-lattice linear', "command-position expansion"),
        ('exec -a label "$OPT" doc-lattice linear', "command-position expansion"),
    ],
    ids=[
        "command-env",
        "exec-env",
        "command-option-env",
        "exec-option-env",
        "command-payload",
        "exec-payload",
        "command-option-payload",
        "exec-option-payload",
    ],
)
def test_direct_doc_lattice_fails_closed_on_dynamic_command_or_exec_wrapper_option(
    script,
    reason,
):
    with pytest.raises(ConfigError, match=rf"shell scan.*{reason}"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        'uv "$OPT" run doc-lattice linear',
        'uv "$OPT" tool run doc-lattice linear',
        'uv "$SUBCOMMAND" doc-lattice linear',
        "uv $OPT doc-lattice linear",
        'uv "$OPT" -- doc-lattice linear',
        'uv "$OPT" --offline doc-lattice linear',
        'uv "$OPT" --group dev doc-lattice linear',
        'uv "$OPT" run --from doc-lattice==2.1.0 doc-lattice linear',
        'uv "$GLOBAL" "$SUBCOMMAND" doc-lattice linear',
        'uv "$GLOBAL" tool "$RUN" doc-lattice linear',
        'uv run "$OPT" doc-lattice linear',
        'uvx "$OPT" doc-lattice linear',
        'uv tool "$OPT" doc-lattice linear',
        'doc-lattice "$OPT" linear',
    ],
    ids=[
        "uv-global-run",
        "uv-global-tool-run",
        "uv-dynamic-run",
        "uv-unquoted-dynamic-run-or-tool-run",
        "uv-dynamic-run-with-terminator",
        "uv-dynamic-run-with-flag",
        "uv-dynamic-run-with-option",
        "uv-dynamic-tool-run-with-option",
        "uv-dynamic-global-and-run",
        "uv-dynamic-global-and-tool-run",
        "uv-run",
        "uvx",
        "uv-tool-run",
        "root",
    ],
)
def test_direct_doc_lattice_fails_closed_on_dynamic_prefix_grammar_before_payload(script):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        'CMD=linear; doc-lattice "$CMD"',
        "CMD='reconcile --all'; doc-lattice $CMD",
    ],
    ids=["quoted-scalar", "unquoted-multiple-fields"],
)
def test_direct_doc_lattice_fails_closed_on_exhausted_dynamic_subcommand(script):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    ["doc-lattice", "doc-lattice --help", "doc-lattice --version"],
    ids=["bare", "root-help", "root-version"],
)
def test_direct_doc_lattice_allows_static_missing_or_nonexecuting_subcommand(script):
    assert direct_doc_lattice_invocations(script) == NONE


@pytest.mark.parametrize(
    "script",
    [
        "uv --directory $OPT doc-lattice linear",
        "uv --project $OPT doc-lattice linear",
        "uv --cache-dir $OPT doc-lattice linear",
        'uv --directory "${@:1}" doc-lattice linear',
        "uv --directory $OPT -- doc-lattice linear",
        "uv --directory $OPT --from doc-lattice==2.1.0 doc-lattice linear",
    ],
    ids=[
        "directory-unquoted",
        "project-unquoted",
        "cache-dir-unquoted",
        "directory-quoted-zero-field",
        "directory-run-terminator",
        "directory-tool-run-option",
    ],
)
def test_direct_doc_lattice_fails_closed_when_dynamic_uv_global_option_value_can_supply_launcher(
    script,
):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


def test_direct_doc_lattice_fails_closed_on_dynamic_uv_value_exposing_env_split_string():
    with pytest.raises(
        ConfigError,
        match=r"shell scan.*(?:env split-string|command-position expansion)",
    ):
        direct_doc_lattice_invocations("uv --directory $OPT env -S 'doc-lattice linear'")


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('uv run --group "${GROUP}" doc-lattice linear', LINEAR),
        ('uv --directory "${GROUP}" run doc-lattice linear', LINEAR),
        ('doc-lattice linear "$VALUE"', LINEAR),
        ('doc-lattice check "$(true)"', CHECK),
        ('uv run doc-lattice linear "$*"', LINEAR),
        ('uvx doc-lattice check "${items[*]}"', CHECK),
    ],
    ids=[
        "quoted-option-value",
        "quoted-global-option-value",
        "scalar-argument",
        "substitution-argument",
        "positional-star-argument",
        "array-star-argument",
    ],
)
def test_direct_doc_lattice_keeps_single_field_option_values_and_post_subcommand_arguments(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        'uv run --group "${@:1}" doc-lattice linear',
        'uv --directory "${@:1}" run doc-lattice linear',
    ],
    ids=["launcher-option-value", "global-option-value"],
)
def test_direct_doc_lattice_fails_closed_on_zero_field_option_value_before_payload(script):
    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        direct_doc_lattice_invocations(script)


def test_direct_doc_lattice_invocations_skips_dynamic_shell_assignment_before_command():
    assert direct_doc_lattice_invocations('FOO="$VALUE" doc-lattice linear') == LINEAR


@pytest.mark.parametrize(
    "script",
    [
        'FOO"$X"=bar doc-lattice linear',
        "FOO$X=bar doc-lattice linear",
    ],
    ids=["quoted-name-fragment", "unquoted-name-fragment"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_dynamic_assignment_name_before_marker(
    script,
):
    assert_marker_refusal(script)


def test_direct_doc_lattice_invocations_keeps_dynamic_argument_after_static_command():
    assert direct_doc_lattice_invocations("doc-lattice linear $(true)") == LINEAR


@pytest.mark.parametrize(
    "option",
    [
        "--s",
        "--sp",
        "--spl",
        "--spli",
        "--split",
        "--split-",
        "--split-s",
        "--split-st",
        "--split-str",
        "--split-stri",
        "--split-strin",
    ],
)
@pytest.mark.parametrize("value_separator", [" ", "="], ids=["separate-value", "equals-value"])
def test_direct_doc_lattice_invocations_fails_closed_on_env_split_string_long_option_abbreviation(
    option,
    value_separator,
):
    script = f"env {option}{value_separator}'doc-lattice linear'"

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    ["env -aS doc-lattice linear", "env -uS doc-lattice linear", "env -CS doc-lattice linear"],
    ids=["argv0", "unset", "chdir"],
)
def test_direct_doc_lattice_invocations_handles_env_short_option_value_attached_to_short_option(
    script,
):
    assert direct_doc_lattice_invocations(script) == LINEAR


@pytest.mark.parametrize(
    "script",
    [
        "env -u NAME doc-lattice linear",
        "env --unset NAME doc-lattice linear",
        "env -C /tmp doc-lattice linear",
        "env --chdir /tmp doc-lattice linear",
    ],
    ids=["short-unset", "long-unset", "short-chdir", "long-chdir"],
)
def test_direct_doc_lattice_invocations_handles_static_env_option_values(script):
    assert direct_doc_lattice_invocations(script) == LINEAR


@pytest.mark.parametrize(
    "script",
    [
        "env --uns NAME doc-lattice linear",
        "env --ch /tmp doc-lattice linear",
        "env --arg fake doc-lattice linear",
        "env -iu NAME doc-lattice linear",
        "env -iC /tmp doc-lattice linear",
        "env -ia fake doc-lattice linear",
    ],
    ids=[
        "abbreviated-unset",
        "abbreviated-chdir",
        "abbreviated-argv0",
        "clustered-unset",
        "clustered-chdir",
        "clustered-argv0",
    ],
)
def test_direct_doc_lattice_invocations_consumes_env_option_values(script):
    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_fails_closed_on_unsupported_static_env_option():
    with pytest.raises(ConfigError, match=r"shell scan.*unsupported env option"):
        direct_doc_lattice_invocations("env --future-option ignored doc-lattice linear")


@pytest.mark.parametrize(
    "script",
    ["env -u", "env --unset", "env -C", "env --chdir"],
    ids=["short-unset", "long-unset", "short-chdir", "long-chdir"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_missing_env_option_value(script):
    with pytest.raises(ConfigError, match=r"shell scan.*env option value"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "env -u $OPTIONS harmless",
        'env --unset "$REF" harmless',
        'env -C "${OPTIONS[@]}" harmless',
        'env --chdir "${!REF}" harmless',
    ],
    ids=["unquoted", "quoted-reference", "quoted-array", "quoted-indirect"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_dynamic_env_option_value(script):
    with pytest.raises(ConfigError, match=r"shell scan.*env option value"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    ["env -{u,S} ignored 'doc-lattice linear'", "env -? ignored 'doc-lattice linear'"],
    ids=["brace-expansion", "glob"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_expandable_env_prefix(script):
    with pytest.raises(ConfigError, match=r"shell scan.*expandable env prefix"):
        direct_doc_lattice_invocations(script)


def test_direct_doc_lattice_invocations_fails_closed_on_dynamic_env_split_string_prefix():
    # EMPTY can be empty at runtime, turning this into the valid GNU abbreviation `--spl`.
    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        direct_doc_lattice_invocations("env --spl\"$EMPTY\" 'doc-lattice linear'")


@pytest.mark.parametrize(
    "script",
    [
        "env -i\"$OPTION\" 'doc-lattice linear'",
        "env --\"$OPTION\" 'doc-lattice reconcile --all'",
    ],
    ids=["short-option", "long-option"],
)
def test_direct_doc_lattice_invocations_fails_closed_on_dynamic_env_option_prefix(script):
    with pytest.raises(ConfigError, match=r"shell scan.*dynamic env"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        'env FOO="$VALUE" doc-lattice linear',
        'env FOO="${VALUE}" doc-lattice linear',
        # REF can be a nameref targeting an array reference such as `items[@]`.
        'env FOO="$REF" harmless',
        'env FOO="$(printf value)" doc-lattice linear',
        'env FOO="$@" harmless',
        'env FOO="${@:1}" harmless',
        'env FOO="${@#x}" harmless',
        'env FOO="${!@}" harmless',
        'env FOO="${!REF}" harmless',
        'env FOO="${VAR:+$@}" harmless',
        'env FOO="${OPTIONS[@]}" harmless',
        'env FOO="${!OPTION_PREFIX@}" harmless',
    ],
    ids=[
        "scalar-reference",
        "braced-scalar-reference",
        "potential-nameref",
        "command-substitution",
        "positional-at",
        "positional-slice",
        "positional-prefix-removal",
        "indirect-positional-at",
        "indirect-reference",
        "nested-positional-at",
        "array-at",
        "named-parameter-at",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_quoted_dynamic_env_assignment(script):
    with pytest.raises(ConfigError, match=r"shell scan.*quoted dynamic env assignment"):
        direct_doc_lattice_invocations(script)


def test_direct_doc_lattice_invocations_fails_closed_on_unquoted_dynamic_env_assignment():
    with pytest.raises(ConfigError, match=r"shell scan.*unquoted dynamic env assignment"):
        direct_doc_lattice_invocations("env FOO=$OPTIONS doc-lattice linear")


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("env FOO-BAR=x doc-lattice linear", LINEAR),
        ("env 1FOO=x doc-lattice reconcile --all", RECONCILE),
        ("env =x doc-lattice linear", LINEAR),
    ],
    ids=["punctuation-name", "leading-digit-name", "empty-name"],
)
def test_direct_doc_lattice_invocations_consumes_every_gnu_env_assignment_operand(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("env -- X=1 doc-lattice linear", LINEAR),
        ("env -- X=1 doc-lattice reconcile --all", RECONCILE),
        ("env -- -=x doc-lattice linear", LINEAR),
    ],
    ids=["linear", "mutating-reconcile", "dash-name"],
)
def test_direct_doc_lattice_invocations_consumes_env_assignments_after_option_terminator(
    script,
    expected,
):
    assert direct_doc_lattice_invocations(script) == expected


def test_direct_doc_lattice_invocations_fails_closed_on_env_terminator_before_marker():
    assert_marker_refusal("env -- -S doc-lattice linear")


@pytest.mark.parametrize(
    "script",
    [
        "command -v doc-lattice linear",
        "command -V doc-lattice linear",
        "command -pv doc-lattice linear",
        "uv run --module doc-lattice linear",
        "uv run --module=doc-lattice linear",
        "uv run -m doc-lattice linear",
        "uv run -mdoc-lattice linear",
        "uv run --script doc-lattice linear",
        "uv run -s doc-lattice linear",
        "uv run --gui-script doc-lattice linear",
        "uv --help run doc-lattice linear",
        "uv -h run doc-lattice linear",
        "uv --version run doc-lattice linear",
        "uv -V run doc-lattice linear",
        "uvx --help doc-lattice linear",
        "uvx -h doc-lattice linear",
        "uvx --version doc-lattice linear",
        "uvx -V doc-lattice linear",
        "uv run --help doc-lattice linear",
        "uv run -h doc-lattice linear",
        "uv tool run --help doc-lattice linear",
        "uv tool run -h doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_nonexecuting_marker_forms(script):
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("if false; then :; else doc-lattice linear; fi", LINEAR),
        ("if false; then :; else doc-lattice reconcile --all; fi", RECONCILE),
    ],
)
def test_direct_doc_lattice_invocations_detects_else_branch_commands(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("uv tool run doc-lattice linear", LINEAR),
        (
            "uv tool run --from doc-lattice==2.1.0 doc-lattice reconcile --all",
            RECONCILE,
        ),
        ("uvx doc-lattice@2.0.0 linear", LINEAR),
        ("uvx doc-lattice@latest reconcile --all", RECONCILE),
        ("uvx doc-lattice==2.0.0 linear", LINEAR),
        ("uvx ' doc-lattice==2.0.0 ' linear", LINEAR),
        ("uv tool run 'doc-lattice ' linear", LINEAR),
        ("uvx 'doc-lattice (>=2.0.0)' linear", LINEAR),
        ("uvx 'doc_lattice[cli]~=2.0' reconcile --all", RECONCILE),
        ("uvx 'doc.lattice @ https://example.invalid/doc-lattice.whl' linear", LINEAR),
        ("uv tool run doc-lattice@2.0.0 linear", LINEAR),
        ("uv tool run 'DOC_LATTICE>=2.0.0' linear", LINEAR),
        ("uv tool run 'doc-lattice!=2.0.0' reconcile --all", RECONCILE),
        ("uv -q run doc-lattice linear", LINEAR),
        ("uv -q run doc-lattice reconcile --all", RECONCILE),
        ("uv --color=always run doc-lattice reconcile --all", RECONCILE),
        ("uv --directory /repo run doc-lattice linear", LINEAR),
        ("uv --no-cache tool run doc-lattice linear", LINEAR),
        ("uv -q tool run doc-lattice@2.0.0 reconcile --all", RECONCILE),
        ("uvx doc-lattice@2.0.0 reconcile --dry-run", RECONCILE_DRY),
        (
            "uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice check",
            CHECK,
        ),
    ],
)
def test_direct_doc_lattice_invocations_recognizes_uv_launcher_spellings(script, expected):
    assert direct_doc_lattice_invocations(script) == expected


@pytest.mark.parametrize("option", ["-q", "--offline", "--no-cache", "--frobnicate", "--"])
def test_uv_tool_option_before_run_selector_fails_closed(option):
    script = f"uv tool {option} run doc-lattice linear"

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "uv tool option before the run selector"
    with pytest.raises(ConfigError, match=r"uv tool option before the run selector"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    ("subcommand", "expected_complete"),
    [("install doc-lattice", False), ("list", True)],
    ids=["marker-bearing-install", "marker-free-list"],
)
def test_uv_tool_bare_non_run_subcommand_applies_marker_fallback(
    subcommand,
    expected_complete,
):
    script = f"uv tool {subcommand}"
    if not expected_complete:
        assert_marker_refusal(script)
        return

    result = scan_doc_lattice_invocations(script)
    assert result.incomplete_reason is None
    assert result.invocations == NONE
    assert direct_doc_lattice_invocations(script) == NONE


@pytest.mark.parametrize(
    "script",
    [
        'OPT=-q; uv tool "$OPT" run doc-lattice linear',
        'OPT=--directory; uv tool "$OPT" /tmp run doc-lattice linear',
        'OPT=-q; uv tool "$OPT" --directory /tmp run doc-lattice linear',
    ],
    ids=["flag", "separate-value", "following-value-option"],
)
def test_uv_tool_dynamic_option_before_literal_run_fails_closed(script):

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None
    with pytest.raises(ConfigError, match=r"shell scan incomplete"):
        direct_doc_lattice_invocations(script)


def test_uv_tool_dynamic_selector_probe_is_bounded():
    script = "uv tool " + " ".join(['"$OPT"'] * 1_100) + " run doc-lattice linear"

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None
    with pytest.raises(ConfigError, match=r"shell scan incomplete"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize("launcher", ["uvx", "uv tool run"])
def test_package_form_no_sync_fails_closed(launcher):
    script = f"{launcher} --no-sync doc-lattice linear"

    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "unresolved uv launcher option"
    with pytest.raises(ConfigError, match=r"unresolved uv launcher option"):
        direct_doc_lattice_invocations(script)


def test_uv_run_no_sync_still_resolves():
    assert direct_doc_lattice_invocations("uv run --no-sync doc-lattice linear") == LINEAR


@pytest.mark.parametrize(
    ("script", "expected_invocations", "complete"),
    [
        # Option before the run selector: fail closed with no invocation (PR #103).
        ("uv tool -q run doc-lattice linear", NONE, False),
        ("uv tool --offline run doc-lattice linear", NONE, False),
        ("uv tool --no-cache run doc-lattice linear", NONE, False),
        ("uv tool --frobnicate run doc-lattice linear", NONE, False),
        ("uv tool -q install doc-lattice", NONE, False),
        # Bare uv tool selectors that are not run: no invocation, resolved cleanly.
        ("uv tool install doc-lattice", NONE, False),
        ("uv tool list", NONE, True),
        # Package-form launchers refuse --no-sync (PR #103): fail closed, no invocation.
        ("uvx --no-sync doc-lattice linear", NONE, False),
        ("uv tool run --no-sync doc-lattice linear", NONE, False),
        # uv run keeps its project environment, so --no-sync resolves normally.
        ("uv run --no-sync doc-lattice linear", LINEAR, True),
    ],
)
def test_scanner_issue_102_fixtures_stay_fail_closed(script, expected_invocations, complete):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == expected_invocations
    if complete:
        assert result.incomplete_reason is None
    else:
        assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "flag",
    [
        "--all-extras",
        "--all-groups",
        "--all-packages",
        "--compile-bytecode",
        "--exact",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-default-groups",
        "--no-index",
        "--no-sources",
        "--only-dev",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "-U",
        "-n",
    ],
)
def test_direct_doc_lattice_invocations_recognizes_documented_uv_run_flags(flag):
    assert (
        direct_doc_lattice_invocations(f"uv run {flag} doc-lattice reconcile --dry-run")
        == RECONCILE_DRY
    )


@pytest.mark.parametrize("launcher", ["uvx", "uv tool run"])
@pytest.mark.parametrize(
    "flag",
    [
        "--compile-bytecode",
        "--lfs",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-index",
        "--no-sources",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "-U",
        "-n",
    ],
)
def test_direct_doc_lattice_invocations_recognizes_documented_uv_tool_run_flags(
    launcher,
    flag,
):
    assert (
        direct_doc_lattice_invocations(f"{launcher} {flag} doc-lattice reconcile --dry-run")
        == RECONCILE_DRY
    )


@pytest.mark.parametrize("launcher", ["uvx", "uv run", "uv tool run"])
def test_direct_doc_lattice_invocations_recognizes_clustered_uv_launcher_flags(launcher):
    assert direct_doc_lattice_invocations(f"{launcher} -qv doc-lattice linear") == LINEAR


def test_direct_doc_lattice_invocations_keeps_marker_free_uv_non_launcher_form():
    assert direct_doc_lattice_invocations("uv sync") == NONE


@pytest.mark.parametrize(
    "script",
    [
        "uv pip install doc-lattice",
        "uvx other-doc-lattice==2.0.0 linear",
        "uvx doc-lattice-tools>=2.0.0 linear",
        "uv run doc-lattice@2.0.0 linear",
        "uv run command doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_marker_bearing_uv_non_launcher_forms(
    script,
):
    assert_marker_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "uv --frobnicate run doc-lattice linear",
        "uv -Z run doc-lattice linear",
        "uv --opt$X run doc-lattice linear",
        "uv --frobnicate tool run doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_unknown_uv_option(script):
    with pytest.raises(ConfigError, match=r"shell scan.*uv"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "uvx --future-opt value doc-lattice linear",
        "uvx --with requests --future-opt value doc-lattice linear",
        "uvx -qZ doc-lattice linear",
        "uv run --future-opt value doc-lattice reconcile --all",
        "uv tool run --future-opt value doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_unknown_launcher_option(script):
    # A future uv launcher option that takes a value would otherwise consume the payload word
    # and hide the invocation, so an unrecognized launcher option must fail closed.
    with pytest.raises(ConfigError, match=r"shell scan.*uv launcher option"):
        direct_doc_lattice_invocations(script)


def test_direct_doc_lattice_invocations_resolves_rendered_uvx_spelling():
    script = "uvx --python 3.13 --from doc-lattice==2.0.0 doc-lattice check"

    assert direct_doc_lattice_invocations(script) == CHECK


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice --future-root-opt X linear",
        "doc-lattice --no-color --future-root-opt X reconcile --all",
        "uvx --from doc-lattice==2.1.0 doc-lattice --future-root-opt X linear",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_unknown_root_option(script):
    # A future doc-lattice root option could consume its successor, so an unrecognized static
    # root option before the subcommand must fail closed rather than mis-read the subcommand.
    with pytest.raises(ConfigError, match=r"shell scan.*doc-lattice root option"):
        direct_doc_lattice_invocations(script)


def test_scanner_covers_every_typer_root_option():
    # Lockstep guard: any root option added to the CLI must be classified by the scanner, or
    # an unclassified option would fail closed on real repository workflows.
    command = get_command(create_app())
    exposed: set[str] = set()
    for param in command.params:
        exposed.update(getattr(param, "opts", ()))
        exposed.update(getattr(param, "secondary_opts", ()))
    option_names = {name for name in exposed if name.startswith("-")}
    covered = _DOC_LATTICE_ROOT_OPTIONS | _DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS

    assert option_names, "expected the Typer root callback to expose at least one option"
    assert option_names <= covered


def test_scanner_reconcile_option_grammar_matches_typer_command():
    root = get_command(create_app())
    assert isinstance(root, TyperGroup)
    command = root.commands["reconcile"]
    value_options: set[str] = set()
    flags: set[str] = set()
    for param in command.params:
        option_names = {name for name in getattr(param, "opts", ()) if name.startswith("-")}
        if getattr(param, "is_flag", False):
            flags.update(option_names)
        else:
            value_options.update(option_names)

    assert value_options == _RECONCILE_OPTIONS_WITH_ARGUMENTS
    assert flags == _RECONCILE_FLAGS


def test_shell_scan_incomplete_is_a_coded_project_error():
    error = _ShellScanIncomplete("step limit exceeded")

    assert isinstance(error, ProjectError)
    assert error.code == "SHELL_SCAN_INCOMPLETE"


def test_nested_dynamic_uv_resolution_charges_shared_scan_budget():
    script = " ".join(["uv $X"] * 18 + ["doc-lattice linear"])
    scanner = _ShellScanner(script, budget=_ScanBudget(200))

    with pytest.raises(_ShellScanIncomplete, match="step limit exceeded"):
        scanner.scan()


def test_child_scanner_supports_legacy_constructor_subclass(monkeypatch: pytest.MonkeyPatch):
    class LegacyChildScanner(_ShellScanner):
        def __init__(
            self,
            source: str,
            *,
            budget: _ScanBudget | None = None,
            invocations: list[tuple[str, bool]] | None = None,
            classify_commands: bool = True,
        ) -> None:
            super().__init__(
                source,
                budget=budget,
                invocations=invocations,
                classify_commands=classify_commands,
            )

    monkeypatch.setattr(shell_scanner, "_ShellScanner", LegacyChildScanner)

    assert direct_doc_lattice_invocations("echo `doc-lattice linear`") == LINEAR


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('doc-"lattice"', True),
        ("DOC_LATTICE", True),
        ("doc...lattice", True),
        ("doc-lattıce", False),  # noqa: RUF001 -- intentional dotless-i regression case
    ],
    ids=["composed-fragments", "ascii-casefold", "repeated-separators", "dotless-i"],
)
def test_shell_word_marker_fact_matches_composed_ascii_regex(source, expected):
    scanner = _ShellScanner(source, classify_commands=False)

    word, end = scanner._parse_word(0, len(source), 0)

    assert end == len(source)
    assert word.has_doc_lattice_marker is expected


def test_command_marker_fact_aggregates_and_resets():
    source = "doc-lattice"
    scanner = _ShellScanner(source, classify_commands=False)
    state = _CommandScanState(words=[], heredocs=[], cases=[])
    word, end = scanner._parse_word(0, len(source), 0)

    scanner._record_word(state, word)

    assert end == len(source)
    assert state.command_has_marker is True
    state.reset_command()
    assert state.words == []
    assert state.command_has_marker is False


@pytest.mark.parametrize("suffix", ["", "doc-lattice"], ids=["marker-free", "marker-bearing"])
def test_long_finalized_word_marker_scan_does_not_charge_step_budget(suffix):
    source = "'" + ("x" * 100_000) + suffix + "'"
    budget = _ScanBudget(3)
    scanner = _ShellScanner(source, budget=budget, classify_commands=False)

    scanner.scan()

    assert budget.remaining_steps == 1


def test_direct_doc_lattice_invocations_prefixes_context_on_incomplete_scan():
    script = 'echo "' + ("$(" * 65) + "doc-lattice linear" + (")" * 65) + '"'

    with pytest.raises(ConfigError, match=r"\.github/workflows/x\.yml: shell scan incomplete"):
        direct_doc_lattice_invocations(script, context=".github/workflows/x.yml")

    with pytest.raises(ConfigError, match=r"^shell scan incomplete"):
        direct_doc_lattice_invocations(script)


def test_scan_doc_lattice_invocations_reports_incomplete_reason_without_raising():
    script = 'echo "' + ("$(" * 65) + "doc-lattice linear" + (")" * 65) + '"'

    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is not None
    assert "recursion limit" in result.incomplete_reason


def test_direct_doc_lattice_invocations_ignores_heredoc_bodies():
    script = """\
doc-lattice check
cat <<'POLICY'
doc-lattice linear
uv run doc-lattice reconcile --all
POLICY
doc-lattice lint
"""

    assert direct_doc_lattice_invocations(script) == (
        ("check", False),
        ("lint", False),
    )


def test_direct_doc_lattice_invocations_keeps_command_with_and_after_heredoc():
    script = """\
doc-lattice check <<-EOF
	doc-lattice linear
	EOF
doc-lattice lint
"""

    assert direct_doc_lattice_invocations(script) == (
        ("check", False),
        ("lint", False),
    )


def test_direct_doc_lattice_invocations_strips_tabs_from_continued_dash_heredoc_lines():
    script = "cat <<-EOF\n\\\n\tEOF\ndoc-lattice linear\n"

    assert direct_doc_lattice_invocations(script) == LINEAR


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_direct_doc_lattice_invocations_preserves_quoted_heredoc_continuation(newline):
    script = f"cat <<'EOF'{newline}body \\{newline}EOF{newline}doc-lattice linear{newline}"

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_keeps_command_after_here_string():
    script = "cat <<< harmless\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_keeps_command_after_arithmetic_shift():
    script = "(( x = 1 << 2 ))\ndoc-lattice reconcile --all"

    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


def test_direct_doc_lattice_invocations_assembles_quoted_heredoc_delimiter_word():
    script = """\
cat <<'E'OF
harmless
EOF
doc-lattice linear
"""

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_retains_quoted_empty_heredoc_delimiter():
    script = "cat <<''\n'unclosed\n\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_consumes_complete_literal_heredoc_delimiter_word():
    script = "cat <<$(printf EOF)\nbody\n$(printf EOF)\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_does_not_execute_expansion_syntax_in_heredoc_word():
    script = (
        "cat <<$(uv $X doc-lattice linear)\nbody\n$(uv $X doc-lattice linear)\ndoc-lattice check"
    )

    assert direct_doc_lattice_invocations(script) == CHECK


def test_direct_doc_lattice_invocations_preserves_non_special_double_quote_escape():
    script = 'cat <<"E\\OF"\nharmless\nE\\OF\ndoc-lattice linear\n'

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_detects_legacy_command_substitution():
    script = "echo `doc-lattice linear`"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_detects_legacy_substitution_in_double_quotes():
    script = 'echo "`doc-lattice linear`"'

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


@pytest.mark.parametrize(
    "script",
    [
        "echo '`doc-lattice linear`'",
        r"echo \`doc-lattice linear\`",
    ],
)
def test_direct_doc_lattice_invocations_fails_closed_on_literal_backtick_marker(script):
    assert_marker_refusal(script)


def test_direct_doc_lattice_invocations_keeps_command_after_comment_line():
    script = "# setup\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_keeps_command_after_trailing_comment():
    script = "echo setup # harmless\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_removes_ansi_c_quoted_heredoc_body():
    script = """\
cat <<$'EOF'
harmless
EOF
doc-lattice linear
"""

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_fails_closed_on_literal_backtick_marker_in_substitution():
    script = '''echo "$(printf '%s' '`doc-lattice linear`')"'''

    assert_marker_refusal(script)


def test_direct_doc_lattice_invocations_detects_active_backticks_in_substitution():
    script = '''echo "$(printf '%s' `doc-lattice linear`)"'''

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_ignores_heredoc_text_in_comment():
    script = "# note <<EOF\ndoc-lattice linear"

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_fails_closed_on_locale_translated_executable():
    with pytest.raises(ConfigError, match=r"shell scan.*locale-translated executable"):
        direct_doc_lattice_invocations('$"harmless" linear')


def test_direct_doc_lattice_invocations_fails_closed_on_locale_translated_heredoc_delimiter():
    script = 'cat <<$"harmless"\nEOF\ndoc-lattice linear\nharmless\n'

    with pytest.raises(ConfigError, match=r"shell scan.*locale-translated heredoc delimiter"):
        direct_doc_lattice_invocations(script)


def test_direct_doc_lattice_invocations_allows_locale_translated_non_executable_argument():
    assert direct_doc_lattice_invocations('printf %s $"harmless"') == NONE


def test_direct_doc_lattice_invocations_keeps_hash_inside_shell_word():
    script = "doc-lattice reconcile --all --ref --dry-run#suffix"

    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


def test_direct_doc_lattice_invocations_fails_closed_on_literal_marker_after_parameter_expansion():
    script = '''echo "$(printf %s ${x:-)}; printf '%s' '`doc-lattice linear`')"'''

    assert_marker_refusal(script)


def test_direct_doc_lattice_invocations_scans_process_substitution_in_parameter_word():
    script = "unset x; echo ${x:-<(doc-lattice linear)}"

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_keeps_process_substitution_joined_to_word_suffix():
    script = "echo <(true)#notcomment; doc-lattice linear"

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_tracks_case_pattern_parentheses_in_substitution():
    script = 'echo "$(case x in x) doc-lattice linear;; esac)"'

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_tracks_dynamic_case_subject_in_substitution():
    script = 'echo "$(case "$x" in x) doc-lattice linear;; esac)"'

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_expands_single_quotes_inside_quoted_parameter():
    script = '''unset x; echo "${x:-'$(doc-lattice linear)'}"'''

    assert direct_doc_lattice_invocations(script) == LINEAR


def test_direct_doc_lattice_invocations_honors_escaped_dollar_inside_parameter():
    script = r'unset x; echo "${x:-\$(doc-lattice linear)}"'

    assert direct_doc_lattice_invocations(script) == NONE


def test_direct_doc_lattice_invocations_fails_closed_at_recursion_limit():
    script = 'echo "' + ("$(" * 65) + "doc-lattice linear" + (")" * 65) + '"'

    with pytest.raises(ConfigError, match=r"shell scan.*recursion limit"):
        direct_doc_lattice_invocations(script)


@pytest.mark.parametrize(
    "script",
    [
        "echo doc-lattice reconcile",
        "find . -name 'doc-lattice*'",
        "command -v doc-lattice",
    ],
    ids=["unknown-head", "find-operand", "command-query"],
)
def test_marker_bearing_non_invocation_fails_closed(script):
    assert_marker_refusal(script)


def test_function_shadow_form_fails_closed():
    script = """\
echo() { eval "$CMD"; }
CMD='doc-lattice reconcile' echo done
"""

    assert_marker_refusal(script)


@pytest.mark.parametrize(
    "script",
    ['echo doc-"lattice"', "echo DOC_LATTICE", "echo doc...lattice"],
    ids=["composed-fragments", "ascii-casefold", "repeated-separators"],
)
def test_composed_ascii_marker_under_unknown_head_fails_closed(script):
    assert_marker_refusal(script)


def test_non_ascii_near_marker_under_unknown_head_stays_certified():
    assert direct_doc_lattice_invocations("echo doc-latt\u0131ce") == NONE


def test_marker_bearing_non_invocation_reason_names_certification_failure():
    result = scan_doc_lattice_invocations("echo doc-lattice reconcile")

    assert (
        result.incomplete_reason
        == "marker-bearing command is not a certified doc-lattice invocation"
    )


def test_command_marker_state_resets_between_simple_commands():
    result = scan_doc_lattice_invocations("doc-lattice --help; echo ok")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_redirection_target_marker_is_out_of_scope():
    result = scan_doc_lattice_invocations("bash -c 'echo hi' > doc-lattice.log")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


# A retained doc-lattice marker under any command the resolver does not classify as doc-lattice
# fails closed. The original issue #105 dispatcher rows remain here as empirical regression
# knowledge, but dispatcher reachability is no longer the certification boundary.
MARKER_REFUSE_CASES = [
    ("bash -c marker payload", "bash -c 'doc-lattice reconcile'"),
    ("eval marker payload", 'eval "doc-lattice $X"'),
    ("sh short-option cluster", "sh -lc 'doc-lattice reconcile'"),
    ("bash operand becomes arg0", "bash -c 'echo ok' doc-lattice"),
    ("bash value-less long option before -c", "bash --norc -c 'doc-lattice check'"),
    ("dynamic dispatcher selector", "bash $OPT 'doc-lattice lint'"),
    ("source plain head marker argv", "source ./doc-lattice-env.sh"),
    ("dot plain head marker argv", ". ./doc-lattice-env.sh"),
    ("dash head inline command", "dash -c 'doc-lattice reconcile --all'"),
    ("zsh head inline command", "zsh -c 'doc-lattice reconcile'"),
    ("bash value option before -c", "bash -o pipefail -c 'doc-lattice reconcile'"),
    ("assignment prefix before dispatcher", "FOO=1 bash -c 'doc-lattice reconcile'"),
    ("nested command substitution dispatch", "echo $(bash -c 'doc-lattice reconcile')"),
    ("command wrapper before dispatcher", "command bash -c 'doc-lattice reconcile'"),
    ("env wrapper before dispatcher", "env bash -c 'doc-lattice linear'"),
    ("exec wrapper before dispatcher", "exec bash -c 'doc-lattice reconcile'"),
    ("time keyword before dispatcher", "time bash -c 'doc-lattice reconcile'"),
    ("env options and assignment before dispatcher", "env -i PATH=/x bash -c 'doc-lattice lint'"),
    ("command wrapper before plain eval head", "command eval 'doc-lattice reconcile'"),
    ("builtin chain before dispatcher", "builtin command bash -c 'doc-lattice reconcile'"),
    ("coproc before dispatcher", "coproc bash -c 'doc-lattice reconcile'"),
    ("coproc name before dispatcher", "coproc worker bash -c 'doc-lattice reconcile'"),
    ("plus cluster inline command", "bash +c 'doc-lattice linear'"),
    ("plus cluster after value option", "bash +O extglob +c 'doc-lattice reconcile'"),
    ("zsh emulate mode before -c", "zsh --emulate sh -c 'doc-lattice linear'"),
    ("windows shell launcher", "bash.exe -c 'doc-lattice linear'"),
    ("windows shell launcher casefolds", "SH.EXE -c 'doc-lattice reconcile'"),
    ("uv run launcher before dispatcher", "uv run bash -c 'doc-lattice reconcile'"),
    ("uvx launcher before dispatcher", "uvx bash -c 'doc-lattice reconcile'"),
    ("uv tool run launcher before dispatcher", "uv tool run bash -c 'doc-lattice reconcile'"),
    ("env time chain before dispatcher", "env time bash -c 'doc-lattice reconcile'"),
    ("builtin eval head", "builtin eval 'doc-lattice reconcile'"),
    ("builtin source head", "builtin source ./doc-lattice-env.sh"),
    ("coprocess plain dispatcher head", "coproc eval 'doc-lattice reconcile'"),
    ("marker-bearing assignment prefix", "CMD='doc-lattice reconcile' sh -c \"$CMD\""),
    ("env assignment carries marker", "env CMD='doc-lattice reconcile' sh -c \"$CMD\""),
    ("rbash restricted head inline command", "rbash -c 'doc-lattice reconcile'"),
    ("rzsh restricted head inline command", "rzsh -c 'doc-lattice linear'"),
    ("dynamic short option value smuggles inline", "bash -o $X 'doc-lattice reconcile'"),
    ("dynamic long option value smuggles inline", "bash --rcfile $X 'doc-lattice reconcile'"),
    ("quoted unbraced option value smuggles inline", "bash -o \"$X\" 'doc-lattice reconcile'"),
    ("lone plus before -c", "bash + -c 'doc-lattice reconcile'"),
    ("sh lone plus before cluster", "sh + -lc 'doc-lattice reconcile'"),
    ("zsh lone plus before -c", "zsh + -c 'doc-lattice reconcile'"),
    ("zsh -b terminator before -c", "zsh -b -c 'doc-lattice reconcile'"),
    ("uvx requirement launcher before dispatcher", "uvx bash@1.0 -c 'doc-lattice reconcile'"),
    (
        "dynamic uv provenance-distinct requirement head",
        "uv $X bash@1.0 -c 'doc-lattice reconcile'",
    ),
    ("uv tool run requirement head", "uv tool run bash@1.0 -c 'doc-lattice reconcile'"),
    ("uvx requirement specifier before dispatcher", "uvx 'bash==1.0' -c 'doc-lattice reconcile'"),
    (
        "uvx named direct requirement before dispatcher",
        "uvx 'bash@file:///tmp/bash-1.0-py3-none-any.whl' -c 'doc-lattice reconcile'",
    ),
    (
        "uv tool run spaced named direct requirement before dispatcher",
        "uv tool run 'bash @ file:///tmp/bash-1.0-py3-none-any.whl' -c 'doc-lattice reconcile'",
    ),
    (
        "uvx trailing-whitespace requirement before dispatcher",
        "uvx 'bash ' -c 'doc-lattice reconcile'",
    ),
    (
        "uv tool run surrounding-whitespace requirement before dispatcher",
        "uv tool run ' bash ' -c 'doc-lattice reconcile'",
    ),
    (
        "uvx path-only requirement with at-sign parent before dispatcher",
        "uvx '/tmp/@scope/bash' -c 'doc-lattice reconcile'",
    ),
    (
        "uv tool run path-only requirement with bracketed parent before dispatcher",
        "uv tool run '/tmp/[cache]/bash' -c 'doc-lattice reconcile'",
    ),
    (
        "uvx file URL requirement with at-sign parent before dispatcher",
        "uvx 'file:///tmp/@scope/bash' -c 'doc-lattice reconcile'",
    ),
    (
        "versioned nested uv requirement before dispatcher",
        "uvx uv@0.8.0 run bash -c 'doc-lattice reconcile'",
    ),
    (
        "uv tool run versioned nested uv requirement before dispatcher",
        "uv tool run uv@0.8.0 run bash -c 'doc-lattice reconcile'",
    ),
    (
        "versioned nested uvx requirement before dispatcher",
        "uvx uvx@0.8.0 bash -c 'doc-lattice reconcile'",
    ),
    (
        "versioned env requirement before dispatcher",
        "uvx env@1.0 bash -c 'doc-lattice reconcile'",
    ),
    ("builtin dot head", "builtin . ./doc-lattice-env.sh"),
    ("nohup wrapper before dispatcher", "nohup bash -c 'doc-lattice reconcile --all'"),
    ("setsid wrapper before dispatcher", "setsid sh -lc 'doc-lattice reconcile'"),
    ("xargs wrapper before dispatcher", "xargs -0 bash -c 'doc-lattice reconcile'"),
    ("sudo wrapper before dispatcher", "sudo -u deploy bash -c 'doc-lattice reconcile'"),
    ("unknown uv tool before dispatcher", "uvx sometool bash -c 'doc-lattice reconcile'"),
    ("unrecognized head with dispatcher argv", "echo bash -c 'doc-lattice reconcile'"),
    (
        "dynamic word after wrapper before dispatcher",
        "nohup \"$FLAG\" bash -c 'doc-lattice reconcile'",
    ),
    (
        "coproc unrecognized program before dispatcher",
        "coproc reader bash -c 'echo doc-lattice'",
    ),
    ("time keyword before plain eval head", "time eval 'doc-lattice reconcile'"),
    ("inline selection before eager stop option", "bash -c 'doc-lattice reconcile' --help"),
    ("path-qualified shell head inline command", "/bin/bash -c 'doc-lattice reconcile'"),
    ("noexec toggled back off before inline command", "bash -n +n -c 'doc-lattice reconcile'"),
    ("set option clears noexec before inline command", "bash -n +o noexec -c 'doc-lattice lint'"),
    ("plus cluster unsets noexec letter", "bash +nc 'doc-lattice reconcile'"),
    ("interactive flag beside noexec", "bash -i -n -c 'doc-lattice reconcile'"),
    ("impure cluster beside noexec", "bash -n -lc 'doc-lattice reconcile'"),
    ("exec set option can re-enable execution", "zsh -n -o exec -c 'doc-lattice reconcile'"),
    ("short dump mode is pushd-to-home in zsh", "bash -D -c 'doc-lattice reconcile'"),
    ("dynamic set option value beside noexec", "bash -n -o $X -c 'doc-lattice reconcile'"),
    # Selecting -c does not end invocation-option parsing, so the pure-noexec certification has
    # to survive the whole option region rather than only its prefix.
    ("noexec re-enabled after inline selection", "bash -n -c +n 'doc-lattice reconcile --all'"),
    ("set option re-enable after inline selection", "bash -n -c +o noexec 'doc-lattice lint'"),
    ("exec set option after inline selection", "zsh -n -c -o exec 'doc-lattice reconcile'"),
    ("impure option after inline selection", "bash -nc -x 'doc-lattice reconcile'"),
    ("dynamic option value after inline selection", "bash -n -c -o $X 'doc-lattice reconcile'"),
    (
        "local wheel shell requirement",
        "uvx ./bash-1.0.0-py3-none-any.whl -c 'doc-lattice reconcile'",
    ),
    (
        "bare wheel filename shell requirement",
        "uvx bash-1.0.0-py2.py3-none-any.whl -c 'doc-lattice reconcile'",
    ),
    (
        "uv tool run local wheel shell requirement",
        "uv tool run ./bash-1.0.0-py3-none-any.whl -c 'doc-lattice lint'",
    ),
    (
        "wheel build-tag shell requirement",
        "uvx ./bash-1.0.0-1-py3-none-any.whl -c 'doc-lattice reconcile'",
    ),
    ("sdist requirement with marker payload", "uvx ./bash-1.0.0.tar.gz -c 'doc-lattice reconcile'"),
    (
        "directory requirement with marker payload",
        "uvx ./tools/shellkit -c 'doc-lattice reconcile'",
    ),
    ("dot directory requirement with marker payload", "uvx . -c 'doc-lattice reconcile'"),
    (
        "marker-bearing sdist doc-lattice requirement",
        "uvx ./dist/doc_lattice-2.0.0.tar.gz reconcile",
    ),
    (
        "local uv wheel nested launcher",
        "uvx ./uv-0.8.0-py3-none-any.whl run bash -c 'doc-lattice reconcile'",
    ),
    ("path-qualified coproc after exec", "exec ./coproc bash -c 'doc-lattice reconcile'"),
    ("ambiguous word before external coproc", "exec $MAYBE coproc bash -c 'doc-lattice reconcile'"),
    ("external script file named for doc-lattice", "bash ./doc-lattice-runner.sh"),
    ("non-dispatcher head echoes marker text", "echo doc-lattice reconcile"),
    ("command wrapper external script file", "command bash ./doc-lattice-runner.sh"),
    ("env wrapper external script file", "env bash ./doc-lattice-runner.sh"),
    ("command query never executes marker", "command -v doc-lattice"),
    ("emulate mode then external script file", "zsh --emulate sh ./doc-lattice-runner.sh"),
    ("windows launcher external script file", "bash.exe ./doc-lattice-runner.sh"),
    ("uv run external script file", "uv run bash ./doc-lattice-runner.sh"),
    ("builtin non-dispatcher target", "builtin echo doc-lattice"),
    ("builtin shell target is not a builtin", "builtin bash -c 'doc-lattice reconcile'"),
    ("braced quoted option value stays resolvable", 'bash -o "${X}" ./doc-lattice-runner.sh'),
    ("rbash external script file", "rbash ./doc-lattice-runner.sh"),
    ("assignment marker without dispatcher head", "CMD='doc-lattice reconcile' echo done"),
    ("lone dash ends options before operand", "bash - -c 'doc-lattice reconcile'"),
    ("requirement-suffixed plain head is not a dispatcher", "bash@1.0 -c 'doc-lattice reconcile'"),
    ("uvx requirement external script file", "uvx bash@1.0 ./doc-lattice-runner.sh"),
    (
        "uvx direct requirement URL filename does not override declared name",
        "uvx 'not-bash @ file:///tmp/bash-1.0-py3-none-any.whl' -c 'doc-lattice reconcile'",
    ),
    ("wrapper before external script file", "nohup bash ./doc-lattice-runner.sh"),
    ("find dot operand is not a dispatcher", "find . -name 'doc-lattice*'"),
    ("wrapper argv marker without shell head", "xargs doc-lattice-formatter --all"),
    ("marker argument after script operand", "bash ./run.sh doc-lattice"),
    (
        "versioned env requirement never resolves its arguments",
        "uvx env@1.0 doc-lattice reconcile",
    ),
    (
        "versioned time requirement never resolves its arguments",
        "uv tool run time@2.0 doc-lattice reconcile",
    ),
    ("exec wrapper before plain eval head", "exec eval 'doc-lattice reconcile'"),
    ("env wrapper before plain source head", "env source ./doc-lattice-env.sh"),
    ("external time before plain eval head", "command time -p eval 'doc-lattice reconcile'"),
    ("uv run before plain eval head", "uv run eval 'doc-lattice reconcile'"),
    ("eager help stop before inline command", "bash --help -c 'doc-lattice reconcile'"),
    ("eager version stop before inline command", "zsh --version -c 'doc-lattice reconcile'"),
    ("path-qualified eval is a path execution", "./eval 'doc-lattice reconcile'"),
    ("path-qualified source is a path execution", "./source ./doc-lattice-env.sh"),
    ("path-qualified dot is a path execution", "./. ./doc-lattice-env.sh"),
    ("command wrapper before path-qualified eval", "command ./eval 'doc-lattice reconcile'"),
    ("uppercase plain head is not the builtin", "EVAL 'doc-lattice reconcile'"),
    ("suffixed plain head is not the builtin", "eval.exe 'doc-lattice reconcile'"),
    ("syntax check noexec before inline command", "bash -n -c 'doc-lattice reconcile'"),
    ("dash noexec before inline command", "dash -n -c 'doc-lattice reconcile'"),
    ("noexec cluster inline command", "sh -nc 'doc-lattice reconcile'"),
    ("reversed noexec cluster inline command", "bash -cn 'doc-lattice reconcile'"),
    ("set option noexec before inline command", "bash -o noexec -c 'doc-lattice reconcile'"),
    ("stacked noexec setters before inline command", "bash -o noexec -n -c 'doc-lattice lint'"),
    ("dump strings mode before inline command", "bash --dump-strings -c 'doc-lattice reconcile'"),
    ("dump po strings mode before inline command", "bash --dump-po-strings -c 'doc-lattice lint'"),
    ("noexec setter after inline selection", "bash -n -c -n 'doc-lattice reconcile'"),
    ("set option noexec after inline selection", "bash -n -c -o noexec 'doc-lattice lint'"),
    ("exec wrapper before builtin dispatcher target", "exec builtin eval 'doc-lattice reconcile'"),
    ("exec wrapper before coprocess dispatcher", "exec coproc eval 'doc-lattice reconcile'"),
    ("exec wrapper before coproc word", "exec coproc bash -c 'doc-lattice reconcile'"),
    ("env wrapper before coproc word", "env coproc bash -c 'doc-lattice reconcile'"),
    ("quoted coproc word after exec", "exec 'coproc' bash -c 'doc-lattice reconcile'"),
    (
        "local wheel non-shell requirement",
        "uvx ./innocent-1.0.0-py3-none-any.whl -c 'doc-lattice reconcile'",
    ),
    (
        "wheel requirement before external script operand",
        "uvx ./bash-1.0.0-py3-none-any.whl ./doc-lattice-runner.sh",
    ),
]


@pytest.mark.parametrize(
    ("_description", "script"),
    MARKER_REFUSE_CASES,
    ids=[case[0] for case in MARKER_REFUSE_CASES],
)
def test_marker_bearing_non_invocation_case_fails_closed(_description, script):
    assert_marker_refusal(script)


MARKER_CERTIFY_CASES = [
    (
        "marker only in trailing comment",
        "bash -c 'echo hello'  # doc-lattice check runs here",
        NONE,
    ),
    ("marker-free inline command", "bash -c 'echo hello world'", NONE),
    ("Unicode dotless i is not an ASCII marker", "bash -c 'doc-latt\u0131ce reconcile'", NONE),
    ("dispatcher head with no argv", "eval", NONE),
    ("directory requirement without marker", "uvx ./tools/shellkit -c 'echo hello'", NONE),
    ("resolved direct invocation", "doc-lattice linear", LINEAR),
    (
        "resolved wheel requirement invocation",
        "uvx ./dist/doc_lattice-2.0.0-py3-none-any.whl reconcile",
        RECONCILE,
    ),
    (
        "resolved nested wheel launcher invocation",
        "uvx ./uv-0.8.0-py3-none-any.whl run doc-lattice linear",
        LINEAR,
    ),
    ("resolved root help", "doc-lattice --help", NONE),
]


@pytest.mark.parametrize(
    ("_description", "script", "expected"),
    MARKER_CERTIFY_CASES,
    ids=[case[0] for case in MARKER_CERTIFY_CASES],
)
def test_resolved_or_marker_free_command_stays_certified(_description, script, expected):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == expected


def test_eval_taints_marker_split_across_cross_command_append_assignment():
    assert_taint_refusal("X=doc-\nX+='lattice reconcile'\neval \"$X\"")


@pytest.mark.parametrize(
    "script",
    [
        'unset X; eval "${X:-doc-}lattice reconcile"',
        'unset X; eval "${X:=doc-}lattice"; eval "$X"',
        'unset X; : "${X:=doc-}"; X+=lattice; eval "$X"',
        'eval "${X/pattern/doc-}lattice"',
        "eval doc-{lattice,noop}",
        "printf %s {doc-,lattice} | bash",
        "X=doc-; bash <<EOF\n${X}lattice reconcile\nEOF",
    ],
    ids=(
        "default-operand",
        "conditional-assignment-later-eval",
        "conditional-assignment-then-append",
        "replacement-operand",
        "brace-concatenation",
        "brace-argv-ports",
        "unquoted-heredoc-variable",
    ),
)
def test_parameter_and_brace_synthesis_reaches_execution_sinks(script: str):
    assert_taint_refusal(script)


def test_parameter_alternate_never_concatenates_variable_and_operand():
    result = scan_doc_lattice_invocations('X=doc-; eval "${X:+lattice}"')

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_parameter_operator_records_conditional_assignment_evidence():
    source = "${X:=doc-}"
    scanner = _ShellScanner(source, classify_commands=False)

    word, end = scanner._parse_word(0, len(source), 0)

    assert end == len(source)
    assert word.conditional_assignments[0].name == "X"
    assert word.conditional_assignments[0].content == LiteralTransfer("doc-")


@pytest.mark.parametrize(
    "script",
    [
        ': > "${X:=doc-}"; eval "$X"lattice',
        ': <<< "${X:=doc-}"; eval "$X"lattice',
        ': <<EOF\n${X:=doc-}\nEOF\neval "$X"lattice',
        '{ :; } > "${X:=doc-}"; eval "$X"lattice',
        '{ :; } <<EOF\n${X:=doc-}\nEOF\neval "$X"lattice',
    ],
    ids=(
        "redirection-word",
        "here-string",
        "heredoc",
        "brace-redirection",
        "brace-heredoc",
    ),
)
def test_redirection_parameter_assignment_reaches_owner_environment(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        '( :; ) > "${X:=doc-}"; eval "$X"lattice',
        '( :; ) <<EOF\n${X:=doc-}\nEOF\neval "$X"lattice',
    ],
    ids=("subshell-redirection", "subshell-heredoc"),
)
def test_redirection_parameter_assignment_stays_in_isolated_scope(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        'command cat <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
        '/bin/cat /dev/null > "${X:=doc-}"; eval "$X"lattice',
        'env cat <<EOF >/dev/null\n${X:=doc-}\nEOF\neval "$X"lattice',
        'env printf x <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
    ],
    ids=(
        "command-here-string",
        "path-redirection-word",
        "env-heredoc",
        "env-externalized-builtin",
    ),
)
def test_external_command_redirection_assignment_does_not_persist(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        'printf x <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
        '<<< "${X:=doc-}"; eval "$X"lattice',
        'printf x <<EOF >/dev/null\n${X:=doc-}\nEOF\neval "$X"lattice',
    ],
    ids=("builtin-here-string", "null-command", "builtin-heredoc"),
)
def test_builtin_redirection_assignment_persists(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'f() { :; }; f <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
        'function f { :; }; f <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
        'f () { :; }; f <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
        'cat() { :; }; cat <<< "${X:=doc-}" >/dev/null; eval "$X"lattice',
    ],
    ids=("compact", "function-keyword", "spaced", "external-shadow"),
)
def test_unknown_command_redirection_assignment_stays_fail_safe_for_shell_function(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        '{ :; } <<< "${X:=doc-}" | cat; eval "$X"lattice',
        'printf x | { :; } <<< "${X:=doc-}"; eval "$X"lattice',
        '{ :; } <<< "${X:=doc-}" & wait; eval "$X"lattice',
        'coproc { :; } <<< "${X:=doc-}"; wait; eval "$X"lattice',
        '{ :; } <<EOF | cat\n${X:=doc-}\nEOF\neval "$X"lattice',
    ],
    ids=("producer", "consumer", "background", "coprocess", "producer-heredoc"),
)
def test_isolated_compound_redirection_assignments_do_not_leak(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        '{ :; } <<< "${X:=doc-}"; eval "$X"lattice',
        ('shopt -s lastpipe; printf x | { :; } <<< "${X:=doc-}"; eval "$X"lattice'),
    ],
    ids=("foreground", "lastpipe-consumer"),
)
def test_persistent_compound_redirection_assignments_reach_later_eval(script: str):
    assert_taint_refusal(script)


def test_compound_redirection_assignment_rhs_uses_execution_environment():
    assert_taint_refusal('{ X=doc-; { :; } >"${Y:=$X}"; eval "$Y"lattice; } | cat')


@pytest.mark.parametrize(
    "script",
    [
        'X=safe; f() { local X=doc-; { :; } >"${Y:=$X}"; eval "$Y"lattice; }; f',
        'f() { local X=doc-; { :; } >"${Y:=$X}"; eval "$Y"lattice; }; f',
        ('X=safe; f() { local X=doc-; { { :; } >"${Y:=$X}"; eval "$Y"lattice; } | cat; }; f'),
        'f() { local Y; { :; } >"${Y:=doc-}"; eval "$Y"lattice; }; f',
        ('f() { local Y=safe; unset Y; { :; } >"${Y:=doc-}"; eval "$Y"lattice; }; f'),
        'f() { local Y; { :; } <<< "${Y:=doc-}"; eval "$Y"lattice; }; f',
        ('f() { local Y; { :; } <<EOF\n${Y:=doc-}\nEOF\neval "$Y"lattice; }; f'),
    ],
    ids=(
        "local-rhs-shadowed-global",
        "local-rhs-unset-global",
        "local-rhs-nested-pipeline",
        "local-destination",
        "local-destination-after-unset",
        "local-destination-here-string",
        "local-destination-heredoc",
    ),
)
def test_compound_redirection_assignments_respect_function_scope(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'inner() { X=doc-; }; outer() { local X=safe; inner; eval "$X"lattice; }; outer',
        ('inner() { export X=doc-; }; outer() { local X=safe; inner; eval "$X"lattice; }; outer'),
        ("inner() { unset X; }; outer() { local X=safe; inner; eval '${X:=doc-}lattice'; }; outer"),
    ],
    ids=("ordinary-assignment", "export-assignment", "unset"),
)
def test_called_function_mutates_callers_dynamic_local_scope(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        (
            "inner() { X=doc-; }; mid() { inner; }; "
            'outer() { local X=safe; mid; eval "$X"lattice; }; outer'
        ),
        (
            "inner() { export X=doc-; }; mid() { inner; }; "
            'outer() { local X=safe; mid; eval "$X"lattice; }; outer'
        ),
        (
            "inner() { unset X; }; mid() { inner; }; "
            "outer() { local X=safe; mid; eval '${X:=doc-}lattice'; }; outer"
        ),
        ("inner() { eval 'X=doc-'; }; outer() { local X=safe; inner; eval \"$X\"lattice; }; outer"),
        ('inner() { { :; } >"${X:=doc-}"; }; outer() { local X; inner; eval "$X"lattice; }; outer'),
        (
            'inner() { N=X; unset "$N"; }; '
            "outer() { local X=safe; inner; eval '${X:=doc-}lattice'; }; outer"
        ),
        (
            "inner() { eval 'X=doc-;'; }; "
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
        (
            "inner() { eval 'X=doc-; :'; }; "
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
    ],
    ids=(
        "transitive-ordinary-assignment",
        "transitive-export-assignment",
        "transitive-unset",
        "eval-assignment",
        "compound-assignment",
        "dynamic-unset",
        "eval-trailing-semicolon",
        "eval-multiple-commands",
    ),
)
def test_called_function_propagates_all_dynamic_scope_effects(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        ("inner() { eval 'X+=doc-'; }; outer() { local X=; inner; eval \"$X\"lattice; }; outer"),
        (
            "inner() { eval 'builtin unset X'; }; "
            "outer() { local X=safe; inner; eval '${X:=doc-}lattice'; }; outer"
        ),
        (
            "inner() { eval 'command unset X'; }; "
            "outer() { local X=safe; inner; eval '${X:=doc-}lattice'; }; outer"
        ),
        (
            "inner() { eval 'builtin export X=doc-'; }; "
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
    ],
    ids=("append", "builtin-unset", "command-unset", "builtin-export"),
)
def test_called_function_propagates_wrapped_eval_mutations(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'X=; f() { eval "$X"lattice; }; eval "X+=doc-"; f',
        ("X=safe; f() { eval '${X:=doc-}lattice'; }; eval 'builtin unset X'; f"),
        ("X=safe; f() { eval '${X:=doc-}lattice'; }; eval 'command unset X'; f"),
        ("X=safe; f() { eval \"$X\"lattice; }; eval 'builtin export X=doc-'; f"),
    ],
    ids=("append", "builtin-unset", "command-unset", "builtin-export"),
)
def test_function_eval_call_time_global_observes_wrapped_eval_mutations(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        ('inner() { X=doc- | cat; }; outer() { local X=safe; inner; eval "$X"lattice; }; outer'),
        ('inner() { X=doc- & wait; }; outer() { local X=safe; inner; eval "$X"lattice; }; outer'),
        (
            "inner() { coproc { X=doc-; }; wait; }; "
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
        (
            "inner() { false && unset X; }; "
            "outer() { local X=safe; inner; eval '${X:=doc-}lattice'; }; outer"
        ),
        (
            "inner() { if false; then X=doc-; fi; }; "
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
        (
            "inner() { unset X | cat; }; "
            "outer() { local X=safe; inner; eval '${X:=doc-}lattice'; }; outer"
        ),
    ],
    ids=(
        "pipeline-assignment",
        "background-assignment",
        "coprocess-assignment",
        "conditional-unset",
        "conditional-assignment",
        "pipeline-unset",
    ),
)
def test_called_function_effects_respect_control_and_execution_context(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        ('inner() { true && X=doc-; }; outer() { local X=safe; inner; eval "$X"lattice; }; outer'),
        ('inner() { false || X=doc-; }; outer() { local X=safe; inner; eval "$X"lattice; }; outer'),
        (
            "inner() { if true; then X=doc-; fi; }; "
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
    ],
    ids=("and", "or", "if"),
)
def test_called_function_effects_include_definitely_executed_branches(script: str):
    assert_taint_refusal(script)


def test_called_function_propagates_variable_backed_eval_mutation():
    assert_taint_refusal(
        'inner() { S=X=doc-; eval "$S"; }; '
        'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
    )


def test_function_eval_call_time_global_observes_variable_backed_eval_mutation():
    assert_taint_refusal('S=X=doc-; f() { eval "$X"lattice; }; eval "$S"; f')


@pytest.mark.parametrize(
    "script",
    [
        (
            'S=X=doc-; inner() { eval "$S"; }; '
            'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
        ),
        (
            'inner() { eval "$S"; }; '
            'outer() { local S=X=doc-; local X=safe; inner; eval "$X"lattice; }; outer'
        ),
        (
            'inner() { eval "$S"; }; '
            'outer() { local X=safe; S=X=doc- inner; eval "$X"lattice; }; outer'
        ),
    ],
    ids=("global", "caller-local", "call-prefix"),
)
def test_variable_backed_eval_inherits_function_call_time_values(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        (
            'inner() { eval "$S"; }; '
            'outer() { local X=safe; S=: inner; S=X=doc- inner; eval "$X"lattice; }; outer'
        ),
        (
            'inner() { eval "$S"; }; '
            'outer() { local X=safe; S=X=doc- inner; S=: inner; eval "$X"lattice; }; outer'
        ),
        (
            'inner() { eval "$S"; }; dead() { S=: inner; }; '
            'outer() { local X=safe; S=X=doc- inner; eval "$X"lattice; }; outer'
        ),
        (
            'inner() { false && S=: inner; eval "$S"; }; '
            'outer() { local X=safe; S=X=doc- inner; eval "$X"lattice; }; outer'
        ),
    ],
    ids=("safe-then-marker", "marker-then-safe", "dead-call", "recursive-dead-call"),
)
def test_variable_backed_eval_unions_exact_call_site_programs(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "setter",
    ["S=X=doc-", 'eval "S=X=doc-"'],
    ids=("ordinary-assignment", "static-eval-assignment"),
)
def test_variable_backed_eval_replays_preceding_callee_effects(setter: str):
    assert_taint_refusal(
        f"setprog() {{ {setter}; }}; "
        'inner() { setprog; eval "$S"; }; '
        'outer() { local S=: X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize(
    "setter",
    [
        "S+=doc-",
        'eval "S+=doc-"',
        "S=$P",
        'eval "S=$P"',
    ],
    ids=("ordinary-append", "static-eval-append", "ordinary-rhs", "static-eval-rhs"),
)
def test_variable_backed_eval_replays_callee_effects_as_state_transfers(setter: str):
    assert_taint_refusal(
        f"setprog() {{ {setter}; }}; "
        'inner() { setprog; eval "$S"; }; '
        'outer() { local P=X=doc- S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize(
    "setter",
    [
        "local P=doc-; S+=$P",
        "local P=doc-; eval 'S+=$P'",
        "P=doc- setvalue",
        "P=doc- setvalue_eval",
    ],
    ids=("local", "local-static-eval", "call-prefix", "call-prefix-static-eval"),
)
def test_variable_backed_eval_callee_transfers_use_callee_execution_state(setter: str):
    helpers = "setvalue() { S+=$P; }; setvalue_eval() { eval 'S+=$P'; }; "
    assert_taint_refusal(
        helpers + f"setprog() {{ {setter}; }}; "
        'inner() { setprog; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize("setter", ["S+=$P", "eval 'S+=$P'"])
def test_variable_backed_eval_callee_local_shadow_stays_clean(setter: str):
    result = scan_doc_lattice_invocations(
        f"setprog() {{ local P=safe; {setter}; }}; "
        'inner() { setprog; eval "$S"; }; '
        'outer() { local P=doc- S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "parameter",
    ["$1", "${1}", "$*", "$@"],
    ids=("positional-one", "braced-positional-one", "positional-star", "positional-at"),
)
def test_called_function_positional_parameters_reach_eval(parameter: str):
    assert_taint_refusal(f'f() {{ eval "{parameter}"lattice; }}; f doc-')


@pytest.mark.parametrize(
    "setter",
    [
        "S+=$1",
        "S=X=$1",
        "eval 'S+=$1'",
        "eval 'S=X=$1'",
    ],
    ids=("append", "replace", "static-eval-append", "static-eval-replace"),
)
def test_variable_backed_eval_replays_function_positional_argument(setter: str):
    assert_taint_refusal(
        f"setprog() {{ {setter}; }}; "
        'inner() { setprog doc-; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize(
    "script",
    [
        'f() { eval "$1"lattice; }; f safe',
        'f() { eval "$2"lattice; }; f doc- safe',
    ],
    ids=("safe-value", "correct-position"),
)
def test_called_function_positional_parameters_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_called_function_dynamic_positional_argument_fails_closed():
    result = scan_doc_lattice_invocations('f() { eval "$1"lattice; }; f "$EXTERNAL"')

    assert result.invocations == NONE
    assert result.incomplete_reason == "dynamic function positional argument"


def test_called_function_large_absent_positional_parameter_stays_bounded():
    parameter = "9" * 5000
    result = scan_doc_lattice_invocations(f'f() {{ eval "${{{parameter}}}"lattice; }}; f doc-')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "body",
    [
        'shift; eval "$1"lattice',
        'shift 2; eval "$1"lattice',
        'set -- safe doc-; shift; eval "$1"lattice',
        'set -- doc-; eval "$1"lattice',
    ],
    ids=("shift", "shift-two", "set-then-shift", "set"),
)
def test_called_function_positional_mutations_reach_eval(body: str):
    arguments = "safe safe doc-" if body.startswith("shift 2") else "safe doc-"
    assert_taint_refusal(f"f() {{ {body}; }}; f {arguments}")


@pytest.mark.parametrize(
    "script",
    [
        'f() { shift; eval "$1"lattice; }; f doc- safe',
        'f() { set -- safe; eval "$1"lattice; }; f doc-',
    ],
    ids=("shift-to-safe", "set-to-safe"),
)
def test_called_function_positional_mutations_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "setter",
    ["S+=$1", "eval 'S+=$1'"],
    ids=("ordinary", "static-eval"),
)
def test_function_effect_positional_mutation_uses_updated_argument(setter: str):
    assert_taint_refusal(
        f"setprog() {{ shift; {setter}; }}; "
        'inner() { setprog safe doc-; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )


def test_function_effect_positional_mutation_preserves_clean_control():
    result = scan_doc_lattice_invocations(
        "setprog() { shift; S+=$1; }; "
        'inner() { setprog doc- safe; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_function_effect_failed_shift_preserves_positional_arguments():
    assert_taint_refusal(
        "setprog() { shift 2; S+=$1; }; "
        'inner() { setprog doc-; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )


def test_function_effect_failed_shift_preserves_clean_control():
    result = scan_doc_lattice_invocations(
        "setprog() { shift 2; S+=$1; }; "
        'inner() { setprog safe; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_function_effect_unbounded_shift_fails_closed():
    amount = "9" * 5000
    result = scan_doc_lattice_invocations(
        f"setprog() {{ shift {amount}; S+=$1; }}; "
        'inner() { setprog safe; eval "$S"; }; '
        'outer() { local S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "set_command",
    ["set safe doc-", "set - safe doc-", "set +x safe doc-"],
    ids=("plain", "dash", "option-prefixed"),
)
def test_called_function_set_forms_replace_positional_arguments(set_command: str):
    assert_taint_refusal(f'f() {{ {set_command}; shift; eval "$1"lattice; }}; f')


@pytest.mark.parametrize(
    "set_command",
    ["set doc- safe", "set - doc- safe", "set +x doc- safe"],
    ids=("plain", "dash", "option-prefixed"),
)
def test_called_function_set_forms_preserve_clean_controls(set_command: str):
    result = scan_doc_lattice_invocations(f'f() {{ {set_command}; shift; eval "$1"lattice; }}; f')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_called_function_dynamic_set_form_fails_closed():
    result = scan_doc_lattice_invocations(
        'f() { set "$EXTERNAL" doc-; shift; eval "$1"lattice; }; f'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason == "dynamic function positional mutation"


@pytest.mark.parametrize(
    "script",
    [
        'f() { local IFS=-; eval "$*"; }; f doc lattice',
        'IFS=-; f() { eval "$*"; }; f doc lattice',
    ],
    ids=("local-ifs", "global-ifs"),
)
def test_called_function_quoted_star_uses_active_ifs(script: str):
    assert_taint_refusal(script)


def test_called_function_quoted_star_dynamic_ifs_fails_closed():
    result = scan_doc_lattice_invocations(
        'f() { local IFS="$EXTERNAL"; eval "$*"; }; f doc lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason == "dynamic function positional IFS"


@pytest.mark.parametrize(
    "script",
    [
        'f() { local IFS=:; eval "$*"; }; f doc lattice',
        "f() { local IFS=-; eval $*; }; f doc lattice",
        'f() { local IFS=-; eval "$@"; }; f doc lattice',
    ],
    ids=("safe-ifs", "unquoted-star", "positional-at"),
)
def test_called_function_star_ifs_preserves_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_called_function_unset_local_ifs_shadow_uses_default_joining():
    result = scan_doc_lattice_invocations('IFS=-; f() { local IFS; eval "$*"; }; f doc lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'printf -v X %s doc-; eval "$X"lattice',
        'read X <<< doc-; eval "$X"lattice',
        'P=doc-; declare -n R=P; eval "$R"lattice',
    ],
    ids=("printf-v", "read", "nameref"),
)
def test_deterministic_shell_writers_reach_later_eval(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'printf -v X %s safe; eval "$X"lattice',
        'read X <<< safe; eval "$X"lattice',
        'P=doc-; Q=safe; declare -n R=Q; eval "$R"lattice',
    ],
    ids=("printf-v", "read", "nameref"),
)
def test_deterministic_shell_writers_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'printf -v X %s%s doc- lattice; eval "$X"',
        'printf -v X doc-%s lattice; eval "$X"',
    ],
    ids=("two-substitutions", "literal-prefix"),
)
def test_printf_v_static_formats_reach_later_eval(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'printf -v X %s%s safe value; eval "$X"',
        'printf -v X safe-%s value; eval "$X"',
    ],
    ids=("two-substitutions", "literal-prefix"),
)
def test_printf_v_static_formats_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_printf_v_unsupported_format_fails_closed():
    result = scan_doc_lattice_invocations('printf -v X %q doc-lattice; eval "$X"')

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "script",
    [
        'printf -v X -- %s%s doc- lattice; eval "$X"',
        "printf -v X '%.4slattice' doc-X; eval \"$X\"",
    ],
    ids=("option-terminator", "precision"),
)
def test_printf_v_option_terminator_and_precision_reach_eval(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'printf -v X -- %s%s safe value; eval "$X"',
        "printf -v X '%.4svalue' safeX; eval \"$X\"",
    ],
    ids=("option-terminator", "precision"),
)
def test_printf_v_option_terminator_and_precision_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize("prefix", ["", "."], ids=("width", "precision"))
def test_printf_v_huge_numeric_fields_fail_closed(prefix: str):
    digits = "9" * 5_000
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4_300)
    try:
        result = scan_doc_lattice_invocations(
            f'printf -v X %{prefix}{digits}s doc-; eval "$X"lattice'
        )
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


def test_printf_v_bounded_width_preserves_clean_control():
    result = scan_doc_lattice_invocations('printf -v X %4s safe; eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'P="safe doc-"; P+=lattice; read A X <<<"$P"; eval "$X"',
        'P=doc-; P+=lattice; read <<<"$P"; eval "$REPLY"',
        'shopt -s lastpipe; printf %s%s doc- lattice | read X; eval "$X"',
        'P=doc-; P+=lattice; printf %s "$P" > payload; read X < payload; eval "$X"',
        'read X < <(printf %s%s doc- lattice); eval "$X"',
        'P=doc-; read X <<EOF\n${P}lattice\nEOF\neval "$X"',
        'read A X B <<<"safe doc- tail"; eval "$X"lattice',
        'IFS=: read A X B <<<"safe:doc-:tail"; eval "$X"lattice',
    ],
    ids=(
        "later-target",
        "reply",
        "lastpipe",
        "static-resource",
        "process-substitution",
        "heredoc",
        "middle-target",
        "custom-ifs-middle-target",
    ),
)
def test_read_writes_from_finalized_stdin_reach_later_eval(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'read A X <<<"safe value"; eval "$X"',
        'read <<<safe; eval "$REPLY"',
        'P=safe; printf %s "$P" > payload; read X < payload; eval "$X"lattice',
        'read X < <(printf %s safe); eval "$X"lattice',
        'P=safe; read X <<EOF\n${P}\nEOF\neval "$X"lattice',
        'read A X B <<<"safe value tail"; eval "$X"lattice',
        'IFS=: read A X B <<<"safe:value:tail"; eval "$X"lattice',
    ],
    ids=(
        "later-target",
        "reply",
        "static-resource",
        "process-substitution",
        "heredoc",
        "middle-target",
        "custom-ifs-middle-target",
    ),
)
def test_read_writes_from_finalized_stdin_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_dynamic_target_fails_closed():
    result = scan_doc_lattice_invocations('TARGET=X; read "$TARGET" <<<doc-; eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "script",
    [
        'P="safe:doc_"; P+=lattice; IFS=:_ read A X <<<"$P"; eval "$X"',
        'P=doc-; P+=lattice; IFS=:- read X <<<"$P"; eval "$X"',
        'read X <<EOF\ndoc-\nignored\nEOF\neval "$X"lattice',
    ],
    ids=("last-remainder", "only-target", "first-record"),
)
def test_read_exact_record_and_delimiters_reach_later_eval(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'P=doc_; P+=lattice:safe; IFS=:_ read A X <<<"$P"; eval "$X"',
        'read X <<EOF\nsafe\ndoc-\nEOF\neval "$X"lattice',
    ],
    ids=("split-marker", "later-record"),
)
def test_read_exact_record_and_delimiters_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'read -N 4 X <<<doc-X; eval "$X"lattice',
        'read -n 4 X <<<doc-X; eval "$X"lattice',
        'read -d X V <<<doc-Xlattice; eval "$V"lattice',
    ],
    ids=("exact-count", "maximum-count", "delimiter"),
)
def test_read_unmodeled_record_options_fail_closed(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "script",
    [
        'read -u 3 X 3<<<doc-; eval "$X"lattice',
        'X=doc-; read -u 3 X 3<<<safe; eval "$X"lattice',
    ],
    ids=("marker-input", "clean-input"),
)
def test_read_unmodeled_descriptor_selection_fails_closed(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


def test_read_default_backslash_continuation_reaches_eval():
    assert_taint_refusal("read X <<'EOF'\ndoc-\\\nlattice\nEOF\neval \"$X\"")


def test_read_default_backslash_continuation_preserves_clean_control():
    result = scan_doc_lattice_invocations("read X <<'EOF'\nsafe\\\nvalue\nEOF\neval \"$X\"lattice")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_nonliteral_stream_projects_first_record():
    assert_taint_refusal(
        "shopt -s lastpipe; printf 'doc-\\nignored\\n' | read X; eval \"$X\"lattice"
    )


def test_read_nonliteral_stream_preserves_later_record_clean_control():
    result = scan_doc_lattice_invocations(
        "shopt -s lastpipe; printf 'safe\\ndoc-\\n' | read X; eval \"$X\"lattice"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_mixed_ifs_projects_exact_middle_field():
    assert_taint_refusal(
        'P="safe :doc-"; P+="lattice :tail"; IFS=" :" read A X B <<<"$P"; eval "$X"'
    )


def test_read_mixed_ifs_preserves_clean_field_selection_control():
    result = scan_doc_lattice_invocations(
        'P=doc_; P+=lattice:safe; IFS=" :_" read A X B <<<"$P"; eval "$X"'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_nonliteral_stream_projects_all_scalar_targets():
    assert_taint_refusal(
        "shopt -s lastpipe; printf 'safe doc-\\nignored\\n' | read A X; eval \"$X\"lattice"
    )


def test_read_nonliteral_stream_preserves_exact_target_selection_control():
    result = scan_doc_lattice_invocations(
        "shopt -s lastpipe; printf '%s%s\\n' doc- 'lattice safe' | read A X; eval \"$X\"lattice"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_nonliteral_stream_applies_cooked_line_continuation():
    assert_taint_refusal(
        'shopt -s lastpipe; { printf "%s\\n" "doc-\\\\"; '
        'printf "%s\\n" lattice; } | read X; eval "$X"'
    )


def test_read_nonliteral_stream_cooked_continuation_preserves_clean_control():
    result = scan_doc_lattice_invocations(
        'shopt -s lastpipe; { printf "%s\\n" "doc-\\\\"; '
        'printf "%s\\n" safe; } | read X; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_deferred_projection_over_literal_cap_fails_closed():
    prefix = "x" * 4_097
    result = scan_doc_lattice_invocations(
        f'shopt -s lastpipe; P={prefix}; printf "%s %s%s\\n" "$P" doc- lattice | '
        'read A X; eval "$X"'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


def test_read_deferred_projection_at_literal_cap_reaches_eval():
    prefix = "x" * 4_000
    assert_taint_refusal(
        f'shopt -s lastpipe; P={prefix}; printf "%s %s%s\\n" "$P" doc- lattice | '
        'read A X; eval "$X"'
    )


def test_read_nonliteral_stream_cooked_escape_reaches_eval():
    assert_taint_refusal("shopt -s lastpipe; printf '%s\\n' 'doc\\-lattice' | read X; eval \"$X\"")


def test_read_nonliteral_stream_raw_escape_preserves_clean_control():
    result = scan_doc_lattice_invocations(
        "shopt -s lastpipe; printf '%s\\n' 'doc\\-lattice' | read -r X; eval \"$X\""
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_read_nonliteral_stream_cooked_escape_preserves_ifs_control():
    result = scan_doc_lattice_invocations(
        "shopt -s lastpipe; printf '%s\\n' 'safe:doc-\\:lattice' | "
        'IFS=: read A X; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'P=safe:doc-; P+=lattice; IFS="$EXTERNAL" read A X <<<"$P"; eval "$X"',
        'P=doc-; P+=lattice:safe; IFS="$EXTERNAL" read A X <<<"$P"; eval "$X"',
        (
            'shopt -s lastpipe; IFS="$EXTERNAL"; '
            "printf '%s\\n' 'safe:doc-lattice' | read A X; eval \"$X\""
        ),
    ],
    ids=("marker-field", "clean-field", "deferred-pipeline"),
)
def test_read_dynamic_ifs_fails_closed(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


def test_read_differing_call_site_literals_reach_eval():
    assert_taint_refusal(
        'f(){ shopt -s lastpipe; printf "%s %s%s\\n" "$1" doc- lattice | '
        'read A X; eval "$X"; }; f x; f y'
    )


@pytest.mark.parametrize(
    "calls",
    ["f x", "f x; f x"],
    ids=("single", "identical-repeated"),
)
def test_read_same_call_site_literals_reach_eval_control(calls: str):
    assert_taint_refusal(
        'f(){ shopt -s lastpipe; printf "%s %s%s\\n" "$1" doc- lattice | '
        f'read A X; eval "$X"; }}; {calls}'
    )


@pytest.mark.parametrize(
    "calls",
    ["f x", "f x; f y"],
    ids=("single", "differing"),
)
def test_read_differing_call_site_literals_preserve_clean_inverse(calls: str):
    result = scan_doc_lattice_invocations(
        'f(){ shopt -s lastpipe; printf "%s %s%s\\n" "$1" safe value | '
        f'read A X; eval "$X"; }}; {calls}'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize("read_option", ["", "-r "], ids=("cooked", "raw"))
def test_read_opaque_prefix_preserves_authored_marker_suffix(read_option: str):
    assert_taint_refusal(
        'shopt -s lastpipe; { cat "$EXTERNAL"; printf " doc-%s\\n" lattice; } | '
        f'read {read_option}A X; eval "$X"'
    )


@pytest.mark.parametrize("read_option", ["", "-r "], ids=("cooked", "raw"))
def test_read_opaque_prefix_preserves_clean_suffix_control(read_option: str):
    result = scan_doc_lattice_invocations(
        'shopt -s lastpipe; { cat "$EXTERNAL"; printf " safe%s\\n" value; } | '
        f'read {read_option}A X; eval "$X"'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize("writer", ["R=doc-", "read R <<<doc-"], ids=("assignment", "read"))
def test_nameref_writes_update_referent(writer: str):
    assert_taint_refusal(f'X=safe; declare -n R=X; {writer}; eval "$X"lattice')


def test_nameref_write_preserves_clean_control():
    result = scan_doc_lattice_invocations('X=safe; declare -n R=X; R=safe; eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_static_eval_nameref_declaration_routes_later_write():
    assert_taint_refusal("X=safe; eval 'declare -n R=X; R=doc-'; eval \"$X\"lattice")


def test_static_eval_nameref_declaration_preserves_clean_inverse():
    result = scan_doc_lattice_invocations(
        "X=doc-; eval 'declare -n R=X; R=safe'; eval \"$X\"lattice"
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_nameref_chain_write_updates_final_referent():
    assert_taint_refusal('X=safe; declare -n R=X; declare -n S=R; S=doc-; eval "$X"lattice')


@pytest.mark.parametrize(
    "declaration",
    ['TARGET=X; declare -n R="$TARGET"', "declare -n R=S; declare -n S=R"],
    ids=("dynamic", "cycle"),
)
def test_unsupported_nameref_target_fails_closed(declaration: str):
    result = scan_doc_lattice_invocations(f'X=safe; {declaration}; R=doc-; eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "script",
    [
        'f(){ R=doc-;}; X=safe; declare -n R=X; f; eval "$X"lattice',
        'f(){ R=doc-;}; outer(){ local X=safe; local -n R=X; f; eval "$X"lattice;}; outer',
        'X=safe; f(){ declare -gn R=X;}; f; R=doc-; eval "$X"lattice',
    ],
    ids=("global-after-definition", "caller-local", "declare-global"),
)
def test_runtime_nameref_writes_update_referent(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'f(){ R=safe;}; X=doc-; declare -n R=X; f; eval "$X"lattice',
        'f(){ R=safe;}; outer(){ local X=doc-; local -n R=X; f; eval "$X"lattice;}; outer',
        'X=doc-; f(){ declare -gn R=X;}; f; R=safe; eval "$X"lattice',
    ],
    ids=("global-after-definition", "caller-local", "declare-global"),
)
def test_runtime_nameref_writes_preserve_clean_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_unbound_nameref_can_bind_then_write_referent():
    assert_taint_refusal('X=safe; declare -n R; R=X; R=doc-; eval "$X"lattice')


def test_unbound_nameref_binding_preserves_clean_control():
    result = scan_doc_lattice_invocations('X=doc-; declare -n R; R=X; R=safe; eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_nameref_rebinding_in_subprocess_does_not_leak_to_parent():
    assert_taint_refusal(
        'X=safe; Y=safe; declare -n R=X; (declare -n R=Y); R=doc-; eval "$X"lattice'
    )


def test_nameref_rebinding_in_subprocess_preserves_parent_clean_control():
    result = scan_doc_lattice_invocations(
        'X=doc-; Y=safe; declare -n R=X; (declare -n R=Y); R=safe; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_nameref_write_in_subprocess_does_not_refresh_parent_eval_state():
    assert_taint_refusal('X=doc-; declare -n R=X; (R=safe); eval "$X"lattice')


def test_nameref_write_in_subprocess_preserves_parent_eval_clean_control():
    result = scan_doc_lattice_invocations('X=safe; declare -n R=X; (R=doc-); eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_unset_n_nameref_stops_routing_later_writes():
    assert_taint_refusal('X=doc-; declare -n R=X; unset -n R; R=safe; eval "$X"lattice')


def test_unset_n_nameref_preserves_referent_clean_control():
    result = scan_doc_lattice_invocations(
        'X=safe; declare -n R=X; unset -n R; R=doc-; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_unset_n_nameref_function_effect_stops_routing_later_writes():
    assert_taint_refusal('f(){ unset -n R; R=safe;}; X=doc-; declare -n R=X; f; eval "$X"lattice')


def test_unset_n_nameref_function_effect_preserves_clean_control():
    result = scan_doc_lattice_invocations(
        'f(){ unset -n R; R=doc-;}; X=safe; declare -n R=X; f; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_nested_unset_n_function_effect_preserves_call_site_order():
    assert_taint_refusal(
        'g(){ unset -n R;}; f(){ R=doc-; g; R=safe;}; X=safe; declare -n R=X; f; eval "$X"lattice'
    )


def test_nested_unset_n_function_effect_preserves_clean_control():
    result = scan_doc_lattice_invocations(
        'g(){ unset -n R;}; f(){ R=safe; g; R=doc-;}; X=doc-; declare -n R=X; f; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_deep_function_effect_expansion_fails_with_stable_bound():
    depth = 80
    definitions = [f"f{index}(){{ f{index - 1};}}" for index in range(depth, 0, -1)]
    definitions.append("f0(){ X=doc-;}")
    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(60)
    try:
        result = scan_doc_lattice_invocations(
            f'{"; ".join(definitions)}; X=safe; f{depth}; eval "$X"lattice'
        )
    finally:
        sys.setrecursionlimit(previous_limit)

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint function effect depth limit exceeded"


def test_bounded_function_effect_expansion_preserves_marker_control():
    depth = 64
    definitions = ["f0(){ X=doc-;}"]
    definitions.extend(f"f{index}(){{ f{index - 1};}}" for index in range(1, depth + 1))

    assert_taint_refusal(f'{"; ".join(definitions)}; X=safe; f{depth}; eval "$X"lattice')


@pytest.mark.parametrize(
    "setter",
    ["S+=$Q", "eval 'S+=$Q'"],
    ids=("ordinary", "static-eval"),
)
def test_variable_backed_eval_callee_local_derivation_preserves_caller_state(
    setter: str,
):
    assert_taint_refusal(
        f"setprog() {{ local Q; Q=$P; {setter}; }}; "
        'inner() { setprog; eval "$S"; }; '
        'outer() { local P=doc- S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize(
    "prefix",
    ["local P=safe Q; Q=$P", "local Q; Q=$P; Q=safe"],
    ids=("safe-local-source", "safe-overwrite"),
)
def test_variable_backed_eval_callee_local_derivation_preserves_clean_controls(
    prefix: str,
):
    result = scan_doc_lattice_invocations(
        f"setprog() {{ {prefix}; S+=$Q; }}; "
        'inner() { setprog; eval "$S"; }; '
        'outer() { local P=doc- S=X= X=safe; inner; eval "$X"lattice; }; outer'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize("builtin", ["declare", "typeset"])
def test_called_eval_declaration_stays_local_to_callee(builtin: str):
    result = scan_doc_lattice_invocations(
        f"inner() {{ eval '{builtin} X=doc-'; }}; "
        'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_called_eval_declare_global_reaches_global_scope():
    assert_taint_refusal("inner() { eval 'declare -g X=doc-'; }; inner; eval \"$X\"lattice")


@pytest.mark.parametrize(
    "script",
    [
        'f() { eval "$X"lattice; }; X=doc- f; f() { :; }',
        'f-x() { eval "$X"lattice; }; X=doc- f-x',
        'function f-x { eval "$X"lattice; }; X=doc- f-x',
        'f() { eval "$X"lattice; }; X=doc- eval f',
        'f.x() { eval "$X"lattice; }; X=doc- f.x',
        'function f.x { eval "$X"lattice; }; X=doc- f.x',
        '1f() { eval "$X"lattice; }; X=doc- 1f',
        'f() { eval "$X"lattice; }; X=doc- eval " f "',
        'f() { eval "$X"lattice; }; X=doc- eval "f;"',
        "f() { eval \"$X\"lattice; }; X=doc- eval f ''",
        "f() { eval \"$X\"lattice; }; X=doc- eval $'f\\n'",
        '.f() { eval "$X"lattice; }; X=doc- .f',
        ':f() { eval "$X"lattice; }; X=doc- :f',
        'f+g() { eval "$X"lattice; }; X=doc- f+g',
        'function f@g { eval "$X"lattice; }; X=doc- f@g',
        'f() { eval "$X"lattice; }; X=doc- eval "f arg"',
        'f() { eval "$X"lattice; }; X=doc- eval "true; f"',
        'f() { eval "$X"lattice; }; X=doc- eval "f </dev/null"',
        'f() { eval "$X"lattice; }; X=doc- eval "f && :"',
        'f,g() { eval "$X"lattice; }; X=doc- f,g',
        'function f%g { eval "$X"lattice; }; X=doc- f%g',
        'f() { eval "$X"lattice; }; X=doc- eval "time f"',
        'f() { eval "$X"lattice; }; X=doc- eval "if true; then f; fi"',
        'f^g() { eval "$X"lattice; }; X=doc- f^g',
        'f~g() { eval "$X"lattice; }; X=doc- f~g',
        'function f#x { eval "$X"lattice; }; X=doc- f#x',
        'f() { eval "$X"lattice; }; X=doc- eval "coproc f"',
        'f() { eval "$X"lattice; }; X=doc- eval "! time f"',
        'f() { eval "$X"lattice; }; X=doc- eval "! coproc f"',
    ],
    ids=(
        "redefinition",
        "hyphenated-compact",
        "hyphenated-function",
        "eval-call",
        "dotted-compact",
        "dotted-function",
        "digit-leading",
        "eval-whitespace",
        "eval-semicolon",
        "eval-empty-argument",
        "eval-newline",
        "dot-leading",
        "colon-leading",
        "plus",
        "at-sign",
        "eval-argument",
        "eval-sequence",
        "eval-redirection",
        "eval-and-list",
        "comma",
        "percent",
        "eval-time",
        "eval-if",
        "caret",
        "tilde",
        "hash",
        "eval-coproc",
        "eval-negated-time",
        "eval-negated-coproc",
    ),
)
def test_function_call_prefix_mapping_follows_bash_execution(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize("wrapper", ["builtin", "command"])
def test_eval_builtin_wrappers_do_not_call_user_functions(wrapper: str):
    result = scan_doc_lattice_invocations(f'f() {{ eval "$X"lattice; }}; X=doc- eval "{wrapper} f"')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    ("name", "program"),
    [
        ("time", '"time"'),
        ("time", r"\time"),
        ("coproc", '"coproc"'),
        ("coproc", r"\coproc"),
    ],
    ids=("quoted-time", "escaped-time", "quoted-coproc", "escaped-coproc"),
)
def test_eval_quoted_reserved_words_call_same_named_functions(name: str, program: str):
    assert_taint_refusal(f"function {name} {{ eval \"$X\"lattice; }}; X=doc- eval '{program}'")


def test_eval_coproc_option_terminator_does_not_call_following_function():
    result = scan_doc_lattice_invocations('f() { eval "$X"lattice; }; X=doc- eval "coproc -- f"')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize("redirection", ["2>/dev/null", "{fd}>/dev/null"])
def test_eval_fd_prefixed_redirection_preserves_following_function_call(redirection: str):
    assert_taint_refusal(f'f() {{ eval "$X"lattice; }}; X=doc- eval "{redirection} f"')


@pytest.mark.parametrize("redirection", ["2>/dev/null", "{fd}>/dev/null"])
def test_eval_fd_prefixed_redirection_preserves_following_mutation(redirection: str):
    assert_taint_refusal(
        f'inner() {{ eval "{redirection} X=doc-"; }}; '
        'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize(
    "redirection",
    [
        "2>&1",
        "2>&-",
        "2>|/dev/null",
        "{fd}>&1",
        "{fd}>&-",
        "{fd}>|/dev/null",
    ],
    ids=(
        "numeric-duplicate",
        "numeric-close",
        "numeric-clobber",
        "dynamic-duplicate",
        "dynamic-close",
        "dynamic-clobber",
    ),
)
def test_eval_fd_operator_variants_preserve_following_function_call(redirection: str):
    assert_taint_refusal(f'f() {{ eval "$X"lattice; }}; X=doc- eval "{redirection} f"')


@pytest.mark.parametrize(
    "redirection",
    [
        "2>&1",
        "2>&-",
        "2>|/dev/null",
        "{fd}>&1",
        "{fd}>&-",
        "{fd}>|/dev/null",
    ],
    ids=(
        "numeric-duplicate",
        "numeric-close",
        "numeric-clobber",
        "dynamic-duplicate",
        "dynamic-close",
        "dynamic-clobber",
    ),
)
def test_eval_fd_operator_variants_preserve_following_mutation(redirection: str):
    assert_taint_refusal(
        f'inner() {{ eval "{redirection} X=doc-"; }}; '
        'outer() { local X=safe; inner; eval "$X"lattice; }; outer'
    )


@pytest.mark.parametrize(
    ("name", "eval_command"),
    [
        ("time", "eval \"$'time'\""),
        ("coproc", "eval \"$'coproc'\""),
        ("time", """eval '$"time"'"""),
        ("coproc", """eval '$"coproc"'"""),
    ],
    ids=("ansi-time", "ansi-coproc", "locale-time", "locale-coproc"),
)
def test_eval_dollar_quoted_reserved_words_call_same_named_functions(
    name: str,
    eval_command: str,
):
    assert_taint_refusal(f'function {name} {{ eval "$X"lattice; }}; X=doc- {eval_command}')


@pytest.mark.parametrize("name", ["time", "coproc"])
def test_eval_unquoted_reserved_words_do_not_call_same_named_functions(name: str):
    result = scan_doc_lattice_invocations(
        f'function {name} {{ eval "$X"lattice; }}; X=doc- eval {name}'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    ("name", "encoded"),
    [
        ("time", r"\x74ime"),
        ("time", r"\164ime"),
        ("time", r"\u0074ime"),
        ("coproc", r"copr\x6fc"),
        ("coproc", r"copr\157c"),
        ("coproc", r"copr\u006fc"),
    ],
    ids=("hex-time", "octal-time", "unicode-time", "hex-coproc", "octal-coproc", "unicode-coproc"),
)
def test_eval_ansi_c_escaped_reserved_words_call_same_named_functions(
    name: str,
    encoded: str,
):
    assert_taint_refusal(
        f"""function {name} {{ eval "$X"lattice; }}; X=doc- eval "$'{encoded}'\""""
    )


@pytest.mark.parametrize("name", ["time", "coproc"])
def test_eval_ansi_c_line_continuation_calls_same_named_function(name: str):
    encoded = f"{name[:2]}\\\n{name[2:]}"
    assert_taint_refusal(
        f"""function {name} {{ eval "$X"lattice; }}; X=doc- eval "$'{encoded}'\""""
    )


def test_eval_ansi_c_out_of_range_unicode_fails_closed():
    result = scan_doc_lattice_invocations(
        """function time { eval "$X"lattice; }; X=doc- eval "$'\\U00110000time'\""""
    )

    assert result.invocations == NONE
    assert result.incomplete_reason == "eval ANSI-C escape cannot be represented"


@pytest.mark.parametrize(
    "script",
    [
        'f() { eval "$X"lattice; }; false && unset -f f; X=doc- f',
        'f() { eval "$X"lattice; }; if false; then unset -f f; fi; X=doc- f',
        'f() { eval "$X"lattice; }; { unset -f f; } & wait; X=doc- f',
        'f() { eval "$X"lattice; }; false && f() { :; }; X=doc- f',
        'f() { eval "$X"lattice; }; (f() { :; }); X=doc- f',
    ],
    ids=(
        "conditional-and-unset",
        "conditional-if-unset",
        "background-unset",
        "conditional-redefinition",
        "subshell-redefinition",
    ),
)
def test_function_lifetime_respects_control_and_execution_context(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'true && f() { eval "$X"lattice; }; X=doc- f',
        'false || f() { eval "$X"lattice; }; X=doc- f',
        'if true; then f() { eval "$X"lattice; }; fi; X=doc- f',
    ],
    ids=("and-definition", "or-definition", "if-definition"),
)
def test_function_lifetime_includes_definitely_executed_definitions(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'f() { eval "$X"lattice; }; true && unset -f f; X=doc- f',
        'f() { eval "$X"lattice; }; false || unset -f f; X=doc- f',
        'f() { eval "$X"lattice; }; if true; then unset -f f; fi; X=doc- f',
    ],
    ids=("and-unset", "or-unset", "if-unset"),
)
def test_function_lifetime_applies_definitely_executed_unsets(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        "X=safe; f() { eval '${X:=doc-}lattice'; }; unset X; f",
        "X=safe; f() { eval '${X:=doc-}lattice'; }; X=doc-; f",
        'X=safe; f() { eval "$X"lattice; }; eval "X=doc-"; f',
    ],
    ids=("unset-before-call", "assignment-before-call", "eval-assignment-before-call"),
)
def test_function_eval_uses_call_time_global_state(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "if false; then X=safe; fi; f() { eval '${X:=doc-}lattice'; }; f",
        "false && X=safe; f() { eval '${X:=doc-}lattice'; }; f",
        "true || X=safe; f() { eval '${X:=doc-}lattice'; }; f",
        "case no in yes) X=safe;; esac; f() { eval '${X:=doc-}lattice'; }; f",
        "X=safe | cat; f() { eval '${X:=doc-}lattice'; }; f",
        "{ X=safe; } & wait; f() { eval '${X:=doc-}lattice'; }; f",
        "coproc { X=safe; }; wait; f() { eval '${X:=doc-}lattice'; }; f",
    ],
    ids=(
        "untaken-if",
        "untaken-and",
        "untaken-or",
        "untaken-case",
        "pipeline-producer",
        "background-group",
        "coprocess",
    ),
)
def test_function_eval_call_time_global_ignores_nonpersistent_writes(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        ("X=safe; clear() { unset X; }; clear; f() { eval '${X:=doc-}lattice'; }; f"),
        ('X=safe; setdoc() { X=doc-; }; setdoc; f() { eval "$X"lattice; }; f'),
        ("X=safe; setdoc() { eval 'X=doc-'; }; setdoc; f() { eval \"$X\"lattice; }; f"),
    ],
    ids=("unset", "ordinary-assignment", "eval-assignment"),
)
def test_function_eval_call_time_global_observes_called_function_effects(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'X=safe; f() { eval "$X"lattice; }; eval "X=doc-;"; f',
        'f() { eval "$X"lattice; }; unset X; : "${X:=doc-}"; f',
        'f() { eval "$X"lattice; }; unset X; { :; } >"${X:=doc-}"; f',
    ],
    ids=("eval-trailing-semicolon", "parameter-assignment", "compound-redirection"),
)
def test_function_eval_call_time_global_observes_common_mutations(script: str):
    assert_taint_refusal(script)


def test_function_eval_call_time_safe_global_prevents_default():
    result = scan_doc_lattice_invocations("f() { eval '${X:=doc-}lattice'; }; X=safe; f")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_unset_function_definition_is_not_used_for_later_call_mapping():
    result = scan_doc_lattice_invocations('f() { eval "$X"lattice; }; unset -f f; X=doc- f')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        "f() { eval '${X:=doc-}lattice'; }; X=safe f",
        ("inner() { eval '${X:=doc-}lattice'; }; outer() { local X=safe; inner; }; outer"),
    ],
    ids=("call-prefix", "dynamic-caller-local"),
)
def test_function_entry_setness_prevents_untaken_eval_defaults(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'eval "$X"lattice; { :; } >"${X:=doc-}"',
        'X=safe; { :; } >"${X:=doc-}"; eval "$X"lattice',
    ],
    ids=("future-binding", "untaken-default"),
)
def test_compound_conditional_assignments_are_replayed_in_source_order(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        "S='${X:-doc-}'; eval \"$S\"lattice",
        "S='${X:=doc-}'; eval \"$S\"lattice",
        "X=present; S='${X:+doc-}'; eval \"$S\"lattice",
        "S='${X/pattern/doc-}'; eval \"$S\"lattice",
        "Y=doc-; S='${X:-$Y}'; eval \"$S\"lattice",
        "S='${X:-${Y:-doc-}}'; eval \"$S\"lattice",
    ],
    ids=(
        "default",
        "assign-default",
        "alternate",
        "replacement",
        "variable-default",
        "nested-default",
    ),
)
def test_eval_second_pass_preserves_complex_parameter_operands(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'S=\'${X:=doc-}\'; eval "$S"; eval "$X"lattice',
        "S=': ${X:=doc-}; ${X}lattice'; eval \"$S\"",
        'A=\'${X:=doc-}\'; S=$A; eval "$S"; eval "$X"lattice',
        'S=\'${X:=${Y:=doc-}}\'; eval "$S"; eval "$Y"lattice',
        '( S=\'${X:=doc-}\'; eval "$S"; eval "$X"lattice )',
        '{ S=\'${X:=doc-}\'; eval "$S"; }; eval "$X"lattice',
        "if true; then eval ': ${X:=doc-}'; fi; eval \"$X\"lattice",
        "f() { eval ': ${X:=doc-}'; }; f; eval \"$X\"lattice",
        "case x in x) eval ': ${X:=doc-}';; esac; eval \"$X\"lattice",
        ("select x in one; do eval ': ${X:=doc-}'; break; done <<< 1; eval \"$X\"lattice"),
        'shopt -s lastpipe; S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice',
        ('builtin shopt -s lastpipe; S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'),
        (
            "builtin -- shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "command builtin shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf x | eval "$S"; eval "$X"lattice'
        ),
        ('command shopt -s lastpipe; S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'),
        (
            "shopt -s lastpipe; shopt -su lastpipe; S=': ${X:=doc-}'; "
            'printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "shopt -s lastpipe; { shopt -u lastpipe; } | cat; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "shopt -s lastpipe; printf x | { shopt -u lastpipe; cat; } | cat; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        "{ eval ': ${X:=doc-}'; eval \"$X\"lattice; } | cat",
        "printf x | { eval ': ${X:=doc-}'; eval \"$X\"lattice; }",
        "{ eval ': ${X:=doc-}'; eval \"$X\"lattice; } & wait",
        "coproc { eval ': ${X:=doc-}'; eval \"$X\"lattice; }; wait",
        (
            "printf x | { shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf y | eval "$S"; eval "$X"lattice; }'
        ),
        (
            "{ shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf y | eval "$S"; eval "$X"lattice; } | cat'
        ),
        (
            "{ shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf y | eval "$S"; eval "$X"lattice; } & wait'
        ),
        (
            "coproc { shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf y | eval "$S"; eval "$X"lattice; }; wait'
        ),
        "if true; then eval ': ${X:=doc-}'; ( eval \"$X\"lattice ); fi | cat",
        ("printf x | if true; then eval ': ${X:=doc-}'; ( eval \"$X\"lattice ); fi"),
        "for x in one; do eval ': ${X:=doc-}'; ( eval \"$X\"lattice ); done | cat",
        ("while true; do eval ': ${X:=doc-}'; ( eval \"$X\"lattice ); break; done | cat"),
        "case x in x) eval ': ${X:=doc-}'; ( eval \"$X\"lattice );; esac | cat",
        "if true; then eval ': ${X:=doc-}'; ( eval \"$X\"lattice ); fi & wait",
        (
            "if true; then shopt -s lastpipe; "
            "( S=': ${X:=doc-}'; printf y | eval \"$S\"; "
            'eval "$X"lattice ); fi | cat'
        ),
        (
            "shopt -s lastpipe; { S=': ${X:=doc-}'; "
            'printf y | eval "$S"; eval "$X"lattice; } | cat'
        ),
        ('V=lastpipe; shopt -s "$V"; S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'),
        ('shopt -s lastpipe; S=\': ${X:=doc-}\'; printf x | { eval "$S"; }; eval "$X"lattice'),
        (
            "shopt -s lastpipe; S=': ${X:=doc-}'; "
            'printf x | while read item; do eval "$S"; done; eval "$X"lattice'
        ),
    ],
    ids=(
        "later-eval",
        "same-eval",
        "alias",
        "nested-default",
        "scoped-later-eval",
        "brace-shared-later-eval",
        "conditional-possible-later-eval",
        "called-function-later-eval",
        "matched-case-later-eval",
        "nonempty-select-later-eval",
        "lastpipe-consumer-later-eval",
        "builtin-lastpipe-consumer-later-eval",
        "builtin-separator-lastpipe-consumer-later-eval",
        "nested-builtin-lastpipe-consumer-later-eval",
        "command-lastpipe-consumer-later-eval",
        "invalid-mixed-shopt-preserves-lastpipe",
        "compound-producer-unset-keeps-lastpipe",
        "compound-middle-unset-keeps-lastpipe",
        "compound-producer-local-eval-state",
        "compound-consumer-local-eval-state",
        "background-compound-local-eval-state",
        "coprocess-compound-local-eval-state",
        "isolated-consumer-local-lastpipe-enable",
        "isolated-producer-local-lastpipe-enable",
        "background-local-lastpipe-enable",
        "coprocess-local-lastpipe-enable",
        "if-producer-nested-subshell-state",
        "if-consumer-nested-subshell-state",
        "for-producer-nested-subshell-state",
        "while-producer-nested-subshell-state",
        "case-producer-nested-subshell-state",
        "background-if-nested-subshell-state",
        "if-producer-nested-subshell-lastpipe",
        "isolated-producer-inherits-lastpipe",
        "dynamic-lastpipe-consumer-later-eval",
        "lastpipe-compound-consumer-later-eval",
        "lastpipe-loop-consumer-later-eval",
    ),
)
def test_eval_second_pass_assignment_updates_reachable_variable_state(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "S=': ${X:='; S+='doc-}'; eval \"$S\"; eval \"$X\"lattice",
        "A='doc-}'; S=': ${X:='; S+=$A; eval \"$S\"; eval \"$X\"lattice",
        "S=': ${X:=${Y:='; S+='doc-}}'; eval \"$S\"; eval \"$Y\"lattice",
    ],
    ids=("append", "append-alias", "nested-append"),
)
def test_eval_second_pass_assignment_spans_reachable_variable_writes(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "eval '${X:=doc-}lattice'; X=safe",
        "false && X=safe; eval '${X:=doc-}lattice'",
        "if false; then X=safe; fi; eval '${X:=doc-}lattice'",
        "while false; do X=safe; done; eval '${X:=doc-}lattice'",
        "false && { X=safe; }; eval '${X:=doc-}lattice'",
        "f() { X=safe; }; eval '${X:=doc-}lattice'",
        "function f { X=safe; }; eval '${X:=doc-}lattice'",
        "function f() { X=safe; }; eval '${X:=doc-}lattice'",
        "case x in y) X=safe;; esac; eval '${X:=doc-}lattice'",
        "select x in; do X=safe; break; done; eval '${X:=doc-}lattice'",
        "coproc X=safe; eval '${X:=doc-}lattice'",
        "X=safe & eval '${X:=doc-}lattice'",
        "{ X=safe; } & eval '${X:=doc-}lattice'",
        'S=\': ${X=doc-}\'; eval "$S"; eval "$X"lattice; X=safe',
    ],
    ids=(
        "future",
        "short-circuit-prior",
        "conditional-prior",
        "loop-prior",
        "short-circuit-brace-prior",
        "uncalled-function-prior",
        "uncalled-function-keyword-prior",
        "uncalled-function-keyword-parentheses-prior",
        "unmatched-case-prior",
        "empty-select-prior",
        "coprocess-prior",
        "background-prior",
        "background-compound-prior",
        "future-after-later-eval",
    ),
)
def test_eval_second_pass_assignment_ignores_non_dominating_global_writes(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "X=safe /bin/true; eval '${X:=doc-}lattice'",
        "X=safe true; eval '${X:=doc-}lattice'",
        "X=safe command true; eval '${X:=doc-}lattice'",
        "X=safe env true; eval '${X:=doc-}lattice'",
        "X=safe printf %s x; eval '${X:=doc-}lattice'",
        "X=safe :; eval '${X:=doc-}lattice'",
        "X=safe eval ':'; eval '${X:=doc-}lattice'",
    ],
    ids=("external", "builtin", "command", "env", "printf", "colon", "eval"),
)
def test_eval_second_pass_assignment_ignores_prior_command_prefixes(script: str):
    assert_taint_refusal(script)


def test_static_eval_command_prefix_assignment_does_not_persist():
    result = scan_doc_lattice_invocations("X=safe; eval 'X=doc- true'; eval \"$X\"lattice")

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_static_eval_assignment_only_command_persists_control():
    assert_taint_refusal("X=safe; eval 'X=doc-'; eval \"$X\"lattice")


def test_static_eval_command_prefix_is_visible_to_same_command_control():
    assert_taint_refusal('eval "X=doc- bash -c \'eval \\"\\$X\\"lattice\'"')


@pytest.mark.parametrize(
    "script",
    ["eval '# doc-lattice'", "eval 'echo # doc-lattice'"],
    ids=("command-position", "after-command"),
)
def test_static_eval_active_comments_do_not_reach_execution_sink(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    ["""eval 'echo "#" doc-lattice'""", r"""eval 'echo \# doc-lattice'"""],
    ids=("quoted", "escaped"),
)
def test_static_eval_inactive_comment_markers_remain_taint_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "script",
    [
        "X=safe; unset X; eval '${X:=doc-}lattice'",
        "X=safe; builtin unset X; eval '${X:=doc-}lattice'",
        "X=safe; command unset X; eval '${X:=doc-}lattice'",
        "X=safe; unset -v X; eval '${X:=doc-}lattice'",
        "X=safe; NAME=X; unset \"$NAME\"; eval '${X:=doc-}lattice'",
    ],
    ids=("direct", "builtin-wrapper", "command-wrapper", "variable-option", "dynamic-target"),
)
def test_eval_second_pass_assignment_observes_prior_variable_unsets(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "X=safe; eval '${X:=doc-}lattice'",
        "X=safe eval '${X:=doc-}lattice'",
        "X=safe command eval '${X:=doc-}lattice'",
        "X=safe builtin eval '${X:=doc-}lattice'",
        "X=safe; /usr/bin/unset X; eval '${X:=doc-}lattice'",
        "X=safe; env unset X; eval '${X:=doc-}lattice'",
        "X=safe; unset -f X; eval '${X:=doc-}lattice'",
    ],
    ids=(
        "assignment-only",
        "current-eval",
        "current-command-eval",
        "current-builtin-eval",
        "external-unset",
        "env-unset",
        "function-unset",
    ),
)
def test_eval_second_pass_assignment_preserves_exact_setness_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'declare X=doc-; eval "$X"lattice',
        'declare -x X=doc-; eval "$X"lattice',
        'export X=doc-; eval "$X"lattice',
        'readonly X=doc-; eval "$X"lattice',
        'typeset X=doc-; eval "$X"lattice',
        'X=doc-; declare X+=lattice; eval "$X"',
        'f() { local X=doc-; eval "$X"lattice; }; f',
        'f() { typeset X=doc-; eval "$X"lattice; }; f',
        "X=safe; f() { local X; eval '${X:=doc-}lattice'; }; f",
        "X=safe; f() { local X=; eval '${X:=doc-}lattice'; }; f",
        'N=X; declare "$N"=doc-; eval "$X"lattice',
        'export -p X=doc-; eval "$X"lattice',
        'readonly -p X=doc-; eval "$X"lattice',
    ],
    ids=(
        "declare",
        "declare-option",
        "export",
        "readonly",
        "typeset",
        "append",
        "function-local",
        "function-typeset",
        "local-unset-shadow",
        "local-empty-shadow",
        "dynamic-name",
        "export-print-option",
        "readonly-print-option",
    ),
)
def test_assignment_builtins_update_reachable_eval_variables(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "declaration",
    ["local X=doc-", "local -g X=doc-"],
    ids=("local", "local-global-option"),
)
def test_top_level_local_assignment_builtin_does_not_write(declaration: str):
    result = scan_doc_lattice_invocations(f'{declaration}; eval "$X"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_top_level_declare_global_assignment_remains_valid_control():
    assert_taint_refusal('declare -g X=doc-; eval "$X"lattice')


def test_assignment_builtin_plus_n_removes_nameref_attribute():
    result = scan_doc_lattice_invocations(
        'X=safe; declare -n R=X; declare +n R; R=doc-; eval "$X"lattice'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_assignment_builtin_minus_n_rebinds_nameref_control():
    assert_taint_refusal('X=safe; declare -n R=X; declare -n R=Y; R=doc-; eval "$Y"lattice')


def test_function_local_plus_n_removes_nameref_attribute():
    result = scan_doc_lattice_invocations(
        'f(){ X=safe; local -n R=X; local +n R; R=doc-; eval "$X"lattice;}; f'
    )

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'f() { local X; X=doc-; eval "$X"lattice; }; f',
        'f() { local X; export X=doc-; eval "$X"lattice; }; f',
        'f() { local X; readonly X=doc-; eval "$X"lattice; }; f',
        "X=safe; f() { local X=safe; unset X; eval '${X:=doc-}lattice'; }; f",
        ("X=safe; f() { local X=safe; N=X; unset \"$N\"; eval '${X:=doc-}lattice'; }; f"),
        'X=safe; f() { local X=doc-; local Y=$X; eval "$Y"lattice; }; f',
        'X=safe; f() { local X=doc-; export Y=$X; eval "$Y"lattice; }; f',
    ],
    ids=(
        "ordinary-assignment",
        "export-assignment",
        "readonly-assignment",
        "exact-unset",
        "dynamic-unset",
        "local-rhs",
        "export-rhs",
    ),
)
def test_function_local_mutations_update_the_active_shadow(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'f() { local X=doc-; }; f; eval "$X"lattice',
        'f() { typeset X=doc-; }; f; eval "$X"lattice',
        'f() { local X; X=doc-; }; f; eval "$X"lattice',
        'f() { local X; export X=doc-; }; f; eval "$X"lattice',
        '/usr/bin/declare X=doc-; eval "$X"lattice',
        'env declare X=doc-; eval "$X"lattice',
        'declare -f X; eval "$X"lattice',
        'export X; eval "$X"lattice',
    ],
    ids=(
        "local-does-not-leak",
        "typeset-does-not-leak",
        "ordinary-local-does-not-leak",
        "exported-local-does-not-leak",
        "external-declare",
        "env-declare",
        "function-query",
        "export-without-value",
    ),
)
def test_assignment_builtins_preserve_scope_and_nonmutation_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_declare_global_option_bypasses_function_local_shadow():
    assert_taint_refusal('f() { local X=safe; declare -g X=doc-; }; f; eval "$X"lattice')


@pytest.mark.parametrize("builtin", ["declare", "typeset"])
def test_dynamic_global_option_conservatively_reaches_outer_eval(builtin: str):
    assert_taint_refusal(f'OPT=-g; f() {{ {builtin} "$OPT" X=doc-; }}; f; eval "$X"lattice')


@pytest.mark.parametrize(
    "script",
    [
        'X=safe; inner() { eval "$X"lattice; }; outer() { local X=doc-; inner; }; outer',
        'inner() { eval "$X"lattice; }; outer() { local X=doc-; inner; }; outer',
    ],
    ids=("shadowed-global", "unset-global"),
)
def test_called_function_reads_callers_dynamic_local_scope(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'f() { eval "$X"lattice; }; X=doc- f',
        'X=safe; f() { eval "$X"lattice; }; X=doc- f',
        'f() { local Y=$X; eval "$Y"lattice; }; X=doc- f',
    ],
    ids=("unset-global", "shadowed-global", "local-rhs"),
)
def test_function_call_prefix_assignments_reach_function_body(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'f() { :; }; X=doc- f; eval "$X"lattice',
        'f() { eval "$X"lattice; }; X=doc- /bin/true; f',
    ],
    ids=("nonpersistent-after-call", "unrelated-external-prefix"),
)
def test_function_call_prefix_assignments_preserve_nonleak_controls(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "split",
    range(1, len("${X:=doc-}")),
    ids=lambda split: f"split-{split}",
)
def test_eval_second_pass_assignment_spans_every_parameter_token_split(split: int):
    parameter = "${X:=doc-}"
    script = f"S=': {parameter[:split]}'; S+='{parameter[split:]}'; eval \"$S\"; eval \"$X\"lattice"

    assert_taint_refusal(script)


def test_eval_second_pass_assignment_spans_parameter_introducer_alias():
    assert_taint_refusal("A='{X:=doc-}'; S=': $'; S+=$A; eval \"$S\"; eval \"$X\"lattice")


@pytest.mark.parametrize(
    "split",
    range(1, len("$(printf doc-)lattice")),
    ids=lambda split: f"split-{split}",
)
def test_eval_second_pass_command_substitution_spans_every_token_split(split: int):
    source = "$(printf doc-)lattice"
    script = f"S='{source[:split]}'; S+='{source[split:]}'; eval \"$S\""
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint eval command substitution cannot be bounded"


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        (
            "S='doc-\\'; S+='lattice'; eval \"$S\"",
            TAINT_REFUSAL_REASON,
        ),
        (
            "S='`printf doc-'; S+='`lattice'; eval \"$S\"",
            "shell taint eval command substitution cannot be bounded",
        ),
    ],
    ids=("backslash", "backtick"),
)
def test_eval_second_pass_retains_other_split_lexical_introducers(
    script: str,
    reason: str,
):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == reason


@pytest.mark.parametrize(
    "script",
    [
        'S=\'${X:-doc-}\'; eval "$S"; eval "$X"lattice',
        '( S=\'${X:=doc-}\'; eval "$S" ); eval "$X"lattice',
        'X=safe; S=\': ${X:=doc-}\'; eval "$S"; eval "$X"lattice',
        "X=''; S=': ${X=doc-}'; eval \"$S\"; eval \"$X\"lattice",
        'Y=safe; S=\': ${X:=${Y:=doc-}}\'; eval "$S"; eval "$X"lattice',
        "X=safe; eval '${X:=doc-}lattice'",
        "X=''; eval '${X=doc-}lattice'",
        "Y=safe; eval '${X:=${Y:=doc-}}lattice'",
        '( X=safe; S=\'${X:=doc-}\'; eval "$S"; eval "$X"lattice )',
        "( X=''; S='${X=doc-}'; eval \"$S\"; eval \"$X\"lattice )",
        'S=\': ${X:=doc-}\'; eval "$S" | cat; eval "$X"lattice',
        'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice',
        'S=\': ${X:=doc-}\'; { eval "$S"; } | cat; eval "$X"lattice',
        'S=\': ${X:=doc-}\'; printf x | { eval "$S"; }; eval "$X"lattice',
        ('S=\': ${X:=doc-}\'; for item in x; do eval "$S"; done | cat; eval "$X"lattice'),
        ('S=\': ${X:=doc-}\'; printf x | while read item; do eval "$S"; done; eval "$X"lattice'),
        ('S=\': ${X:=doc-}\'; if true; then eval "$S"; fi | cat; eval "$X"lattice'),
        ('S=\': ${X:=doc-}\'; printf x | if true; then eval "$S"; fi; eval "$X"lattice'),
        ('S=\': ${X:=doc-}\'; case x in x) eval "$S";; esac | cat; eval "$X"lattice'),
        ('S=\': ${X:=doc-}\'; printf x | case x in x) eval "$S";; esac; eval "$X"lattice'),
        'S=\': ${X:=doc-}\'; eval "$S" & wait; eval "$X"lattice',
        'S=\': ${X:=doc-}\'; { eval "$S"; } & wait; eval "$X"lattice',
        'S=\': ${X:=doc-}\'; coproc eval "$S"; wait; eval "$X"lattice',
        'S=\': ${X:=doc-}\'; coproc { eval "$S"; }; wait; eval "$X"lattice',
        'shopt -u lastpipe; S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice',
        (
            "shopt -s lastpipe; shopt -u lastpipe; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "builtin shopt -s lastpipe; builtin shopt -u lastpipe; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "shopt -s lastpipe; printf x | shopt -u lastpipe; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "shopt -s lastpipe; printf x | { shopt -u lastpipe; }; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "{ shopt -s lastpipe; } | cat; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "printf x | { shopt -s lastpipe; }; "
            'S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice'
        ),
        (
            "shopt -s lastpipe; { shopt -u lastpipe; S=': ${X:=doc-}'; "
            'printf y | eval "$S"; eval "$X"lattice; } | cat'
        ),
        'shopt -su lastpipe; S=\': ${X:=doc-}\'; printf x | eval "$S"; eval "$X"lattice',
        'shopt -s lastpipe; S=\': ${X:=doc-}\'; eval "$S" | cat; eval "$X"lattice',
        'shopt -s lastpipe; S=\': ${X:=doc-}\'; { eval "$S"; } | cat; eval "$X"lattice',
    ],
    ids=(
        "non-assigning-default",
        "isolated-subshell",
        "preset-nonempty-colon",
        "preset-empty-no-colon",
        "nested-preset-nonempty",
        "direct-preset-nonempty-colon",
        "direct-preset-empty-no-colon",
        "direct-nested-preset-nonempty",
        "scoped-preset-nonempty-colon",
        "scoped-preset-empty-no-colon",
        "pipeline-producer",
        "pipeline-consumer",
        "compound-pipeline-producer",
        "compound-pipeline-consumer",
        "loop-pipeline-producer",
        "loop-pipeline-consumer",
        "if-pipeline-producer",
        "if-pipeline-consumer",
        "case-pipeline-producer",
        "case-pipeline-consumer",
        "background-eval",
        "background-compound-eval",
        "coprocess-eval",
        "coprocess-compound-eval",
        "lastpipe-disabled-consumer",
        "lastpipe-enabled-then-disabled-consumer",
        "builtin-lastpipe-enabled-then-disabled-consumer",
        "direct-consumer-unset-clears-lastpipe",
        "compound-consumer-unset-clears-lastpipe",
        "compound-producer-set-keeps-default",
        "compound-consumer-set-keeps-default",
        "isolated-producer-local-lastpipe-disable",
        "invalid-mixed-shopt-keeps-default",
        "lastpipe-producer",
        "lastpipe-compound-producer",
    ),
)
def test_eval_second_pass_nonpersistent_assignment_controls_stay_clean(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_eval_second_pass_colon_assignment_updates_preset_empty_variable():
    assert_taint_refusal("X=''; S=': ${X:=doc-}'; eval \"$S\"; eval \"$X\"lattice")


@pytest.mark.parametrize(
    "script",
    [
        "S='${X:-safe}'; eval \"$S\"lattice",
        "S='${X/pattern/safe}'; eval \"$S\"lattice",
    ],
    ids=("default", "replacement"),
)
def test_eval_second_pass_complex_parameter_without_marker_stays_clean(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        "S='${X:-$(printf doc-)}'; eval \"$S\"lattice",
        "S='${X:-`printf doc-`}'; eval \"$S\"lattice",
        "S='${X:-${Y:-$(printf doc-)}}'; eval \"$S\"lattice",
        """S='${X:-$(printf %s "`printf doc-`")}'; eval "$S"lattice""",
        """S='${X:-`printf %s "$(printf doc-)"`}'; eval "$S"lattice""",
    ],
    ids=("modern", "legacy", "nested-default", "legacy-in-modern", "modern-in-legacy"),
)
def test_eval_parameter_operand_command_substitution_fails_closed(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint eval command substitution cannot be bounded"


def test_deep_eval_parameter_command_substitution_fails_with_stable_bound():
    nested = "$(" * 1100 + "printf doc-" + ")" * 1100
    result = scan_doc_lattice_invocations(f"S='${{X:-{nested}}}'; eval \"$S\"lattice")

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint eval command substitution cannot be bounded"


@pytest.mark.parametrize(
    "script",
    [
        r"""S='${X:-\$(printf doc-)}'; eval "$S"lattice""",
    ],
    ids=("escaped",),
)
def test_inactive_eval_parameter_operand_command_substitution_stays_clean(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_brace_alternatives_become_separate_eval_argv_ports():
    result = scan_doc_lattice_invocations("eval {doc-,lattice}")

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        'D=doc-; bash {,-c} "$D"lattice',
        'D=doc-; bash {,} -c "$D"lattice',
        "printf '%s%s\\n' doc- lattice >task.sh; bash {,} task.sh",
    ],
    ids=("selector-leading-empty", "selector-empty-word", "resource-empty-word"),
)
def test_unquoted_empty_brace_fields_are_elided_before_shell_sink_selection(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'D=doc-; bash ""{,-c} "$D"lattice',
        'D=doc-; bash ""{,} -c "$D"lattice',
        "printf '%s%s\\n' doc- lattice >task.sh; bash \"\"{,} task.sh",
    ],
    ids=("selector-leading-empty", "selector-empty-word", "resource-empty-word"),
)
def test_quoted_empty_brace_fields_remain_real_shell_arguments(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_brace_empty_field_presence_is_scoped_to_quoted_alternative():
    assert_taint_refusal('D=doc-; bash {,"-c"} "$D"lattice')


def test_brace_quoted_empty_alternative_remains_a_real_argument_control():
    result = scan_doc_lattice_invocations('D=doc-; bash {"",-c} "$D"lattice')

    assert result.invocations == NONE
    assert result.incomplete_reason is None


@pytest.mark.parametrize(
    "script",
    [
        'X=doc-{lattice,noop}; eval "$X"',
        'X=doc-; X+={lattice,noop}; eval "$X"',
        "X='doc-{lattice,noop}'; eval \"$X\"",
    ],
    ids=("assignment", "append-assignment", "quoted-assignment"),
)
def test_eval_second_pass_expands_braces_from_variable_content(script: str):
    assert_taint_refusal(script)


def test_eval_second_pass_keeps_separate_brace_argv_clean():
    result = scan_doc_lattice_invocations("X='{doc-,lattice}'; eval \"$X\"")

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        'X=doc-{; X+=lattice,noop}; eval "$X"',
        'X=doc-; X+={lattice,; X+=noop}; eval "$X"',
    ],
    ids=("opening-in-assignment", "closing-in-later-append"),
)
def test_eval_second_pass_preserves_active_braces_across_writes(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'X={doc-,x}lattice; eval "$X"',
        'X={doc-,x}; X+=lattice; eval "$X"',
        'X={doc-,x}{lattice,y}; eval "$X"',
        '''X='{doc-,x}"lattice"'; eval "$X"''',
    ],
    ids=("same-write-suffix", "append-suffix", "cartesian-groups", "quoted-suffix"),
)
def test_eval_second_pass_distributes_suffixes_across_brace_words(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        '''X='{doc-,x} lattice'; eval "$X"''',
        '''X='{doc-,x} "lattice"'; eval "$X"''',
    ],
    ids=("unquoted-next-word", "quoted-next-word"),
)
def test_eval_second_pass_word_separator_flushes_brace_words(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        '''X="doc-'{"; X+="lattice,noop}'"; eval "$X"''',
        r'''X='doc-\{'; X+='lattice,noop\}'; eval "$X"''',
        '''X='{doc-,'; X+='lattice}'; eval "$X"''',
    ],
    ids=("quoted", "escaped", "separate-argv"),
)
def test_eval_second_pass_cross_write_brace_controls_stay_clean(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_eval_second_pass_cartesian_brace_words_obey_alternative_cap():
    result = scan_doc_lattice_invocations('X="{a..q}{a..q}"; eval "$X"')

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint brace expansion limit exceeded"


@pytest.mark.parametrize(
    "script",
    [
        'A=doc-{; B=lattice,noop}; eval "$A$B"',
        'P=doc-; A={; B=lattice,x}; eval "$P$A$B"',
        'A={doc-,x; B=}; C=lattice; eval "$A$B$C"',
        "A='{doc-,x'; B='}{lattice,y}'; eval \"$A$B\"",
        'A=doc-{; B=$A; C=$B; D=lattice,x}; eval "$C$D"',
        'X=doc-{; X+=lat; X+=tice,x}; Y=$X; eval "$Y"',
    ],
    ids=(
        "open-prefix",
        "separate-prefix",
        "separate-suffix",
        "cartesian-groups",
        "alias-chain",
        "distinct-appends",
    ),
)
def test_eval_second_pass_composes_brace_stream_across_variables(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        '''A="doc-'{"; B="lattice,x}'"; eval "$A$B"''',
        r'''A='doc-\{'; B='lattice,x\}'; eval "$A$B"''',
        '''A='{doc-,'; B='lattice}'; eval "$A$B"''',
        "A='{'; B=$A; C=$B; eval \"$C\"",
        'X={; X+=x; Y=$X; X=$Y; eval "$X"',
        'X={; X+=x; X+=y; Y=$X; X=$Y; eval "$X"',
    ],
    ids=(
        "quoted-braces",
        "escaped-braces",
        "separate-argv",
        "acyclic-alias-chain",
        "single-append-alias",
        "distinct-append-alias",
    ),
)
def test_eval_second_pass_variable_stream_controls_stay_clean(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_eval_second_pass_cyclic_variable_transition_fails_closed():
    result = scan_doc_lattice_invocations('A={; B=$C; C=$B; eval "$A$B"')

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint eval syntax fixed-point update limit exceeded"


@pytest.mark.parametrize(
    "script",
    [
        ("A=doc-{; if true; then B='lattice,x}'; else B='safe,x}'; fi; eval \"$A$B\""),
        ("A=doc-{; if true; then B='safe,x}'; else B='lattice,x}'; fi; eval \"$A$B\""),
        ("A=doc-{; if true; then B=lat; else B=safe; fi; B+='tice,x}'; eval \"$A$B\""),
    ],
    ids=("marker-first", "marker-last", "append-every-branch"),
)
def test_eval_second_pass_joins_competing_brace_variable_definitions(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        ("A=doc-{; if true; then B='safe,x}'; else B='noop,y}'; fi; eval \"$A$B\""),
        ("A=doc-{; if true; then B='lattice,'; else B='x}'; fi; eval \"$A$B\""),
    ],
    ids=("clean-alternatives", "separate-definitions"),
)
def test_eval_second_pass_competing_definitions_do_not_concatenate(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        '''eval "'doc-{lattice,noop}'"''',
        r'''eval "doc-\{lattice,noop\}"''',
    ],
    ids=("quoted-eval-text", "escaped-eval-text"),
)
def test_eval_second_pass_keeps_inactive_braces_literal(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        "X={1..1000}; true",
        "X={doc-,$Y}lattice; true",
        "X={1..1000} true",
        "X=({1..1000}); true",
    ],
    ids=("range", "dynamic", "prefix", "array"),
)
def test_assignment_rhs_defers_brace_expansion_until_reparsed(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_assignment_shaped_argv_word_expands_braces_before_pipeline_sink():
    assert_taint_refusal("printf %s X=doc-{lattice,noop} | bash")


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("printf %s X={1..1000}", "shell taint brace expansion limit exceeded"),
        (
            "printf %s X={doc-,$Y}lattice",
            "shell taint dynamic brace expansion cannot be bounded",
        ),
    ],
    ids=("range-cap", "dynamic"),
)
def test_assignment_shaped_argv_word_applies_brace_bounds(script: str, reason: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == reason


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ('X={1..1000}; eval "$X"', "shell taint brace expansion limit exceeded"),
        (
            'X={doc-,$Y}lattice; eval "$X"',
            "shell taint dynamic brace expansion cannot be bounded",
        ),
    ],
    ids=("range-cap", "dynamic"),
)
def test_eval_second_pass_applies_deferred_assignment_braces(script: str, reason: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == reason


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("eval doc-{1..1000}-lattice", "shell taint brace expansion limit exceeded"),
        ("eval {doc-,$X}lattice", "shell taint dynamic brace expansion cannot be bounded"),
    ],
)
def test_brace_expansion_bounds_fail_closed(script: str, reason: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == reason


@pytest.mark.parametrize(
    "range_body",
    [
        f"1..{'9' * 5000}",
        f"{'9' * 5000}..2",
        f"1..2..{'9' * 5000}",
        f"1..2..-{'9' * 5000}",
    ],
    ids=("stop", "start", "step", "signed-step"),
)
def test_oversized_numeric_brace_range_fails_closed(range_body: str):
    result = scan_doc_lattice_invocations(f"eval {{{range_body}}}")

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint brace expansion limit exceeded"


@pytest.mark.parametrize(
    "range_body",
    [
        f"a..z..{'9' * 5000}",
        f"a..z..+{'9' * 5000}",
        f"a..z..-{'9' * 5000}",
    ],
    ids=("step", "positive-step", "negative-step"),
)
def test_oversized_letter_brace_range_step_fails_closed(range_body: str):
    result = scan_doc_lattice_invocations(f"eval {{{range_body}}}")

    assert result.invocations == NONE
    assert result.incomplete_reason == "shell taint brace expansion limit exceeded"


@pytest.mark.parametrize(
    "script",
    [
        "eval doc-{l..n..0}attice",
        "eval doc-{l..n..-1}attice",
        "eval doc-{l..j..1}attice",
    ],
    ids=("zero-step", "opposite-step", "descending-opposite-step"),
)
def test_bash_normalized_brace_range_step_still_surfaces_marker(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script_operand",
    ["task.sh", "./task.sh"],
    ids=("plain", "dot-normalized"),
)
def test_split_printf_file_handoff_reaches_shell_script_sink(script_operand: str):
    assert_taint_refusal(
        f"printf '%s%s\\n' 'doc-' 'lattice reconcile' > task.sh\nbash {script_operand}"
    )


def test_padded_brace_range_preserves_shell_script_resource_key():
    assert_taint_refusal("printf '%s%s\\n' 'doc-' 'lattice reconcile' > 01\nbash {01..01}")


def test_heredoc_passthrough_reaches_written_script_sink():
    assert_taint_refusal("cat > task.sh <<'EOF'\ndoc-lattice reconcile\nEOF\nbash task.sh")


def test_split_herestring_reaches_shell_stdin_sink():
    assert_taint_refusal("X=doc-\nX+='lattice reconcile'\nbash <<< \"$X\"")


def test_static_descriptor_zero_read_reaches_shell_stdin_sink():
    assert_taint_refusal("printf '%s%s\\n' 'doc-' 'lattice reconcile' > task.sh\nbash < task.sh")


def test_duplicated_static_input_descriptor_reaches_shell_stdin_sink():
    assert_taint_refusal("printf '%s%s' 'doc-' 'lattice reconcile' > task.sh; bash 3< task.sh 0<&3")


def test_duplicated_static_output_descriptor_receives_stdout():
    assert_taint_refusal("printf '%s%s' 'doc-' 'lattice reconcile' 3> task.sh 1>&3; bash task.sh")


@pytest.mark.parametrize(
    "operator",
    ["&>", "&>>"],
    ids=("truncate", "append"),
)
def test_combined_redirect_copy_routes_stdout_to_task(operator: str):
    assert_taint_refusal(
        f"printf '%s%s' 'doc-' 'lattice reconcile' {operator} task.sh 1>&2; bash task.sh"
    )


@pytest.mark.parametrize(
    "operator",
    ["&>", "&>>"],
    ids=("truncate", "append"),
)
def test_rebound_stderr_breaks_combined_redirect_copy(operator: str):
    result = scan_doc_lattice_invocations(
        f"printf '%s%s' 'doc-' 'lattice reconcile' {operator} task.sh "
        "2>/dev/null 1>&2; bash task.sh"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_nonzero_static_read_is_not_shell_stdin():
    result = scan_doc_lattice_invocations(
        "printf '%s%s\\n' 'doc-' 'lattice reconcile' > task.sh\nbash 3< task.sh"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_final_stdout_binding_routes_bytes_to_task():
    assert_taint_refusal(
        "printf '%s%s\\n' 'doc-' 'lattice reconcile' > /dev/null > task.sh\nbash task.sh"
    )


def test_overwritten_stdout_binding_leaves_only_empty_task():
    result = scan_doc_lattice_invocations(
        "printf '%s%s\\n' 'doc-' 'lattice reconcile' > task.sh > /dev/null\nbash task.sh"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        ("printf '%s%s' 'doc-' 'lattice reconcile' > task.sh; bash 3< task.sh 0<&3 </dev/null"),
        ("printf '%s%s' 'doc-' 'lattice reconcile' 3> task.sh 1>&3 > /dev/null; bash task.sh"),
    ],
    ids=("stdin", "stdout"),
)
def test_later_redirection_overrides_duplicated_descriptor_binding(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_nonzero_heredoc_is_not_shell_stdin():
    result = scan_doc_lattice_invocations("bash 3<<'EOF'\ndoc-lattice reconcile\nEOF")

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_parent_path_alias_reaches_written_script_sink():
    assert_taint_refusal(
        "mkdir -p foo\nprintf '%s%s' 'doc-' 'lattice reconcile' > task.sh\nbash foo/../task.sh"
    )


@pytest.mark.parametrize(
    "script",
    [
        f"{'9' * 5000}> task.sh",
        f"bash 0<&{'9' * 5000}",
        f"printf x 1>&{'9' * 5000}",
    ],
    ids=("prefix", "input-operand", "output-operand"),
)
def test_oversized_descriptor_reports_structured_incomplete_reason(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == "file descriptor digit limit exceeded"


def test_eval_taint_is_order_insensitive_within_run_body():
    assert_taint_refusal("eval \"$X\"\nX=doc-\nX+='lattice reconcile'")


def test_eval_reparses_single_quoted_variable_reference_argument():
    assert_taint_refusal("X=doc-\neval '${X}lattice reconcile --all'")


def test_eval_reparse_keeps_second_pass_single_quoted_variable_reference_literal():
    result = scan_doc_lattice_invocations("X=doc-\neval \"'\\${X}lattice reconcile --all'\"")

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_eval_reparse_interprets_quotes_contributed_by_variable_value():
    assert_taint_refusal('A="doc-\'"; eval "$A""lattice\'" --help')


def test_eval_reparse_decodes_ansi_c_literal_escapes():
    assert_taint_refusal(r'''eval "\$'doc-\x6cattice' --help"''')


def test_eval_reparse_keeps_external_only_value_non_evidentiary():
    result = scan_doc_lattice_invocations('eval "$EXTERNAL"')

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "parameter",
    [
        "$@",
        "$*",
        "$1",
        "${@}",
        "${*}",
        "${1}",
        "${2}",
        "${9}",
        "${10}",
        "${!}",
        "${#}",
        "${?}",
        "${-}",
        "${$}",
        "${0}",
    ],
)
def test_eval_reparse_treats_special_parameters_as_external_gap(parameter: str):
    assert_taint_refusal(f"eval 'doc-{parameter}lattice --help'")


@pytest.mark.parametrize(
    "script",
    [
        'X="\\$\'unterminated"; echo ok',
        "X=\"\\$'unterminated\"; eval 'echo ok'",
    ],
    ids=("no-eval", "eval-unrelated-variable"),
)
def test_unreachable_malformed_eval_syntax_does_not_block_certification(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_eval_reachability_ignores_variable_reference_escaped_inside_ansi_c_literal():
    result = scan_doc_lattice_invocations("X=\"\\$'unterminated\"; eval $'\\\\$X'")

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        "X=\"$'unterminated\"; eval '$X'",
        "X=\"\\$'unterminated\"; eval '$X'",
    ],
    ids=("ordinary-dollar", "escaped-dollar"),
)
def test_second_pass_variable_expansion_does_not_reparse_its_value_as_shell_syntax(
    script: str,
):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('A="printf \'doc-"; eval "$A""lattice\'"', "doc-lattice"),
        (r'''eval "printf \$'doc-\x6cattice'"''', "doc-lattice"),
    ],
    ids=("variable-contributed-quote", "ansi-c-escape"),
)
def test_eval_bash_probe_confirms_second_pass_marker_construction(script: str, expected: str):
    result = subprocess.run(  # noqa: S603 - parameterized test scripts are literal safe probes
        ["/bin/bash", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == expected


def test_eval_with_only_partial_marker_variable_stays_certified():
    result = scan_doc_lattice_invocations('A=doc-\nB=lattice\neval "$A"')

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    ("script", "tainted"),
    [
        ('eval "doc-${EXTERNAL}lattice"', True),
        ('eval "doc${EXTERNAL}lattice"', False),
    ],
    ids=("authored-separator", "no-authored-separator"),
)
def test_eval_parameter_gap_taint_depends_on_authored_marker_separator(script: str, tainted: bool):
    if tainted:
        assert_taint_refusal(script)
        return
    result = scan_doc_lattice_invocations(script)
    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_unmodeled_wrapper_retains_decoded_literal_marker_refusal():
    result = scan_doc_lattice_invocations('dispatch "doc-${EXTERNAL}lattice reconcile"')

    assert result.incomplete_reason == (
        "marker-bearing command is not a certified doc-lattice invocation"
    )


@pytest.mark.parametrize(
    "script",
    [
        "bash -c 'doc-lattice reconcile'",
        'eval "doc-lattice reconcile"',
    ],
    ids=("shell-command", "eval"),
)
def test_modeled_sink_with_complete_authored_marker_retains_phase_one_refusal(
    script: str,
):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason == (
        "marker-bearing command is not a certified doc-lattice invocation"
    )


def test_complete_marker_assignment_retains_phase_one_refusal_reason():
    result = scan_doc_lattice_invocations("X='doc-lattice reconcile'\neval \"$X\"")

    assert result.invocations == NONE
    assert result.incomplete_reason == (
        "marker-bearing command is not a certified doc-lattice invocation"
    )


def test_literal_command_after_unrelated_fragments_remains_classified():
    result = scan_doc_lattice_invocations(
        "X=unrelated\nX+=' fragments'\ndoc-lattice reconcile --dry-run"
    )

    assert result.incomplete_reason is None
    assert result.invocations == RECONCILE_DRY


@pytest.mark.parametrize(
    "script",
    [
        ': $(X=doc-)\nX+=lattice\neval "$X"',
        ': <(X=doc-)\nX+=lattice\neval "$X"',
        '(X=doc-)\nX+=lattice\neval "$X"',
    ],
    ids=("command-substitution", "process-substitution", "subshell-group"),
)
def test_nested_scope_assignments_do_not_taint_outer_variable_flow(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        '{ bash; } <<<"doc-lattice reconcile"',
        "{ bash; } < <(printf '%s%s\\n' doc- 'lattice reconcile')",
        "{ bash; } <<'EOF'\ndoc-lattice reconcile\nEOF",
    ],
    ids=("here-string", "process-substitution", "heredoc"),
)
def test_compound_scope_entry_receives_redirected_stdin(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- 'lattice reconcile' | { bash; }",
        "printf '%s%s\\n' doc- 'lattice reconcile' | ( cat | bash )",
    ],
    ids=("brace-group", "subshell-pipeline"),
)
def test_pipeline_reaches_compound_scope_entries(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- 'lattice reconcile' > >( { bash; } )",
        "printf '%s%s\\n' doc- 'lattice reconcile' > >(cat | bash)",
        "printf '%s%s\\n' doc- 'lattice reconcile' > >(bash | cat)",
    ],
    ids=("brace-group", "pipeline-sink-last", "pipeline-sink-first"),
)
def test_output_process_substitution_reaches_nested_scope_entries(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        '{ X=doc-; X+=lattice; eval "$X reconcile"; }',
        'echo "$(X=doc-; X+=lattice; eval "$X reconcile")"',
        '{ X=doc-; }; eval "$X"lattice reconcile',
    ],
    ids=("brace-local-eval", "command-substitution-local-eval", "brace-parent-leak"),
)
def test_nested_assignments_reach_nested_and_brace_shared_eval_sinks(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        '( X=doc-; ); eval "$X"lattice reconcile',
        'echo "$(X=doc-)"; eval "$X"lattice reconcile',
    ],
    ids=("subshell", "command-substitution"),
)
def test_isolated_nested_assignments_do_not_leak_to_outer_eval(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- lattice >out | bash",
        "printf '%s%s\\n' doc- lattice | cat >out | bash",
        "{ printf doc-; printf lattice; } >out | bash",
    ],
    ids=("producer", "intermediate-consumer", "compound-producer"),
)
def test_stdout_redirection_detaches_pipeline_from_later_consumer(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- lattice | { true; bash; }",
        '{ true; bash; } <<<"doc-lattice reconcile"',
        "printf '%s%s\\n' doc- lattice > >( true; bash )",
    ],
    ids=("pipeline", "here-string", "output-process-substitution"),
)
def test_shared_compound_stdin_reaches_later_possible_consumers(script: str):
    assert_taint_refusal(script)


def test_later_compound_consumer_redirection_overrides_shared_stdin():
    result = scan_doc_lattice_invocations(
        "printf '%s%s\\n' doc- lattice | { true; bash <<<'true'; }"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        'P=doc-; X=lattice; { bash; } <<<"$P$X reconcile"',
        "P=doc-; X=lattice; { bash; } <<EOF\n$P$X reconcile\nEOF",
        'P=doc-; X=lattice; ( bash ) <<<"$P$X reconcile"',
    ],
    ids=("brace-here-string", "brace-heredoc", "subshell-here-string"),
)
def test_compound_redirection_content_uses_scope_environment(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- lattice 2>&1 1>&2 | bash",
        "printf '%s%s\\n' doc- lattice 3>&1 1>&3 | bash",
        "printf '%s%s\\n' doc- lattice 2>&1 1>&2 | { bash; }",
    ],
    ids=("stderr-alias", "descriptor-three-alias", "compound-consumer"),
)
def test_stdout_alias_back_to_implicit_pipe_reaches_consumer(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "printf '%s%s\\n' doc- lattice >/dev/null | bash",
        "printf '%s%s\\n' doc- lattice 1>&3 | bash",
        "printf '%s%s\\n' doc- lattice 2>&1 1>/dev/null | bash",
    ],
    ids=("null", "descriptor-away", "alias-then-null"),
)
def test_final_stdout_binding_away_from_implicit_pipe_stays_clean(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_if_branch_outputs_remain_mutually_exclusive_at_pipeline_sink():
    result = scan_doc_lattice_invocations(
        'if test -n "$EXTERNAL"; then printf doc-; else printf lattice; fi | bash'
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_known_false_if_condition_excludes_unreachable_branch_output():
    result = scan_doc_lattice_invocations("if false; then printf doc-; printf lattice; fi | bash")

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_known_untaken_compound_consumer_does_not_forward_pipeline_input():
    result = scan_doc_lattice_invocations(
        "{ printf doc-; printf lattice; } | { if false; then cat; fi; } | bash"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_for_iteration_bindings_and_repeated_body_reach_pipeline_sink():
    assert_taint_refusal('for X in doc- lattice; do printf %s "$X"; done | bash')


def test_select_iteration_bindings_and_repeated_body_reach_pipeline_sink():
    assert_taint_refusal('select X in doc- lattice; do printf %s "$X"; done | bash')


def test_while_test_body_test_repetition_reaches_pipeline_sink():
    assert_taint_refusal(
        "P=$'#\\n'; "
        'while printf %s "$P" && test "$P" = $\'#\\n\'; '
        "do printf doc-; P=lattice; done | bash"
    )


def test_while_brace_test_list_and_percent_b_repetition_reaches_pipeline_sink():
    assert_taint_refusal(
        "i=0; P=$'#\\n'; "
        'while { printf %b "$P"; test "$i" -lt 1; }; '
        "do printf doc-; P=lattice; i=1; done | bash"
    )


@pytest.mark.parametrize(
    "script",
    [
        "while false; do printf doc-; printf lattice; done | bash",
        "until true; do printf doc-; printf lattice; done | bash",
    ],
    ids=("while-false", "until-true"),
)
def test_known_zero_iteration_loop_excludes_unreachable_body_output(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "escape",
    [r"\154", r"\554", r"\x6c", r"\u006c"],
    ids=("octal", "byte-masked-octal", "hex", "unicode"),
)
def test_percent_b_decoded_escapes_reach_pipeline_sink(escape: str):
    assert_taint_refusal(f"printf %b 'doc-{escape}attice' | bash")


@pytest.mark.parametrize(
    ("conversion", "width_argument"),
    [
        ("%5b", ""),
        ("%-5b", ""),
        ("%.20b", ""),
        ("%10.20b", ""),
        ("%*b", "0 "),
    ],
    ids=("width", "left-width", "precision", "width-precision", "star-width"),
)
def test_modified_percent_b_decoded_escapes_reach_pipeline_sink(
    conversion: str,
    width_argument: str,
):
    assert_taint_refusal(f"printf '{conversion}' {width_argument}'doc-\\154attice' | bash")


def test_until_body_repetition_reaches_pipeline_sink():
    assert_taint_refusal('P=doc-; until false; do printf %s "$P"; P=lattice; done | bash')


def test_case_double_semicolon_arms_remain_mutually_exclusive_at_pipeline_sink():
    result = scan_doc_lattice_invocations(
        "case x in x) printf doc-;; y) printf lattice;; esac | bash"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_case_fallthrough_sequences_adjacent_arm_outputs_at_pipeline_sink():
    assert_taint_refusal("case x in x) printf doc-;& y) printf lattice;; esac | bash")


def test_case_retest_fallthrough_can_skip_a_nonmatching_later_arm():
    result = scan_doc_lattice_invocations(
        "case x in x) printf doc-;;& y) printf lattice;; esac | bash"
    )

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_case_retest_fallthrough_can_take_a_matching_later_arm():
    assert_taint_refusal("case x in x) printf doc-;;& x) printf lattice;; esac | bash")


def test_if_evidence_routes_a_choice_scope_into_its_pipeline():
    scanner = _ShellScanner(
        'if test -n "$EXTERNAL"; then printf a; else printf b; fi | cat',
        classify_commands=False,
    )
    builder = scanner.taint_builder

    assert scanner.scan() == NONE
    assert builder is not None
    scope = next(scope for scope in builder.scopes if scope.kind == "if")
    assert isinstance(scope.output, SequenceOutput)
    assert isinstance(scope.output.parts[-1], ChoiceOutput)
    assert any(pipe.producer_scope_id == scope.scope_id for pipe in builder.pipes)


@pytest.mark.parametrize(
    ("kind", "script", "repeat_index"),
    [
        ("for", 'for X in a b; do printf %s "$X"; done | cat', None),
        ("while", "while true; do printf a; done | cat", 1),
        ("until", "until false; do printf a; done | cat", 1),
    ],
)
def test_loop_evidence_routes_repetition_scopes_into_their_pipelines(
    kind: str,
    script: str,
    repeat_index: int | None,
):
    scanner = _ShellScanner(
        script,
        classify_commands=False,
    )
    builder = scanner.taint_builder

    assert scanner.scan() == NONE
    assert builder is not None
    scope = next(scope for scope in builder.scopes if scope.kind == kind)
    if repeat_index is None:
        repeat = scope.output
    else:
        assert isinstance(scope.output, SequenceOutput)
        repeat = scope.output.parts[repeat_index]
    assert isinstance(repeat, RepeatOutput)
    assert any(pipe.producer_scope_id == scope.scope_id for pipe in builder.pipes)


def test_case_evidence_routes_a_choice_scope_into_its_pipeline():
    scanner = _ShellScanner(
        "case x in x) printf a;; y) printf b;; esac | cat",
        classify_commands=False,
    )
    builder = scanner.taint_builder

    assert scanner.scan() == NONE
    assert builder is not None
    scope = next(scope for scope in builder.scopes if scope.kind == "case")
    assert isinstance(scope.output, ChoiceOutput)
    assert any(pipe.producer_scope_id == scope.scope_id for pipe in builder.pipes)


def test_large_case_statement_fails_closed_before_python_recursion():
    arms = " ".join(f"p{index}) :;;" for index in range(1200))
    result = scan_doc_lattice_invocations(f"case unmatched in {arms} esac")

    assert result.invocations == NONE
    assert result.incomplete_reason == "case arm limit exceeded"


def test_near_limit_dynamic_case_retest_chain_remains_bounded():
    arms = " ".join(f"p{index}) :;;&" for index in range(256))
    result = scan_doc_lattice_invocations(f'case "$EXTERNAL" in {arms} esac')

    assert result.invocations == NONE
    assert result.incomplete_reason == "case dynamic branch limit exceeded"


@pytest.mark.parametrize(
    "script",
    [
        "if true; then printf doc-",
        'for X in doc- lattice; do printf %s "$X"',
        "case x in x) printf doc-;;",
    ],
    ids=("if", "for", "case"),
)
def test_unterminated_structured_control_frames_fail_closed(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is not None


@pytest.mark.parametrize(
    "script",
    [
        'X=safe; ! true && X=doc-; eval "$X"lattice',
        'X=safe; if ! true; then X=doc-; fi; eval "$X"lattice',
    ],
    ids=("and-list", "if-condition"),
)
def test_negated_true_status_skips_unreachable_marker_writes(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        '! false && X=doc-; eval "$X"lattice',
        'if ! false; then X=doc-; fi; eval "$X"lattice',
    ],
    ids=("and-list", "if-condition"),
)
def test_negated_false_status_reaches_marker_writes(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'false && true || X=doc-; eval "$X"lattice',
        'true || false && X=doc-; eval "$X"lattice',
    ],
    ids=("false-and-true-or", "true-or-false-and"),
)
def test_and_or_lists_use_left_associative_status(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        'true() { false; }; X=safe; if true; then :; else X=doc-; fi; eval "$X"lattice',
        'false() { true; }; if false; then X=doc-; fi; eval "$X"lattice',
    ],
    ids=("true-shadowed", "false-shadowed"),
)
def test_active_functions_shadow_literal_status_builtins(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "if true; then eval 'X=doc-'; fi; eval \"$X\"lattice",
        "X=safe; if true; then eval 'unset X'; fi; eval '${X:=doc-}lattice'",
    ],
    ids=("write", "unset"),
)
def test_definitely_taken_branches_propagate_exact_eval_mutations(script: str):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "X=safe; if false; then eval 'X=doc-'; fi; eval \"$X\"lattice",
        "X=safe; if false; then eval 'unset X'; fi; eval '${X:=doc-}lattice'",
    ],
    ids=("write", "unset"),
)
def test_untaken_branches_do_not_propagate_exact_eval_mutations(script: str):
    result = scan_doc_lattice_invocations(script)

    assert result.incomplete_reason is None
    assert result.invocations == NONE


def test_multiple_elif_keeps_an_earlier_taken_branch_state():
    assert_taint_refusal(
        'if true; then X=doc-; elif true; then X=safe; elif true; then X=safe; fi; eval "$X"lattice'
    )


@pytest.mark.parametrize(
    ("condition", "tainted"),
    [("false", False), ("true", True)],
    ids=("untaken", "taken"),
)
def test_static_eval_branch_mutations_follow_exact_condition(
    condition: str,
    tainted: bool,
):
    script = f"X=safe; eval 'if {condition}; then X=doc-; fi'; eval \"$X\"lattice"
    if tainted:
        assert_taint_refusal(script)
        return

    result = scan_doc_lattice_invocations(script)
    assert result.incomplete_reason is None
    assert result.invocations == NONE


@pytest.mark.parametrize(
    "script",
    [
        """\
X=doc-
X+=lattice
builtin eval "$X"
""",
        """\
X=doc-
X+=lattice
command eval "$X"
""",
        """\
X=doc-
X+=lattice
uv run bash -c "$X"
""",
        """\
X=doc-
X+='lattice reconcile'
bash "$OPT" "$X"
""",
        ("printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; chmod +x task.sh; ./task.sh"),
        ("printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; source ./task.sh"),
        ("printf '%s%s\\n' doc- 'lattice reconcile' > task.sh; . ./task.sh"),
        ("printf %s doc- > task.sh; printf %s lattice >> task.sh; bash task.sh"),
    ],
    ids=[
        "builtin-eval",
        "command-eval",
        "uv-run-shell",
        "ambiguous-selector",
        "direct-path",
        "source",
        "dot-source",
        "resource-append",
    ],
)
def test_phase_two_launcher_and_selector_sinks_refuse(script):
    assert_taint_refusal(script)


@pytest.mark.parametrize(
    "script",
    [
        "echo 'make build' > run.sh; bash run.sh",
        "eval doc- lattice",
        """\
bash -c 'echo ok' <<'EOF'
doc-lattice reconcile
EOF
""",
        """\
X=doc-
X+=lattice
env eval "$X"
""",
        "bash -c 'echo hi' > doc-lattice.log",
        ("P=task.sh; printf '%s%s\\n' doc- 'lattice reconcile' > \"$P\"; bash task.sh"),
        ("printf '%s%s\\n' doc- 'lattice reconcile' 2> task.sh; bash task.sh"),
    ],
    ids=[
        "marker-free-generated-script",
        "eval-space-barrier",
        "shell-c-ignores-stdin",
        "external-eval-lookup",
        "redirection-name-is-not-content",
        "dynamic-resource-identity",
        "stderr-does-not-carry-stdout-payload",
    ],
)
def test_phase_two_mandatory_certification_rows(script):
    result = scan_doc_lattice_invocations(script)

    assert result.invocations == NONE
    assert result.incomplete_reason is None


def test_phase_two_refusal_retains_resolved_findings_from_the_same_body():
    result = scan_doc_lattice_invocations(
        """\
doc-lattice check
X=doc-
X+=lattice
eval "$X"
"""
    )

    assert result.invocations == CHECK
    assert result.incomplete_reason == TAINT_REFUSAL_REASON


@pytest.mark.parametrize(
    ("_description", "script", "extra_environment"),
    PHASE_TWO_RUNTIME_REFUSALS,
    ids=[row[0] for row in PHASE_TWO_RUNTIME_REFUSALS],
)
def test_phase_two_refusal_fixture_executes_marker_under_real_bash(
    _description,
    script,
    extra_environment,
    tmp_path: Path,
):
    bash = shutil.which("bash", path=os.defpath)
    assert bash is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = tmp_path / "marker-ran"
    doc_lattice = bin_dir / "doc-lattice"
    doc_lattice.write_text(
        '#!/bin/sh\n: > "$MARKER_PROBE"\n',
        encoding="utf-8",
    )
    doc_lattice.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\n[ "$1" = run ] || exit 64\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    environment = {
        **extra_environment,
        "LC_ALL": "C",
        "MARKER_PROBE": str(probe),
        "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
    }

    scan = scan_doc_lattice_invocations(script)
    completed = subprocess.run(  # noqa: S603 - fixtures execute static plan-authored scripts
        [bash, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert scan.invocations == NONE
    assert scan.incomplete_reason == TAINT_REFUSAL_REASON
    assert probe.exists(), completed.stderr
