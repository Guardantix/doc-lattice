"""Tests for the bounded, non-executing doc-lattice shell invocation scanner."""

import pytest
from typer.main import get_command

from doc_lattice.cli.application import create_app
from doc_lattice.error_types import ConfigError, ProjectError
from doc_lattice.github_ci.shell_scanner import (
    _DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS,
    _DOC_LATTICE_ROOT_OPTIONS,
    _ShellScanIncomplete,
    direct_doc_lattice_invocations,
    scan_doc_lattice_invocations,
)

NONE = ()
LINEAR = (("linear", False),)
RECONCILE = (("reconcile", False),)
RECONCILE_DRY = (("reconcile", True),)
CHECK = (("check", False),)
LINEAR_LINT = (("linear", False), ("lint", False))

ACCEPTANCE_CASES = [
    # Literal executable identity and control syntax.
    ("ansi-c executable", "$'doc-lattice' linear", LINEAR),
    ("locale executable", '$"doc-lattice" linear', LINEAR),
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
        "ansi-quoted heredoc suppresses substitution",
        "cat <<$'EOF'\n$(doc-lattice linear)\nEOF",
        NONE,
    ),
    (
        "locale-quoted heredoc suppresses substitution",
        'cat <<$"EOF"\n$(doc-lattice linear)\nEOF',
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
]


@pytest.mark.parametrize(
    ("_description", "script", "expected"),
    ACCEPTANCE_CASES,
    ids=[case[0] for case in ACCEPTANCE_CASES],
)
def test_direct_doc_lattice_acceptance_corpus(_description, script, expected):
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


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_direct_doc_lattice_invocations_does_not_continue_after_escaped_backslash(newline):
    script = "doc-lattice rec" + "\\\\" + newline + "oncile --dry-run"

    assert direct_doc_lattice_invocations(script) == (("rec" + "\\", False),)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("PATH+=:/tools doc-lattice linear --exit-code", (("linear", False),)),
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
        'coproc "$NAME" doc-lattice linear',
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
        ("uv tool run doc-lattice@2.0.0 linear", LINEAR),
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


@pytest.mark.parametrize(
    "script",
    [
        "uv sync",
        "uv pip install doc-lattice",
        "uv run doc-lattice@2.0.0 linear",
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


def test_shell_scan_incomplete_is_a_coded_project_error():
    error = _ShellScanIncomplete("step limit exceeded")

    assert isinstance(error, ProjectError)
    assert error.code == "SHELL_SCAN_INCOMPLETE"


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


def test_direct_doc_lattice_invocations_removes_locale_quoted_heredoc_body():
    script = """\
cat <<$"EOF"
harmless
EOF
doc-lattice linear
"""

    assert direct_doc_lattice_invocations(script) == (("linear", False),)


def test_direct_doc_lattice_invocations_keeps_hash_inside_shell_word():
    script = "doc-lattice reconcile --all --ref --dry-run#suffix"

    assert direct_doc_lattice_invocations(script) == (("reconcile", False),)


def test_direct_doc_lattice_invocations_tracks_parameter_expansion_in_substitution():
    script = '''echo "$(printf %s ${x:-)}; printf '%s' '`doc-lattice linear`')"'''

    assert direct_doc_lattice_invocations(script) == ()


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
