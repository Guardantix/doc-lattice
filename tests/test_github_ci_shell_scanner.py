"""Tests for the bounded, non-executing doc-lattice shell invocation scanner."""

import pytest
from typer.core import TyperGroup
from typer.main import get_command

from doc_lattice.cli.application import create_app
from doc_lattice.error_types import ConfigError, ProjectError
from doc_lattice.github_ci.launcher_policy import ScanWord, resolve_command
from doc_lattice.github_ci.shell_scanner import (
    _DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS,
    _DOC_LATTICE_ROOT_OPTIONS,
    _RECONCILE_FLAGS,
    _RECONCILE_OPTIONS_WITH_ARGUMENTS,
    _ScanBudget,
    _ShellScanIncomplete,
    _ShellScanner,
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)

NONE = ()
LINEAR = (("linear", False),)
RECONCILE = (("reconcile", False),)
RECONCILE_DRY = (("reconcile", True),)
CHECK = (("check", False),)
LINEAR_LINT = (("linear", False), ("lint", False))
INCOMPLETE = object()

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
        NONE,
    ),
    (
        "single-quoted substitution literal",
        "echo '$(doc-lattice linear)'",
        NONE,
    ),
    (
        "inner single-quoted substitution literal",
        """echo "$(printf '%s' '$(doc-lattice linear)')\"""",
        NONE,
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
        NONE,
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
        NONE,
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
        NONE,
    ),
    (
        "multiline single-quoted literal",
        "printf '%s' 'doc-lattice linear\nuv run doc-lattice reconcile'",
        NONE,
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
    ("bare uv tool install remains non-candidate", "uv tool install doc-lattice", NONE),
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
        ("echo 'doc-lattice linear'", ()),
        ('printf "%s\\n" "doc-lattice reconcile --all"', ()),
        (
            "set +e\ndoc-lattice check\nrc_check=$?\ndoc-lattice lint\nrc_lint=$?\n",
            (("check", False), ("lint", False)),
        ),
        ("if doc-lattice linear; then printf ok; fi", (("linear", False),)),
    ],
)
def test_direct_doc_lattice_invocations_handles_documented_forms(script, expected):
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
        ("echo\r doc-lattice linear", NONE),
    ],
    ids=["hash-remains-word-text", "carriage-return-remains-command-text"],
)
def test_direct_doc_lattice_invocations_preserves_lone_carriage_returns(script, expected):
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
    ],
    ids=["indexed", "declare-indexed", "arithmetic-subscript"],
)
def test_direct_doc_lattice_invocations_treats_array_literals_as_data(script):
    assert direct_doc_lattice_invocations(script) == NONE


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("args=($(doc-lattice linear))", LINEAR),
        ("args=(<(doc-lattice reconcile --all))", RECONCILE),
        ("args=(doc-lattice linear)\ndoc-lattice check", CHECK),
    ],
    ids=["command-substitution", "process-substitution", "following-command"],
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
def test_direct_doc_lattice_invocations_ignores_indirect_or_similarly_named_commands(script):
    assert direct_doc_lattice_invocations(script) == ()


@pytest.mark.parametrize(
    "script",
    [
        "{doc-lattice linear",
        "doc-lattice --version linear",
        "doc-lattice --no-color --version linear",
    ],
)
def test_direct_doc_lattice_invocations_does_not_widen_dynamic_or_nonexecuting_forms(
    script,
):
    assert direct_doc_lattice_invocations(script) == ()


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
def test_direct_doc_lattice_invocations_ignores_unsupported_builtin_targets(script):
    assert direct_doc_lattice_invocations(script) == NONE


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
def test_direct_doc_lattice_does_not_treat_quoted_single_field_expansion_as_erasable(script):
    assert direct_doc_lattice_invocations(script) == NONE


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
def test_direct_doc_lattice_invocations_does_not_treat_dynamic_assignment_name_as_prefix(script):
    assert direct_doc_lattice_invocations(script) == NONE


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


def test_direct_doc_lattice_invocations_honors_env_option_terminator():
    assert direct_doc_lattice_invocations("env -- -S doc-lattice linear") == NONE


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
def test_direct_doc_lattice_invocations_ignores_nonexecuting_command_forms(script):
    assert direct_doc_lattice_invocations(script) == ()


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


@pytest.mark.parametrize("subcommand", ["install doc-lattice", "list"])
def test_uv_tool_bare_non_run_subcommand_stays_not_candidate(subcommand):
    script = f"uv tool {subcommand}"

    assert scan_doc_lattice_invocations(script).incomplete_reason is None
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


def _literal_policy_words(script: str) -> tuple[ScanWord, ...]:
    offset = 0
    words = []
    for text in script.split():
        words.append(ScanWord(text, offset, offset + len(text), unstable=False))
        offset += len(text) + 1
    return tuple(words)


@pytest.mark.parametrize(
    "script",
    [
        "uv tool -q run doc-lattice linear",
        "uv tool --offline run doc-lattice linear",
        "uv tool --no-cache run doc-lattice linear",
        "uv tool --frobnicate run doc-lattice linear",
        "uv tool -q install doc-lattice",
        "uv tool install doc-lattice",
        "uv tool list",
        "uvx --no-sync doc-lattice linear",
        "uv tool run --no-sync doc-lattice linear",
        "uv run --no-sync doc-lattice linear",
    ],
)
def test_scanner_matches_launcher_policy_on_issue_102_fixtures(script):
    floor = resolve_command(_literal_policy_words(script))
    scanner = scan_doc_lattice_invocations(script)
    scanner_kind = (
        "refused"
        if scanner.incomplete_reason is not None
        else "resolved"
        if scanner.invocations
        else "not_candidate"
    )

    assert scanner_kind == floor.kind
    if floor.invocation is not None:
        assert scanner.invocations == (floor.invocation,)


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


@pytest.mark.parametrize(
    "script",
    [
        "uv sync",
        "uv pip install doc-lattice",
        "uvx other-doc-lattice==2.0.0 linear",
        "uvx doc-lattice-tools>=2.0.0 linear",
        "uv run doc-lattice@2.0.0 linear",
        "uv run command doc-lattice linear",
    ],
)
def test_direct_doc_lattice_invocations_ignores_uv_non_launcher_forms(script):
    assert direct_doc_lattice_invocations(script) == ()


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
def test_direct_doc_lattice_invocations_ignores_literal_backticks(script):
    assert direct_doc_lattice_invocations(script) == ()


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


def test_direct_doc_lattice_invocations_ignores_literal_backticks_in_substitution():
    script = '''echo "$(printf '%s' '`doc-lattice linear`')"'''

    assert direct_doc_lattice_invocations(script) == ()


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


def test_direct_doc_lattice_invocations_tracks_parameter_expansion_in_substitution():
    script = '''echo "$(printf %s ${x:-)}; printf '%s' '`doc-lattice linear`')"'''

    assert direct_doc_lattice_invocations(script) == ()


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
