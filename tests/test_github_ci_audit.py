"""Tests for repository-global and managed GitHub CI audit policy."""

import os
import re
from pathlib import Path

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.audit import (
    SECRET_NAMES,
    audit_global_workflows,
    audit_managed_installation,
    audit_repository,
)
from doc_lattice.github_ci.filesystem import (
    MAX_CUMULATIVE_WORKFLOW_BYTES,
    MAX_WORKFLOW_BYTES,
    MAX_WORKFLOW_FILES,
    discover_workflows,
    inspect_installed_artifacts,
)
from doc_lattice.github_ci.identity import parse_repository
from doc_lattice.github_ci.model import InstalledArtifact, WorkflowDiscovery, WorkflowDocument
from doc_lattice.github_ci.render import (
    CANONICAL_ARTIFACT_TARGETS,
    CHECKOUT_REF,
    SETUP_UV_REF,
    render_managed_artifacts,
)
from doc_lattice.github_ci.workflow_parser import parse_workflow


def _workflow(text: str, path: str = ".github/workflows/example.yml") -> WorkflowDocument:
    return parse_workflow(Path(path), text)


def _finding_codes(findings) -> set[str]:
    return {finding.code for finding in findings}


def test_global_audit_allows_effective_linear_help_on_pr():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice linear --help
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == set()


def test_secret_name_regex_single_sources_from_secret_names():
    from doc_lattice.github_ci import audit as audit_module  # noqa: PLC0415

    parts = audit_module._SECRET_NAME_ALTERNATION.split("|")

    # The alternation is derived from SECRET_NAMES, so a new secret extends detection.
    assert set(parts) == {re.escape(name) for name in SECRET_NAMES}
    # Longest-first ordering keeps an alternative from partially matching a longer name.
    assert [len(part) for part in parts] == sorted((len(part) for part in parts), reverse=True)


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("doc-lattice --no-color linear", "PR_LINEAR_INVOCATION"),
        ("doc-lattice --no-color reconcile --all", "PR_MUTATING_RECONCILE"),
        (
            "uvx --from doc-lattice==2.1.0 doc-lattice --no-color linear",
            "PR_LINEAR_INVOCATION",
        ),
        ("uvx doc-lattice==2.0.0 linear", "PR_LINEAR_INVOCATION"),
        (
            "uv tool run doc-lattice==2.0.0 reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
        (
            "uv run --exact doc-lattice reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
        (
            "uv tool run --refresh doc-lattice reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
        ("uvx -qv doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("{ doc-lattice linear; }", "PR_LINEAR_INVOCATION"),
        ("{ doc-lattice reconcile --all; }", "PR_MUTATING_RECONCILE"),
        ("time -p doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("time -p -- doc-lattice reconcile --all", "PR_MUTATING_RECONCILE"),
        (r"\time -p doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("'time' -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("command time -p doc-lattice linear", "PR_LINEAR_INVOCATION"),
        (
            "exec time -p -- doc-lattice reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
        ("coproc DL doc-lattice reconcile --all", "PR_MUTATING_RECONCILE"),
        (
            "coproc DL uvx --from doc-lattice==2.1.0 doc-lattice linear",
            "PR_LINEAR_INVOCATION",
        ),
        (
            "coproc DL uv run doc-lattice reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
        ("coproc DL env X=1 doc-lattice linear", "PR_LINEAR_INVOCATION"),
        (
            "coproc DL command doc-lattice reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
        ("uv run env X=1 doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("uv run time doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("uvx /usr/bin/time -p doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("uv run env X=1 time doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("uv run uvx doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("/usr/bin/time doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("env /usr/bin/time -p doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("env time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("command env time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("exec env time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("time env time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("env env time -- doc-lattice linear", "PR_LINEAR_INVOCATION"),
    ],
)
def test_global_audit_rejects_root_options_and_compound_grammar_on_pr(
    script,
    expected_code,
):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {expected_code}


@pytest.mark.parametrize(
    "script",
    ['./"$TOOLS"/doc-lattice linear', 'tools/"$OS"/doc-lattice reconcile --all'],
    ids=["dot-relative", "nested-relative"],
)
def test_global_audit_fails_closed_on_dynamic_relative_doc_lattice_paths(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*dynamic relative doc-lattice"):
        audit_global_workflows((document,))


def test_global_audit_fails_closed_on_uv_tool_option_before_run_selector():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: uv tool -q run doc-lattice linear
""",
        path=".github/workflows/selector-option.yml",
    )

    with pytest.raises(
        ConfigError,
        match=(
            r"\.github/workflows/selector-option\.yml: shell scan incomplete: "
            r"uv tool option before the run selector"
        ),
    ):
        audit_global_workflows((document,))


def test_global_audit_allows_literal_doc_lattice_array_data_on_pr():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          args=(doc-lattice linear)
          declare -a reconcile_args=(doc-lattice reconcile --all)
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == set()


def test_global_audit_rejects_linear_after_quoted_heredoc_continuation():
    script = "cat <<'EOF'\nbody \\\nEOF\ndoc-lattice linear"
    indented_script = script.replace("\n", "\n          ")
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {indented_script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_LINEAR_INVOCATION"}


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("doc-lattice \\\n  linear", "PR_LINEAR_INVOCATION"),
        ("doc-lattice \\\n  reconcile --all", "PR_MUTATING_RECONCILE"),
    ],
    ids=["linear", "mutating-reconcile"],
)
def test_global_audit_rejects_indented_command_continuations(script, expected_code):
    indented_script = script.replace("\n", "\n          ")
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {indented_script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {expected_code}


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("env --uns NAME doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("env -iu NAME doc-lattice reconcile --all", "PR_MUTATING_RECONCILE"),
    ],
)
def test_global_audit_rejects_env_option_values_on_pr(script: str, expected_code: str):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: {script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {expected_code}


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("exec -ca fake doc-lattice linear", "PR_LINEAR_INVOCATION"),
        ("exec -la fake doc-lattice reconcile --all", "PR_MUTATING_RECONCILE"),
    ],
)
def test_global_audit_rejects_clustered_exec_argv0_on_pr(script: str, expected_code: str):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: {script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {expected_code}


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("builtin exec doc-lattice linear", "PR_LINEAR_INVOCATION"),
        (
            "builtin command doc-lattice reconcile --all",
            "PR_MUTATING_RECONCILE",
        ),
    ],
)
def test_global_audit_rejects_supported_builtin_wrappers_on_pr(
    script: str,
    expected_code: str,
):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: {script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {expected_code}


@pytest.mark.parametrize(
    "script",
    [
        # A trailing backslash in a comment does not continue it, so the next line still runs.
        "# harmless \\\ndoc-lattice linear",
        # Unbalanced $((...)) is a command substitution running a subshell, not arithmetic.
        "x=$((doc-lattice linear) )",
        # Unbalanced ((...)) is a nested subshell, not an arithmetic command.
        "((doc-lattice linear) )",
    ],
    ids=["comment-backslash", "dollar-arithmetic-fallback", "arithmetic-command-fallback"],
)
def test_global_audit_rejects_linear_hidden_by_comment_or_arithmetic_fallback(script):
    # These forms run `doc-lattice linear` in Bash while a naive scanner would swallow the
    # region, so the PR audit must still emit PR_LINEAR_INVOCATION rather than pass.
    indented_script = script.replace("\n", "\n          ")
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {indented_script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_LINEAR_INVOCATION"}


def test_global_audit_fails_closed_at_shell_invocation_limit():
    script = "\n".join([*(["doc-lattice check"] * 10_000), "doc-lattice linear"])
    assert len(script.encode()) < MAX_WORKFLOW_BYTES
    indented_script = script.replace("\n", "\n          ")
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {indented_script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*invocation limit"):
        audit_global_workflows((document,))


def test_global_audit_fails_closed_on_brace_expanded_subcommand():
    # Bash expands `doc-lattice linea{r,}` to `doc-lattice linear linea` and runs linear, so a
    # scanner that declined to classify it would let the PR audit pass. The scan cannot certify
    # the subcommand, so the audit must fail closed (ConfigError, exit 2) rather than return no
    # findings and silently approve the workflow.
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          doc-lattice linea{r,}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*brace or glob expansion"):
        audit_global_workflows((document,))


def test_global_audit_escapes_control_characters_in_incomplete_shell_scan_context():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          doc-lattice linea{r,}
""",
        ".github/workflows/bad\nname\x1b.yml",
    )

    with pytest.raises(ConfigError) as caught:
        audit_global_workflows((document,))

    message = str(caught.value)
    assert ".github/workflows/bad\\nname\\u001b.yml: shell scan incomplete" in message
    assert "brace or glob expansion" in message
    assert "\n" not in message
    assert "\x1b" not in message


def test_repository_audit_fails_closed_on_env_split_string():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          env --split-string='doc-lattice reconcile --all'
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


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
def test_repository_audit_fails_closed_on_wrapped_env_split_string(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


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
def test_global_audit_fails_closed_on_dynamic_shell_assignment_before_env_split_string(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "script",
    [
        "$(true) env -S 'doc-lattice linear'",
        "command $(true) env -S 'doc-lattice linear'",
        "exec $(true) env -S 'doc-lattice linear'",
        "time $(true) env -S 'doc-lattice linear'",
        "shopt -s nullglob; no-match-* env -S 'doc-lattice linear'",
    ],
    ids=["top-level", "command-wrapper", "exec-wrapper", "time-prefix", "active-glob"],
)
def test_global_audit_fails_closed_on_erasable_command_boundary_before_env_split_string(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "script",
    [
        "\"$@\" env -S 'doc-lattice linear'",
        "\"${@}\" env -S 'doc-lattice linear'",
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
        "unbraced-nameref",
        "command-wrapper",
        "exec-wrapper",
        "time-prefix",
        "coproc-prefix",
        "path-qualified-env",
    ],
)
def test_global_audit_fails_closed_on_quoted_zero_field_boundary_before_env_split_string(
    script,
):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "script",
    [
        'command "$OPT" doc-lattice linear',
        'exec "$OPT" doc-lattice linear',
        'command -p "$OPT" doc-lattice linear',
        'exec -a label "$OPT" doc-lattice linear',
        'uv "$OPT" run doc-lattice linear',
        'uv "$SUBCOMMAND" doc-lattice linear',
        "uv $OPT doc-lattice linear",
        'uv "$OPT" -- doc-lattice linear',
        'uv "$OPT" --group dev doc-lattice linear',
        'uv "$OPT" run --from doc-lattice==2.1.0 doc-lattice linear',
        'uv "$GLOBAL" tool "$RUN" doc-lattice linear',
        "uv --directory $OPT doc-lattice linear",
        "uv --project $OPT doc-lattice linear",
        "uv --cache-dir $OPT doc-lattice linear",
        'uv --directory "${@:1}" doc-lattice linear',
        "uv --directory $OPT -- doc-lattice linear",
        "uv --directory $OPT --from doc-lattice==2.1.0 doc-lattice linear",
        'uv run "$OPT" doc-lattice linear',
        'uvx "$OPT" doc-lattice linear',
        'uv tool "$OPT" doc-lattice linear',
        'doc-lattice "$OPT" linear',
        "declare -a items=(); declare -n VALUE='items[@]'; \"$VALUE\" doc-lattice linear",
    ],
    ids=[
        "command",
        "exec",
        "command-option",
        "exec-option",
        "uv-global",
        "uv-dynamic-run",
        "uv-unquoted-dynamic-run-or-tool-run",
        "uv-dynamic-run-with-terminator",
        "uv-dynamic-run-with-option",
        "uv-dynamic-tool-run-with-option",
        "uv-dynamic-global-and-tool-run",
        "uv-directory-value",
        "uv-project-value",
        "uv-cache-dir-value",
        "uv-directory-zero-field-value",
        "uv-directory-value-run-terminator",
        "uv-directory-value-tool-run-option",
        "uv-run",
        "uvx",
        "uv-tool-run",
        "root",
        "unbraced-nameref",
    ],
)
def test_global_audit_fails_closed_on_dynamic_prefix_grammar_before_payload(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "script",
    [
        'CMD=linear; doc-lattice "$CMD"',
        "CMD='reconcile --all'; doc-lattice $CMD",
    ],
    ids=["quoted-scalar", "unquoted-multiple-fields"],
)
def test_global_audit_fails_closed_on_exhausted_dynamic_subcommand(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*command-position expansion"):
        audit_global_workflows((document,))


def test_global_audit_fails_closed_when_dynamic_uv_global_option_value_exposes_env_split_string():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          uv --directory $OPT env -S 'doc-lattice linear'
"""
    )

    with pytest.raises(
        ConfigError,
        match=r"shell scan.*(?:env split-string|command-position expansion)",
    ):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "script",
    [
        'uv run time "$*" doc-lattice linear',
        '/usr/bin/time "$(printf -- -p)" doc-lattice linear',
        'env time "$*" doc-lattice linear',
    ],
    ids=["nested", "path-qualified", "env-prefix"],
)
def test_global_audit_fails_closed_on_dynamic_external_time_prefix(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*dynamic external time prefix"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "script",
    [
        "env time -f '%e' doc-lattice linear",
        r"\time -f '%e' doc-lattice linear",
        "command time -f '%e' doc-lattice linear",
        "exec time -f '%e' doc-lattice linear",
    ],
    ids=["env-prefix", "escaped", "command-wrapper", "exec-wrapper"],
)
def test_global_audit_fails_closed_on_unknown_external_time_option(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*external time option"):
        audit_global_workflows((document,))


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
def test_global_audit_does_not_treat_quoted_single_field_expansion_as_erasable(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == set()


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
def test_repository_audit_fails_closed_on_env_split_string_long_option_abbreviation(
    option,
    value_separator,
):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          env {option}{value_separator}'doc-lattice reconcile --all'
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_repository_audit_fails_closed_on_dynamic_env_split_string_prefix():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          env --spl"$EMPTY" 'doc-lattice reconcile --all'
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*env split-string"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("env -u", "env option value"),
        ("env --unset", "env option value"),
        ("env -C", "env option value"),
        ("env --chdir", "env option value"),
        ("env -u $OPTIONS harmless", "env option value"),
        ('env --unset "$REF" harmless', "env option value"),
        ('env -C "${OPTIONS[@]}" harmless', "env option value"),
        ('env --chdir "${!REF}" harmless', "env option value"),
        ("env -{u,S} ignored 'doc-lattice linear'", "expandable env prefix"),
        ("env -? ignored 'doc-lattice linear'", "expandable env prefix"),
    ],
    ids=[
        "short-unset-missing",
        "long-unset-missing",
        "short-chdir-missing",
        "long-chdir-missing",
        "unquoted-value",
        "quoted-reference-value",
        "quoted-array-value",
        "quoted-indirect-value",
        "brace-prefix",
        "glob-prefix",
    ],
)
def test_repository_audit_fails_closed_on_unresolved_env_prefix(script, reason):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=rf"shell scan.*{reason}"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


@pytest.mark.parametrize(
    "script",
    [
        "env -i\"$OPTION\" 'doc-lattice linear'",
        "env --\"$OPTION\" 'doc-lattice reconcile --all'",
    ],
    ids=["short-option", "long-option"],
)
def test_repository_audit_fails_closed_on_dynamic_env_option_prefix(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {script}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*dynamic env"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_repository_audit_fails_closed_on_unquoted_dynamic_env_assignment():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          env FOO=$OPTIONS doc-lattice reconcile --all
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*unquoted dynamic env assignment"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


@pytest.mark.parametrize(
    "command",
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
    ],
)
def test_repository_audit_fails_closed_on_quoted_dynamic_env_assignment(command):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {command}
"""
    )

    with pytest.raises(ConfigError, match=r"shell scan.*quoted dynamic env assignment"):
        audit_repository(
            WorkflowDiscovery(directory_exists=True, documents=(document,)),
            (None,) * len(CANONICAL_ARTIFACT_TARGETS),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_global_audit_ignores_env_payload_after_option_terminator():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          env -- -S doc-lattice linear
"""
    )

    assert audit_global_workflows((document,)) == ()


def test_global_audit_reports_target_secret_linear_and_mutating_reconcile():
    document = _workflow(
        """\
name: unsafe
on:
  pull_request_target:
  pull_request_review:
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: ${{ secrets.LINEAR_API_KEY }}
        run: |
          doc-lattice linear --exit-code
          doc-lattice reconcile --all
"""
    )

    findings = audit_global_workflows((document,))

    assert _finding_codes(findings) == {
        "PULL_REQUEST_TARGET",
        "PR_LINEAR_INVOCATION",
        "PR_MUTATING_RECONCILE",
        "LINEAR_SECRET_REFERENCE",
    }


def test_global_audit_allows_unrelated_release_workflow():
    document = _workflow(
        """\
name: release
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: true
      - uses: actions/cache@v4
        with:
          path: .cache
      - run: uv publish
"""
    )

    assert audit_global_workflows((document,)) == ()


@pytest.mark.parametrize(
    "event",
    [
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
    ],
)
def test_global_audit_applies_command_rules_to_every_pr_event(event: str):
    document = _workflow(
        f"""\
on:
  {event}:
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          doc-lattice linear
          uv run doc-lattice reconcile --all
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {
        "PR_LINEAR_INVOCATION",
        "PR_MUTATING_RECONCILE",
    }


@pytest.mark.parametrize(
    "workflow",
    [
        """\
on: pull_request
defaults:
  run:
    shell: pwsh
jobs:
  audit:
    runs-on: ubuntu-latest
    defaults:
      run:
        shell: python {0}
    steps:
      - shell: doc-lattice linear --config {0}
        run: |
          team: engineering
""",
        """\
on: pull_request
defaults:
  run:
    shell: pwsh
jobs:
  audit:
    runs-on: ubuntu-latest
    defaults:
      run:
        shell: doc-lattice linear --config {0}
    steps:
      - run: |
          team: engineering
""",
        """\
on: pull_request
defaults:
  run:
    shell: doc-lattice linear --config {0}
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          team: engineering
""",
    ],
    ids=["step", "job-default", "workflow-default"],
)
def test_global_audit_scans_effective_shell_template(workflow: str):
    document = _workflow(workflow)

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_LINEAR_INVOCATION"}


@pytest.mark.parametrize(
    "shell",
    [
        "bash",
        "bash --noprofile --norc -eo pipefail {0}",
        "/bin/sh -eu {0}",
    ],
    ids=["builtin-bash", "custom-bash", "custom-sh"],
)
def test_global_audit_scans_supported_bash_shell_bodies(shell: str):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: {shell}
        run: doc-lattice linear
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_LINEAR_INVOCATION"}


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        (".venv/Scripts/doc-lattice.exe linear", "PR_LINEAR_INVOCATION"),
        (".venv/Scripts/doc-lattice.exe reconcile --all", "PR_MUTATING_RECONCILE"),
        ("uv run doc-lattice.exe linear", "PR_LINEAR_INVOCATION"),
        ("uv run doc-lattice.exe reconcile --all", "PR_MUTATING_RECONCILE"),
        (".venv/Scripts/DOC-LATTICE.EXE reconcile --all", "PR_MUTATING_RECONCILE"),
        ("uv run DoC-LaTtIcE.ExE linear", "PR_LINEAR_INVOCATION"),
    ],
    ids=[
        "direct-linear",
        "direct-mutating-reconcile",
        "nested-linear",
        "nested-mutating-reconcile",
        "direct-uppercase-mutating-reconcile",
        "nested-mixed-case-linear",
    ],
)
def test_global_audit_rejects_windows_console_script_launchers_on_pr(
    script: str,
    expected_code: str,
):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: windows-latest
    steps:
      - shell: bash
        run: {script}
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {expected_code}


@pytest.mark.parametrize("shell", ["bash -c {0}", "bash --init-file {0}"])
def test_global_audit_rejects_command_string_or_startup_file_shells(shell: str):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: {shell}
        run: echo safe
"""
    )

    with pytest.raises(ConfigError, match=r"unsupported shell semantics.*pull-request run step"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "workflow",
    [
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - shell: pwsh
        run: Write-Output safe
""",
        """\
on: pull_request
jobs:
  audit:
    runs-on: windows-latest
    steps:
      - run: Write-Output safe
""",
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-${{ matrix.version }}
    steps:
      - run: Write-Output safe
""",
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-private
    steps:
      - run: Write-Output safe
""",
    ],
    ids=["explicit-pwsh", "windows-default", "dynamic-runner", "custom-runner-prefix"],
)
def test_global_audit_fails_closed_on_unsupported_shell_semantics(workflow: str):
    document = _workflow(workflow)

    with pytest.raises(ConfigError, match=r"unsupported shell semantics.*pull-request run step"):
        audit_global_workflows((document,))


@pytest.mark.parametrize(
    "runner",
    [
        "ubuntu-slim",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-26.04",
        "ubuntu-22.04-arm",
        "ubuntu-24.04-arm",
        "ubuntu-26.04-arm",
        "macos-14",
        "macos-15",
        "macos-26",
        "macos-15-intel",
        "macos-26-intel",
    ],
)
def test_global_audit_scans_documented_non_windows_hosted_runner(runner: str):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: {runner}
    steps:
      - run: doc-lattice linear
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_LINEAR_INVOCATION"}


def test_global_audit_scans_singleton_array_hosted_runner():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: [ubuntu-latest]
    steps:
      - run: doc-lattice linear
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_LINEAR_INVOCATION"}


@pytest.mark.parametrize(
    "script",
    [
        "doc-lattice reconcile --all --dry-run",
        "uv run --all-extras doc-lattice reconcile --dry-run",
        "uv run -qv doc-lattice reconcile --dry-run",
        "uvx --no-index --find-links dist doc-lattice reconcile --dry-run",
    ],
    ids=["direct", "uv-run-all-extras", "uv-run-short-cluster", "uvx-no-index"],
)
def test_global_audit_allows_pr_dry_run_reconcile(script):
    document = _workflow(
        f"""\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: {script}
"""
    )

    assert audit_global_workflows((document,)) == ()


def test_global_audit_allows_quoted_glob_like_dry_run_reconcile_argument():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice reconcile 'pc[1]' --dry-run
"""
    )

    assert audit_global_workflows((document,)) == ()


def test_global_audit_rejects_dry_run_token_consumed_as_reconcile_config_value():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice reconcile pc-design --config --dry-run
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"PR_MUTATING_RECONCILE"}


def test_global_audit_does_not_apply_pr_command_rules_to_workflow_run():
    document = _workflow(
        """\
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: |
          doc-lattice linear
          doc-lattice reconcile --all
"""
    )

    assert audit_global_workflows((document,)) == ()


@pytest.mark.parametrize(
    "fragment",
    [
        "TOKEN: LINEAR_API_KEY",
        "TOKEN: DOC_LATTICE_LINEAR_API_KEY",
        "TOKEN: ${{ secrets.LINEAR_API_KEY }}",
        "TOKEN: ${{ secrets['DOC_LATTICE_LINEAR_API_KEY'] }}",
        "TOKEN: ${{ secrets [ 'LINEAR_API_KEY' ] }}",
        "TOKEN: reusable.yml?secret=DOC_LATTICE_LINEAR_API_KEY",
    ],
)
def test_global_audit_detects_linear_secret_names_in_scalar_syntaxes(fragment: str):
    document = _workflow(
        f"""\
on: workflow_call
jobs:
  reusable:
    runs-on: ubuntu-latest
    env:
      {fragment}
    steps:
      - uses: owner/reusable/.github/workflows/check.yml@main
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize("secret_name", sorted(SECRET_NAMES))
def test_global_audit_detects_secret_names_used_as_job_or_step_env_keys(secret_name: str):
    job_env = _workflow(
        f"""\
on: push
jobs:
  audit:
    runs-on: ubuntu-latest
    env:
      {secret_name}: ordinary
    steps:
      - run: true
"""
    )
    step_env = _workflow(
        f"""\
on: push
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - env:
          {secret_name}: ordinary
        run: true
"""
    )

    assert _finding_codes(audit_global_workflows((job_env,))) == {"LINEAR_SECRET_REFERENCE"}
    assert _finding_codes(audit_global_workflows((step_env,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize(
    "workflow",
    [
        """\
on:
  workflow_call:
    secrets:
      LINEAR_API_KEY:
        required: true
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: true
""",
        """\
on: push
jobs:
  reusable:
    uses: owner/repository/.github/workflows/reusable.yml@main
    secrets:
      DOC_LATTICE_LINEAR_API_KEY: ordinary
""",
    ],
)
def test_global_audit_detects_reusable_workflow_secret_mapping_keys(workflow: str):
    document = _workflow(workflow)

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_rejects_reusable_workflow_secret_inheritance():
    document = _workflow(
        """\
on: pull_request
jobs:
  reusable:
    uses: ./.github/workflows/reusable.yml
    secrets: inherit
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_allows_inherit_outside_reusable_job_secrets():
    document = _workflow(
        """\
on: push
jobs:
  ordinary:
    runs-on: ubuntu-latest
    steps:
      - uses: owner/action@0123456789abcdef0123456789abcdef01234567
        with:
          mode: inherit
      - run: echo inherit
"""
    )

    assert audit_global_workflows((document,)) == ()


def test_global_audit_allows_only_the_exact_canonical_linear_secret_slot():
    canonical = _workflow(
        """\
on: push
jobs:
  linear:
    runs-on: ubuntu-latest
    steps:
      - run: install
      - env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: doc-lattice linear --exit-code
""",
        ".github/workflows/doc-lattice-linear.yml",
    )
    duplicate = _workflow(
        """\
on: push
jobs:
  linear:
    runs-on: ubuntu-latest
    env:
      TOKEN: DOC_LATTICE_LINEAR_API_KEY
    steps:
      - run: install
      - env:
          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}
        run: doc-lattice linear --exit-code
""",
        ".github/workflows/doc-lattice-linear.yml",
    )

    assert audit_global_workflows((canonical,)) == ()
    assert _finding_codes(audit_global_workflows((duplicate,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize(
    "value",
    [
        "${{ secrets.linear_api_key }}",
        "${{ secrets['Doc_Lattice_Linear_Api_Key'] }}",
        "${{ toJSON(SeCrEtS.LINEAR_API_KEY) }}",
        "${{ secrets[env.linear_api_key] }}",
    ],
)
def test_global_audit_detects_case_insensitive_secret_references(value: str):
    document = _workflow(
        f"""\
on: push
env:
  TOKEN: {value}
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_fails_closed_on_computed_secret_key():
    document = _workflow(
        """\
on: issue_comment
jobs:
  unrelated:
    environment: doc-lattice-linear
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: ${{ secrets[format('DOC_LATTICE_LINEAR_API_{0}', 'KEY')] }}
        run: true
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize(
    "value",
    [
        "${{ toJSON(secrets) }}",
        "${{ secrets }}",
        "${{ secrets.* }}",
        "${{ secrets[*] }}",
        "${{ secrets.RELEASE_TOKEN.* }}",
        "${{ secrets['RELEASE_TOKEN'].* }}",
        "${{ secrets.RELEASE_TOKEN* }}",
        "${{ secrets",
    ],
    ids=[
        "function",
        "whole-context",
        "dot-wildcard",
        "index-wildcard",
        "chained-dot",
        "chained-index",
        "adjacent-wildcard",
        "unterminated",
    ],
)
def test_global_audit_rejects_unbounded_secret_context_access(value: str):
    document = _workflow(
        f"""\
on: push
jobs:
  unrelated:
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: {value}
        run: true
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


@pytest.mark.parametrize(
    "value",
    [
        "${{ secrets.RELEASE_TOKEN }}",
        "${{ secrets . RELEASE_TOKEN }}",
        "${{ secrets['RELEASE_TOKEN'] }}",
        "${{ 'secrets' }}",
        "${{ format('it''s secrets', secrets.RELEASE_TOKEN) }}",
    ],
    ids=["dot", "spaced-dot", "index", "quoted-literal", "escaped-quote"],
)
def test_global_audit_allows_static_unrelated_secret_access(value: str):
    document = _workflow(
        f"""\
on: push
jobs:
  unrelated:
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: {value}
        run: true
"""
    )

    assert audit_global_workflows((document,)) == ()


def test_global_audit_allows_static_unrelated_secret_index():
    document = _workflow(
        """\
on: push
jobs:
  unrelated:
    runs-on: ubuntu-latest
    steps:
      - env:
          TOKEN: ${{ secrets['RELEASE_TOKEN'] }}
        run: true
"""
    )

    assert audit_global_workflows((document,)) == ()


@pytest.mark.parametrize("key", ["LINEAR_API_KEY", "doc_lattice_linear_api_key"])
def test_global_audit_detects_root_environment_secret_keys(key: str):
    document = _workflow(
        f"""\
on: push
env:
  {key}: ordinary
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_rejects_case_variation_in_canonical_secret_slot():
    document = _workflow(
        """\
on: push
jobs:
  linear:
    runs-on: ubuntu-latest
    steps:
      - run: install
      - env:
          linear_api_key: ${{ secrets.doc_lattice_linear_api_key }}
        run: doc-lattice linear --exit-code
""",
        ".github/workflows/doc-lattice-linear.yml",
    )

    assert _finding_codes(audit_global_workflows((document,))) == {"LINEAR_SECRET_REFERENCE"}


def test_global_audit_deduplicates_identical_findings_with_stable_details():
    document = _workflow(
        """\
on: [pull_request, pull_request_target]
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: doc-lattice linear
""",
        ".github/workflows/duplicate.yml",
    )

    findings = audit_global_workflows((document, document))

    assert len(findings) == 2
    assert [(finding.path, finding.code, finding.message) for finding in findings] == [
        (
            ".github/workflows/duplicate.yml",
            "PR_LINEAR_INVOCATION",
            "pull-request workflows must not invoke doc-lattice linear",
        ),
        (
            ".github/workflows/duplicate.yml",
            "PULL_REQUEST_TARGET",
            "pull_request_target is prohibited for repository workflows",
        ),
    ]


def test_global_audit_documents_arbitrary_script_indirection_as_undetected():
    document = _workflow(
        """\
on: pull_request
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - run: ./scripts/run-doc-policy
"""
    )

    assert audit_global_workflows((document,)) == ()


@pytest.mark.parametrize(
    ("event", "github_ref", "environment_main_matches"),
    [
        ("pull_request", "refs/pull/17/merge", False),
        ("pull_request_review", "refs/pull/17/merge", False),
        ("pull_request_review_comment", "refs/pull/17/merge", False),
        ("pull_request_target", "refs/heads/main", True),
    ],
)
def test_documented_github_ref_security_model(
    event: str,
    github_ref: str,
    environment_main_matches: bool,
):
    assert (github_ref == "refs/heads/main") is environment_main_matches
    if event == "pull_request_target":
        workflow = parse_workflow(
            Path(".github/workflows/unsafe.yml"),
            "on: pull_request_target\njobs: {}\n",
        )
        assert _finding_codes(audit_global_workflows((workflow,))) == {"PULL_REQUEST_TARGET"}


def test_documented_prechange_head_ref_could_match_exact_main_policy():
    attacker_controlled_head_branch = "main"
    allowed_environment_branches = {"main"}

    assert attacker_controlled_head_branch in allowed_environment_branches


def _write_managed_artifacts(root: Path, repository: str = "Guardantix/doc-lattice") -> None:
    for artifact in render_managed_artifacts(repository, "2.1.0"):
        destination = root / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.text, encoding="utf-8")


def test_discover_workflows_returns_normal_absent_directory_state(tmp_path: Path):
    discovery = discover_workflows(tmp_path)

    assert discovery == WorkflowDiscovery(directory_exists=False, documents=())
    assert not (tmp_path / ".github").exists()


@pytest.mark.parametrize("kind", ["external", "internal", "broken"])
def test_discover_workflows_rejects_symlinked_github_parent_when_workflows_absent(
    tmp_path: Path,
    kind: str,
):
    root = tmp_path / "root"
    root.mkdir()
    if kind == "external":
        target = tmp_path / "outside"
        target.mkdir()
    elif kind == "internal":
        target = root / "internal"
        target.mkdir()
    else:
        target = root / "missing"
    (root / ".github").symlink_to(target, target_is_directory=True)

    with pytest.raises(ConfigError) as caught:
        discover_workflows(root)

    assert ".github/workflows" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_discover_workflows_reads_direct_yaml_files_in_stable_relative_order(
    tmp_path: Path,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "z.yaml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    (workflows / "a.yml").write_text("on: pull_request\njobs: {}\n", encoding="utf-8")
    (workflows / "ignored.txt").write_text("not yaml", encoding="utf-8")
    nested = workflows / "nested"
    nested.mkdir()
    (nested / "nested.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")

    discovery = discover_workflows(tmp_path)

    assert discovery.directory_exists is True
    assert [document.path for document in discovery.documents] == [
        Path(".github/workflows/a.yml"),
        Path(".github/workflows/z.yaml"),
    ]
    assert [trigger.name for document in discovery.documents for trigger in document.triggers] == [
        "pull_request",
        "push",
    ]


@pytest.mark.parametrize("filename", [".yml", ".yaml"])
def test_discover_workflows_reads_extension_only_yaml_filenames(
    tmp_path: Path,
    filename: str,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / filename).write_text("on: pull_request_target\njobs: {}\n", encoding="utf-8")

    discovery = discover_workflows(tmp_path)

    assert [document.path for document in discovery.documents] == [
        Path(f".github/workflows/{filename}"),
    ]
    assert [trigger.name for document in discovery.documents for trigger in document.triggers] == [
        "pull_request_target",
    ]


def test_discover_workflows_rejects_workflows_directory_file(tmp_path: Path):
    github = tmp_path / ".github"
    github.mkdir()
    (github / "workflows").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"workflow directory.*directory"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_external_workflows_directory_symlink(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "unsafe.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    root = tmp_path / "root"
    (root / ".github").mkdir(parents=True)
    (root / ".github/workflows").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match=r"symlink.*workflow directory"):
        discover_workflows(root)


def test_discover_workflows_rejects_symlinked_or_nonregular_yaml_files(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    real = tmp_path / "real.yml"
    real.write_text("on: push\njobs: {}\n", encoding="utf-8")
    (workflows / "linked.yml").symlink_to(real)

    with pytest.raises(ConfigError, match=r"symlink.*\.github/workflows/linked\.yml"):
        discover_workflows(tmp_path)

    (workflows / "linked.yml").unlink()
    os.mkfifo(workflows / "fifo.yml")

    with pytest.raises(ConfigError, match=r"regular file.*fifo\.yml"):
        discover_workflows(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized", "non_utf8"])
def test_discover_workflows_escapes_control_characters_in_candidate_diagnostics(
    tmp_path: Path,
    kind: str,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "bad\nname\x1b.yml"
    if kind == "symlink":
        source = tmp_path / "source.yml"
        source.write_text("on: push\njobs: {}\n", encoding="utf-8")
        target.symlink_to(source)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "oversized":
        target.write_bytes(b"x" * (MAX_WORKFLOW_BYTES + 1))
    else:
        target.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError) as caught:
        discover_workflows(tmp_path)

    message = str(caught.value)
    assert ".github/workflows/bad\\nname\\u001b.yml" in message
    assert "\n" not in message
    assert "\x1b" not in message


def test_discover_workflows_skips_subdirectory_entries(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "real.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
    # GitHub Actions ignores subdirectories, even ones named like a workflow file, so a
    # benign directory must not make the whole repository unauditable.
    (workflows / "templates.yml").mkdir()

    discovery = discover_workflows(tmp_path)

    assert discovery.directory_exists is True
    assert [document.path for document in discovery.documents] == [
        Path(".github/workflows/real.yml"),
    ]


def test_discover_workflows_rejects_non_utf8_or_malformed_yaml(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "bad.yml"
    target.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError, match=r"UTF-8.*\.github/workflows/bad\.yml"):
        discover_workflows(tmp_path)

    target.write_text("on: [", encoding="utf-8")

    with pytest.raises(
        ConfigError,
        match=r'cannot parse GitHub workflow "\.github/workflows/bad\.yml"',
    ):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_more_than_maximum_direct_files(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    for index in range(MAX_WORKFLOW_FILES + 1):
        (workflows / f"{index:03}.yml").write_text("jobs: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"more than 256.*\.github/workflows"):
        discover_workflows(tmp_path)


def test_discover_workflows_stops_enumerating_at_file_count_limit(
    tmp_path: Path,
    monkeypatch,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    real_iterdir = Path.iterdir

    def _bounded_entries(path: Path):
        if path != workflows:
            yield from real_iterdir(path)
            return
        for index in range(MAX_WORKFLOW_FILES + 1):
            yield path / f"{index:03}.yml"
        raise AssertionError("workflow discovery consumed past its declared file limit")

    monkeypatch.setattr(Path, "iterdir", _bounded_entries)

    with pytest.raises(ConfigError, match=r"more than 256.*\.github/workflows"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_per_file_byte_limit_plus_one(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "large.yml"
    target.write_bytes(b"jobs: {}\n#" + b"x" * (MAX_WORKFLOW_BYTES - 9))
    assert target.stat().st_size == MAX_WORKFLOW_BYTES + 1

    with pytest.raises(ConfigError, match=r"byte limit.*\.github/workflows/large\.yml"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_cumulative_byte_limit_plus_one(tmp_path: Path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    for index in range(8):
        target = workflows / f"{index}.yml"
        target.write_bytes(b"jobs: {}\n#" + b"x" * (MAX_WORKFLOW_BYTES - 10))
        assert target.stat().st_size == MAX_WORKFLOW_BYTES
    (workflows / "overflow.yml").write_bytes(b"x")
    assert sum(path.stat().st_size for path in workflows.iterdir()) == (
        MAX_CUMULATIVE_WORKFLOW_BYTES + 1
    )

    with pytest.raises(ConfigError, match=r"cumulative byte limit.*\.github/workflows"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_file_growth_between_stat_and_read(
    tmp_path: Path,
    monkeypatch,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    target = workflows / "growing.yml"
    target.write_text("jobs: {}\n", encoding="utf-8")
    real_open = Path.open
    grew = False

    def _grow_after_open(path: Path, *args, **kwargs):
        nonlocal grew
        handle = real_open(path, *args, **kwargs)
        if path == target and not grew and "r" in args[0]:
            grew = True
            with real_open(path, "ab") as writer:
                writer.write(b"# grew\n")
        return handle

    monkeypatch.setattr(Path, "open", _grow_after_open)

    with pytest.raises(ConfigError, match=r"changed during discovery.*growing\.yml"):
        discover_workflows(tmp_path)


def test_discover_workflows_rejects_same_size_replacement_after_open(
    tmp_path: Path,
    monkeypatch,
):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    unsafe = b"on: pull_request_target\njobs: {}\n"
    safe_prefix = b"on: push\njobs: {}\n"
    safe = safe_prefix + b"#" + (b"x" * (len(unsafe) - len(safe_prefix) - 2)) + b"\n"
    assert len(safe) == len(unsafe)
    target = workflows / "replaced.yml"
    target.write_bytes(safe)
    replacement = workflows / "replaced.pending"
    replacement.write_bytes(unsafe)
    real_open = Path.open
    replaced = False

    def _replace_after_open(path: Path, *args, **kwargs):
        nonlocal replaced
        handle = real_open(path, *args, **kwargs)
        if path == target and not replaced and "r" in args[0]:
            replaced = True
            replacement.replace(target)
        return handle

    monkeypatch.setattr(Path, "open", _replace_after_open)

    with pytest.raises(ConfigError, match=r"changed during discovery.*replaced\.yml"):
        discover_workflows(tmp_path)

    assert replaced is True
    assert target.read_bytes() == unsafe


def test_inspect_installed_artifacts_returns_exact_text_and_parsed_markers(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_managed_artifacts(tmp_path)

    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    assert all(isinstance(artifact, InstalledArtifact) for artifact in installed)
    assert [artifact.expected for artifact in installed if artifact is not None] == list(
        CANONICAL_ARTIFACT_TARGETS
    )
    assert [artifact.text for artifact in installed if artifact is not None] == [
        artifact.text for artifact in expected
    ]
    assert [artifact.marker.role for artifact in installed if artifact and artifact.marker] == [
        "offline",
        "linear",
        "bootstrap",
        "attributes",
    ]
    assert all(artifact.marker_error is None for artifact in installed if artifact)


def test_inspect_installed_artifacts_preserves_missing_positions_without_mutation(
    tmp_path: Path,
):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    offline = expected[0]
    destination = tmp_path / offline.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text(offline.text, encoding="utf-8")

    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    assert installed[0] is not None
    assert installed[1:] == (None, None, None)
    assert not (tmp_path / ".github/doc-lattice-bootstrap.sh").exists()


def test_inspect_installed_artifacts_reports_bad_marker_as_data(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    bootstrap = expected[2]
    destination = tmp_path / bootstrap.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text(
        bootstrap.text.replace(
            "# doc-lattice-managed: github-ci-v1",
            "# doc-lattice-managed: broken",
            1,
        ),
        encoding="utf-8",
    )

    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    assert installed[:2] == (None, None)
    assert installed[2] is not None
    assert installed[2].marker is None
    assert "invalid ownership marker" in (installed[2].marker_error or "")


def test_inspect_installed_artifacts_rejects_non_utf8_and_symlinks(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    offline = expected[0]
    destination = tmp_path / offline.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigError, match=r"UTF-8.*doc-lattice\.yml"):
        inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    destination.unlink()
    real = tmp_path / "real.yml"
    real.write_text(offline.text, encoding="utf-8")
    destination.symlink_to(real)

    with pytest.raises(ConfigError, match=r"symlink.*doc-lattice\.yml"):
        inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)


def test_inspect_installed_artifacts_rejects_oversized_managed_file(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    offline = expected[0]
    destination = tmp_path / offline.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"x" * (MAX_WORKFLOW_BYTES + 1))

    with pytest.raises(ConfigError, match=r"byte limit.*doc-lattice\.yml"):
        inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)


def test_inspect_external_parent_error_uses_only_repository_relative_path(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".github").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError) as caught:
        inspect_installed_artifacts(root, CANONICAL_ARTIFACT_TARGETS)

    assert ".github/workflows/doc-lattice.yml" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_inspection_never_yaml_parses_bootstrap(tmp_path: Path):
    expected = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    bootstrap = expected[2]
    destination = tmp_path / bootstrap.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_text(bootstrap.text + "\nnot: [valid YAML\n", encoding="utf-8")

    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    assert installed[2] is not None
    assert installed[2].marker is not None


def _audit_installed(
    root: Path,
    *,
    expected_repository: str = "Guardantix/doc-lattice",
    running_version: str = "2.1.0",
):
    discovery = discover_workflows(root)
    installed = inspect_installed_artifacts(root, CANONICAL_ARTIFACT_TARGETS)
    findings = audit_managed_installation(
        discovery,
        installed,
        parse_repository(expected_repository),
        running_version,
    )
    return findings


def _mutate_artifact(
    root: Path,
    role: str,
    old: str,
    new: str,
) -> None:
    artifact = next(
        artifact
        for artifact in render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
        if artifact.role == role
    )
    destination = root / artifact.relative_path
    text = destination.read_text(encoding="utf-8")
    assert old in text
    destination.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_managed_audit_uses_inspected_canonical_workflow_snapshot(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    discovery = discover_workflows(tmp_path)
    _mutate_artifact(
        tmp_path,
        "offline",
        "on:\n  push:",
        "on:\n  pull_request_target:\n  push:",
    )
    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    findings = audit_managed_installation(
        discovery,
        installed,
        parse_repository("Guardantix/doc-lattice"),
        "2.1.0",
    )

    assert _finding_codes(findings) == {"MANAGED_TRIGGERS"}


def test_managed_audit_accepts_exact_rendered_installation(tmp_path: Path):
    _write_managed_artifacts(tmp_path)

    assert _audit_installed(tmp_path) == ()


def test_managed_audit_rejects_weakened_bootstrap_lf_rule(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "attributes",
        "doc-lattice-bootstrap.sh text eol=lf",
        "doc-lattice-bootstrap.sh text",
    )

    assert _finding_codes(_audit_installed(tmp_path)) == {"MANAGED_ATTRIBUTES"}


def test_managed_audit_accepts_crlf_attributes_file_with_exact_rule(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    attributes = next(
        artifact
        for artifact in render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
        if artifact.role == "attributes"
    )
    destination = tmp_path / attributes.relative_path
    destination.write_bytes(attributes.text.replace("\n", "\r\n").encode("utf-8"))

    assert _audit_installed(tmp_path) == ()


def test_audit_repository_merges_global_and_managed_findings_sorted_unique(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", "contents: read", "contents: write")
    unsafe = tmp_path / ".github/workflows/unsafe.yml"
    unsafe.write_text("on: pull_request_target\njobs: {}\n", encoding="utf-8")
    discovery = discover_workflows(tmp_path)
    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    findings = audit_repository(
        discovery,
        installed,
        parse_repository("Guardantix/doc-lattice"),
        "2.1.0",
    )

    assert _finding_codes(findings) == {"PULL_REQUEST_TARGET", "MANAGED_PERMISSIONS"}
    assert findings == tuple(sorted(set(findings)))


def test_managed_audit_renders_expected_documents_once_per_call(tmp_path: Path, monkeypatch):
    from doc_lattice.github_ci import audit as audit_module  # noqa: PLC0415

    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", "contents: read", "contents: write")
    _mutate_artifact(tmp_path, "linear", "contents: read", "contents: write")
    render_calls: list[tuple[str, str]] = []
    real_render_workflows = audit_module.render_workflows

    def tracked_render_workflows(repository: str, version: str):
        render_calls.append((repository, version))
        return real_render_workflows(repository, version)

    monkeypatch.setattr(audit_module, "render_workflows", tracked_render_workflows)

    findings = _audit_installed(tmp_path)

    # Both managed workflows drift, but their expected documents share one (repository, version)
    # so the render happens exactly once for the whole audit call.
    assert _finding_codes(findings) == {"MANAGED_PERMISSIONS"}
    assert render_calls == [("Guardantix/doc-lattice", "2.1.0")]


def test_managed_audit_requires_exactly_four_canonical_inspection_slots(tmp_path: Path):
    discovery = discover_workflows(tmp_path)
    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    with pytest.raises(ConfigError, match="exactly four"):
        audit_managed_installation(
            discovery,
            installed[:2],
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_managed_audit_rejects_present_artifacts_out_of_canonical_order(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    discovery = discover_workflows(tmp_path)
    installed = inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    with pytest.raises(ConfigError, match="canonical order"):
        audit_managed_installation(
            discovery,
            tuple(reversed(installed)),
            parse_repository("Guardantix/doc-lattice"),
            "2.1.0",
        )


def test_managed_audit_reports_absent_directory_and_artifacts_as_findings(tmp_path: Path):
    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {
        "MISSING_WORKFLOW_DIRECTORY",
        "MISSING_MANAGED_ARTIFACT",
    }
    assert not (tmp_path / ".github").exists()


@pytest.mark.parametrize("missing_role", ["offline", "linear", "bootstrap", "attributes"])
def test_managed_audit_reports_each_missing_canonical_artifact(
    tmp_path: Path,
    missing_role: str,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_managed_artifacts(tmp_path)
    missing = next(artifact for artifact in artifacts if artifact.role == missing_role)
    (tmp_path / missing.relative_path).unlink()

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MISSING_MANAGED_ARTIFACT"}
    assert {finding.path for finding in findings} == {missing.relative_path.as_posix()}


def test_managed_audit_reports_stale_generator_without_current_version_cascade(
    tmp_path: Path,
):
    _write_managed_artifacts(tmp_path)
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    for artifact in old_artifacts:
        destination = tmp_path / artifact.relative_path
        destination.write_text(artifact.text, encoding="utf-8")

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"STALE_GENERATOR"}
    assert all("ci refresh" in finding.message for finding in findings)


def test_managed_audit_stale_generator_advises_upgrade_for_newer_marker(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    newer_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.2.0")
    for artifact in newer_artifacts:
        destination = tmp_path / artifact.relative_path
        destination.write_text(artifact.text, encoding="utf-8")

    findings = _audit_installed(tmp_path, running_version="2.1.0")

    assert _finding_codes(findings) == {"STALE_GENERATOR"}
    # A newer installed marker cannot be reached by `ci refresh`, which refuses the
    # downgrade, so the advice must direct the maintainer to upgrade instead.
    assert all("upgrade your local doc-lattice to at least 2.2.0" in f.message for f in findings)
    assert all("ci refresh" not in finding.message for finding in findings)


@pytest.mark.parametrize(
    "running_version",
    ["2.2.0.dev1", "2.2.0rc1", "2.1.0+local"],
)
def test_managed_audit_stale_marker_skips_semantic_comparison(
    tmp_path: Path,
    monkeypatch,
    running_version: str,
):
    from doc_lattice.github_ci import audit as audit_module  # noqa: PLC0415

    _write_managed_artifacts(tmp_path)
    rendered_versions: list[str] = []
    real_render_workflows = audit_module.render_workflows

    def track_render_workflows(repository: str, version: str):
        rendered_versions.append(version)
        return real_render_workflows(repository, version)

    monkeypatch.setattr(audit_module, "render_workflows", track_render_workflows)

    findings = _audit_installed(tmp_path, running_version=running_version)

    # The installed marker (2.1.0) differs from the running version, so STALE_GENERATOR is
    # the only actionable finding and no chimera expected document is rendered.
    assert _finding_codes(findings) == {"STALE_GENERATOR"}
    assert rendered_versions == []


def test_managed_audit_byte_canonical_other_release_reports_only_stale_generator(
    tmp_path: Path,
    monkeypatch,
):
    from doc_lattice.github_ci import audit as audit_module  # noqa: PLC0415

    # A byte-canonical install produced by a different release whose templates differ must
    # not gain spurious managed-drift findings from a chimera expected document. When the
    # marker version differs from the running version the semantic comparison is skipped
    # entirely, so render_workflows must never be called.
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    for artifact in old_artifacts:
        destination = tmp_path / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.text, encoding="utf-8")

    def fail_render(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("semantic comparison must be skipped on a version mismatch")

    monkeypatch.setattr(audit_module, "render_workflows", fail_render)

    findings = _audit_installed(tmp_path, running_version="2.1.0")

    assert _finding_codes(findings) == {"STALE_GENERATOR"}


def test_managed_audit_reports_invalid_bootstrap_marker_as_finding(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "bootstrap",
        "# doc-lattice-managed: github-ci-v1",
        "# doc-lattice-managed: broken",
    )

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MANAGED_MARKER"}
    assert findings[0].path == ".github/doc-lattice-bootstrap.sh"


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("branches: [main]", "branches: [develop]", "MANAGED_TRIGGERS"),
        ("contents: read", "contents: write", "MANAGED_PERMISSIONS"),
        (
            f"actions/checkout@{CHECKOUT_REF}",
            "actions/checkout@v4",
            "MANAGED_ACTION",
        ),
        ("persist-credentials: false", "persist-credentials: true", "MANAGED_CHECKOUT"),
        ("enable-cache: false", "enable-cache: true", "MANAGED_CACHE"),
        ("doc-lattice check", "doc-lattice check-changed", "MANAGED_COMMAND"),
    ],
)
def test_managed_audit_reports_focused_offline_drift(
    tmp_path: Path,
    old: str,
    new: str,
    code: str,
):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", old, new)

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {code}
    assert {finding.path for finding in findings} == {".github/workflows/doc-lattice.yml"}


def test_managed_audit_rejects_actions_cache_in_managed_offline_workflow(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "offline",
        "      - name: Audit, check, and lint\n",
        "      - uses: actions/cache@v4\n"
        "        with:\n"
        "          path: .cache\n"
        "      - name: Audit, check, and lint\n",
    )

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MANAGED_CACHE"}


def test_managed_audit_detects_action_moved_after_command_step(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    offline = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[0]
    destination = tmp_path / offline.relative_path
    text = destination.read_text(encoding="utf-8")
    setup_uv = (
        f"      - uses: astral-sh/setup-uv@{SETUP_UV_REF} # v6.8.0\n"
        "        with:\n"
        "          enable-cache: false\n"
    )
    assert setup_uv in text
    destination.write_text(
        text.replace(setup_uv, "", 1).rstrip() + "\n" + setup_uv,
        encoding="utf-8",
    )

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {"MANAGED_ACTION"}


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("jobs:\n  linear:", "jobs:\n  trusted:", "MANAGED_JOB"),
        (
            "github.repository == 'Guardantix/doc-lattice'",
            "github.repository == 'other/repository'",
            "MANAGED_COMMAND",
        ),
        (
            "environment: doc-lattice-linear",
            "environment: production",
            "MANAGED_JOB",
        ),
        (
            "LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}",
            "TOKEN: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}",
            "MANAGED_SECRET",
        ),
        (
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" linear --exit-code',
            '"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" lint',
            "MANAGED_COMMAND",
        ),
        (
            "uv python install 3.13\n"
            '          uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"',
            'uv venv --python 3.13 "$RUNNER_TEMP/doc-lattice-venv"\n'
            "          uv python install 3.13",
            "MANAGED_COMMAND",
        ),
    ],
)
def test_managed_audit_reports_focused_linear_drift(
    tmp_path: Path,
    old: str,
    new: str,
    code: str,
):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "linear", old, new)

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {code}
    assert {finding.path for finding in findings} == {".github/workflows/doc-lattice-linear.yml"}


def test_managed_audit_requires_linear_secret_only_on_final_step(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(
        tmp_path,
        "linear",
        "      - name: Run trusted Linear gate\n"
        "        env:\n"
        "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
        "        run:",
        "      - env:\n"
        "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
        "        run: echo early\n"
        "      - name: Run trusted Linear gate\n"
        "        run:",
    )

    findings = _audit_installed(tmp_path)

    assert "MANAGED_SECRET" in _finding_codes(findings)


def test_managed_audit_accepts_ascii_case_only_repository_identity_change(tmp_path: Path):
    _write_managed_artifacts(tmp_path, "guardantix/DOC-LATTICE")

    findings = _audit_installed(
        tmp_path,
        expected_repository="Guardantix/doc-lattice",
    )

    assert findings == ()


def test_managed_audit_reports_repository_rename_without_semantic_cascade(tmp_path: Path):
    _write_managed_artifacts(tmp_path, "FormerOwner/former-repository")

    findings = _audit_installed(
        tmp_path,
        expected_repository="Guardantix/doc-lattice",
    )

    assert _finding_codes(findings) == {"REPOSITORY_IDENTITY"}


def test_managed_audit_ignores_unrelated_workflow_permissions(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    release = tmp_path / ".github/workflows/release.yml"
    release.write_text(
        """\
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
""",
        encoding="utf-8",
    )

    assert _audit_installed(tmp_path) == ()


def test_managed_audit_findings_are_sorted_and_unique(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", "contents: read", "contents: write")
    _mutate_artifact(tmp_path, "linear", "contents: read", "contents: write")

    findings = _audit_installed(tmp_path)

    assert findings == tuple(sorted(set(findings)))


@pytest.mark.parametrize(
    ("role", "old", "new", "code"),
    [
        (
            "offline",
            "  pull_request:\n    branches: [main]",
            "  pull_request:\n    branches: [main]\n    paths: [docs/**]",
            "MANAGED_TRIGGERS",
        ),
        (
            "offline",
            "  pull_request:\n    branches: [main]",
            "  pull_request:\n    branches: [main]\n    paths-ignore: [generated/**]",
            "MANAGED_TRIGGERS",
        ),
        (
            "offline",
            "permissions:\n  contents: read",
            "env:\n"
            "  UV_DEFAULT_INDEX: https://example.invalid/simple\n"
            "permissions:\n"
            "  contents: read",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "permissions:\n  contents: read",
            "defaults:\n"
            "  run:\n"
            "    shell: bash\n"
            "    working-directory: docs\n"
            "permissions:\n"
            "  contents: read",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "      - name: Audit, check, and lint\n        run:",
            "      - name: Audit, check, and lint\n        if: false\n"
            "        continue-on-error: true\n        shell: bash\n"
            "        working-directory: docs\n        run:",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "    runs-on: ubuntu-latest",
            "    if: false\n    continue-on-error: true\n    runs-on: ubuntu-latest",
            "MANAGED_COMMAND",
        ),
        (
            "offline",
            "      - name: Audit, check, and lint",
            "      - uses: owner/extra-action@main\n      - name: Audit, check, and lint",
            "MANAGED_ACTION",
        ),
        (
            "offline",
            "      - name: Audit, check, and lint",
            "      - run: echo extra\n      - name: Audit, check, and lint",
            "MANAGED_COMMAND",
        ),
        (
            "linear",
            "      - name: Run trusted Linear gate\n"
            "        env:\n"
            "          LINEAR_API_KEY: ${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}\n"
            '        run: \'"$RUNNER_TEMP/doc-lattice-venv/bin/doc-lattice" '
            "linear --exit-code'\n",
            "",
            "MANAGED_COMMAND",
        ),
    ],
)
def test_managed_audit_detects_residual_behavioral_structure(
    tmp_path: Path,
    role: str,
    old: str,
    new: str,
    code: str,
):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, role, old, new)

    findings = _audit_installed(tmp_path)

    assert _finding_codes(findings) == {code}


def test_managed_audit_allows_display_name_changes(tmp_path: Path):
    _write_managed_artifacts(tmp_path)
    _mutate_artifact(tmp_path, "offline", "name: doc-lattice", "name: Friendly workflow")
    _mutate_artifact(
        tmp_path,
        "offline",
        "name: Offline doc-lattice gates",
        "name: Friendly job",
    )
    _mutate_artifact(
        tmp_path,
        "offline",
        "name: Audit, check, and lint",
        "name: Friendly step",
    )

    assert _audit_installed(tmp_path) == ()
