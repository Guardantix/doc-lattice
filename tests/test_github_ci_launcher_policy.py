"""Tests for the doc-lattice launcher and option policy over the shared word IR."""

from doc_lattice.github_ci.launcher_policy import ScanWord, resolve_command


def words(*specs):
    """Build a ScanWord tuple from (text, unstable) pairs with synthetic offsets."""
    result = []
    offset = 0
    for text, unstable in specs:
        result.append(ScanWord(text, offset, offset + len(text), unstable))
        offset += len(text) + 1
    return tuple(result)


def lit(*texts):
    return words(*[(text, False) for text in texts])


def test_direct_invocations_resolve():
    assert resolve_command(lit("doc-lattice", "check")).invocation == ("check", False)
    assert resolve_command(lit("doc-lattice", "lint")).invocation == ("lint", False)
    assert resolve_command(lit("doc-lattice", "linear")).invocation == ("linear", False)
    assert resolve_command(
        lit("doc-lattice", "ci", "audit", "--repository", "OWNER/REPO")
    ).invocation == ("ci", False)


def test_path_basename_resolves():
    assert resolve_command(lit("/usr/local/bin/doc-lattice", "check")).invocation == (
        "check",
        False,
    )


def test_non_candidate_commands():
    assert resolve_command(lit("exit", "1")).kind == "not_candidate"
    assert resolve_command(lit("uv", "sync")).kind == "not_candidate"
    assert resolve_command(lit("echo", "doc-lattice")).kind == "not_candidate"


def test_launcher_forms_resolve():
    assert resolve_command(
        lit(
            "uvx",
            "--python",
            "3.13",
            "--from",
            "doc-lattice==2.0.0",
            "doc-lattice",
            "ci",
            "audit",
            "--repository",
            "OWNER/REPO",
        )
    ).invocation == ("ci", False)
    assert resolve_command(lit("uv", "run", "--no-sync", "doc-lattice", "check")).invocation == (
        "check",
        False,
    )
    assert resolve_command(lit("uv", "tool", "run", "doc-lattice", "check")).invocation == (
        "check",
        False,
    )
    assert resolve_command(
        lit("uvx", "--from", "doc-lattice", "--with", "pyodide-build", "doc-lattice", "check")
    ).invocation == ("check", False)
    assert resolve_command(lit("uvx", "doc-lattice", "check")).invocation == ("check", False)


def test_root_options():
    assert resolve_command(lit("doc-lattice", "--no-color", "check")).invocation == (
        "check",
        False,
    )
    helped = resolve_command(lit("uv", "run", "doc-lattice", "--help"))
    assert helped.kind == "resolved"
    assert helped.invocation is None
    versioned = resolve_command(lit("doc-lattice", "--version"))
    assert versioned.kind == "resolved"
    assert versioned.invocation is None


def test_dry_run_extraction():
    assert resolve_command(lit("doc-lattice", "reconcile", "--dry-run")).invocation == (
        "reconcile",
        True,
    )
    assert resolve_command(lit("doc-lattice", "reconcile")).invocation == (
        "reconcile",
        False,
    )
    quoted = resolve_command(lit("doc-lattice", "reconcile", "pc-design", "--dry-run"))
    assert quoted.invocation == ("reconcile", True)


def test_unstable_after_subcommand_terminates_retention():
    resolution = resolve_command(
        words(
            ("doc-lattice", False),
            ("reconcile", False),
            ("pc-design", False),
            ("$OPTION", True),
            ("--dry-run", False),
        )
    )
    assert resolution.invocation == ("reconcile", False)


def test_unstable_before_subcommand_refuses():
    resolution = resolve_command(words(("doc-lattice", False), ("$X", True), ("check", False)))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"
    assert resolution.offset == 12


def test_unknown_subcommand_refuses():
    resolution = resolve_command(lit("doc-lattice", "frobnicate"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_unknown_launcher_option_refuses():
    resolution = resolve_command(lit("uvx", "--quiet", "doc-lattice", "check"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_dynamic_payload_before_established_refuses():
    resolution = resolve_command(words(("uvx", False), ("$PKG", True), ("check", False)))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_empty_quoted_word_never_matches():
    resolution = resolve_command(words(("", False), ("check", False)))
    assert resolution.kind == "not_candidate"


def test_distribution_spelling_variants_resolve():
    assert resolve_command(
        lit("uvx", "--from", "doc_lattice==2.0.0", "doc-lattice", "check")
    ).invocation == ("check", False)


def test_uv_dynamic_selector_refuses():
    resolution = resolve_command(
        words(("uv", False), ("$OPT", True), ("doc-lattice", False), ("linear", False))
    )
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"
    assert resolution.offset == 3


def test_uv_tool_dynamic_selector_refuses():
    resolution = resolve_command(
        words(
            ("uv", False),
            ("tool", False),
            ("$X", True),
            ("doc-lattice", False),
            ("check", False),
        )
    )
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"
    assert resolution.offset == 8


def test_path_form_launcher_refuses():
    for launcher in ("/usr/bin/uvx", "/usr/bin/uv"):
        resolution = resolve_command(
            words((launcher, False), ("doc-lattice", False), ("check", False))
        )
        assert resolution.kind == "refused"
        assert resolution.reason_category == "policy-unresolvable"
        assert resolution.offset == 0


def test_wrapper_head_refuses():
    for wrapper in ("command", "exec", "builtin", "env"):
        resolution = resolve_command(lit(wrapper, "doc-lattice", "linear"))
        assert resolution.kind == "refused", wrapper
        assert resolution.reason_category == "policy-unresolvable", wrapper
        assert resolution.offset == 0, wrapper


def test_echo_doc_lattice_stays_not_candidate():
    assert resolve_command(lit("echo", "doc-lattice")).kind == "not_candidate"


def test_uv_global_option_before_selector_refuses():
    for option in ("-q", "--no-cache", "--directory", "--frobnicate"):
        resolution = resolve_command(lit("uv", option, "run", "doc-lattice", "linear"))
        assert resolution.kind == "refused", option
        assert resolution.reason_category == "policy-unresolvable", option
        assert resolution.offset == 3, option
    tool = resolve_command(lit("uv", "--frobnicate", "tool", "run", "doc-lattice", "linear"))
    assert tool.kind == "refused"
    assert tool.offset == 3


def test_uv_sync_stays_not_candidate():
    assert resolve_command(lit("uv", "sync")).kind == "not_candidate"


def test_uv_tool_option_before_run_selector_refuses():
    # uv accepts options between tool and run (uv tool -q run dispatches to uv tool run), so a
    # stable option-like selector fails closed instead of dropping the doc-lattice payload.
    for option in ("-q", "--offline", "--no-cache", "--frobnicate"):
        resolution = resolve_command(lit("uv", "tool", option, "run", "doc-lattice", "linear"))
        assert resolution.kind == "refused", option
        assert resolution.reason_category == "policy-unresolvable", option
        assert resolution.offset == 8, option


def test_uv_tool_non_run_subcommand_stays_not_candidate():
    assert resolve_command(lit("uv", "tool", "install", "doc-lattice")).kind == "not_candidate"
    assert resolve_command(lit("uv", "tool", "list")).kind == "not_candidate"


def test_uv_run_payload_is_command_form_no_version_strip():
    # uv run launches a literal command, so a versioned payload does not normalize to
    # doc-lattice under the package rules; it still resembles doc-lattice, so the floor fails
    # closed on it rather than resolving the version-stripped launch that uv tool run would.
    resolution = resolve_command(lit("uv", "run", "doc-lattice@2.0.0", "linear"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_uv_tool_run_payload_is_package_form_version_strip():
    # uv tool run (like uvx) treats the payload as a package spec, so a versioned spelling
    # resolves to the doc-lattice launch.
    assert resolve_command(lit("uv", "tool", "run", "doc-lattice@2.0.0", "linear")).invocation == (
        "linear",
        False,
    )
    assert resolve_command(lit("uvx", "doc-lattice@latest", "reconcile")).invocation == (
        "reconcile",
        False,
    )


def test_wrapper_payload_after_launcher_refuses():
    for payload in ("env", "time", "/usr/bin/time", "uvx"):
        resolution = resolve_command(lit("uv", "run", payload, "doc-lattice", "linear"))
        assert resolution.kind == "refused", payload
        assert resolution.reason_category == "policy-unresolvable", payload


def test_lookalike_payload_after_launcher_refuses():
    # A quoted payload carrying surrounding whitespace or embedded text resembles doc-lattice but
    # does not normalize cleanly as a distribution, so the floor fails closed.
    for payload in ("doc-lattice ", "doc-lattice (>=2)", "doc.lattice @ url"):
        resolution = resolve_command(words(("uvx", False), (payload, False), ("linear", False)))
        assert resolution.kind == "refused", payload
        assert resolution.reason_category == "policy-unresolvable", payload


def test_distinct_tool_payload_stays_not_candidate():
    assert resolve_command(lit("uv", "run", "pytest", "doc-lattice-tests")).kind == "not_candidate"


def test_path_form_and_quoted_dispatch_head_refuses():
    for head in ("/usr/bin/time", "/usr/bin/env", "time"):
        resolution = resolve_command(lit(head, "doc-lattice", "linear"))
        assert resolution.kind == "refused", head
        assert resolution.reason_category == "policy-unresolvable", head
        assert resolution.offset == 0, head


def test_trailing_help_after_subcommand_refuses():
    for command in (
        lit("doc-lattice", "linear", "--help"),
        lit("doc-lattice", "reconcile", "--version"),
        lit("doc-lattice", "reconcile", "pc-design", "--format", "human", "--help"),
    ):
        resolution = resolve_command(command)
        assert resolution.kind == "refused"
        assert resolution.reason_category == "policy-unresolvable"


def test_trailing_help_after_terminator_or_unstable_resolves():
    assert resolve_command(lit("doc-lattice", "linear", "--", "--help")).invocation == (
        "linear",
        False,
    )
    assert resolve_command(lit("doc-lattice", "reconcile", "--", "--help")).invocation == (
        "reconcile",
        False,
    )
    unstable = resolve_command(
        words(("doc-lattice", False), ("linear", False), ("$X", True), ("--help", False))
    )
    assert unstable.invocation == ("linear", False)


def test_non_reconcile_dry_run_refuses():
    resolution = resolve_command(lit("doc-lattice", "check", "--dry-run"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"


def test_reconcile_dry_run_after_double_dash_not_credited():
    assert resolve_command(
        lit("doc-lattice", "reconcile", "pc-design", "--", "--dry-run")
    ).invocation == ("reconcile", False)


def test_reconcile_dry_run_after_bare_value_option_refuses():
    for option in ("--config", "--ref", "--format"):
        resolution = resolve_command(
            lit("doc-lattice", "reconcile", "pc-design", option, "--dry-run")
        )
        assert resolution.kind == "refused", option
        assert resolution.reason_category == "policy-unresolvable", option


def test_reconcile_dry_run_after_attached_value_option_credited():
    assert resolve_command(
        lit("doc-lattice", "reconcile", "pc-design", "--config=cfg.yml", "--dry-run")
    ).invocation == ("reconcile", True)


def test_exe_executable_head_resolves():
    # A .exe launcher shim resolves like a bare doc-lattice head, matching the runtime scanner's
    # _is_doc_lattice_executable_basename (shell_scanner.py:2961).
    assert resolve_command(lit(".venv/Scripts/doc-lattice.exe", "linear")).invocation == (
        "linear",
        False,
    )


def test_exe_executable_head_is_casefolded():
    assert resolve_command(lit("DOC-LATTICE.EXE", "check")).invocation == ("check", False)


def test_bare_exe_head_resolves_with_no_invocation():
    resolution = resolve_command(lit("doc-lattice.exe"))
    assert resolution.kind == "resolved"
    assert resolution.invocation is None


def test_uv_run_from_option_refuses():
    # uv run does not accept --from (uvx does); the old scanner reports an unresolved uv launcher
    # option there, so the floor refuses it before the payload rather than certifying a
    # marker-bearing source with no invocation. --from starts at offset 7 under the lit() helper.
    resolution = resolve_command(lit("uv", "run", "--from", "doc-lattice", "check"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"
    assert resolution.offset == 7


def test_uv_run_from_attached_value_refuses():
    resolution = resolve_command(lit("uv", "run", "--from=doc-lattice", "check"))
    assert resolution.kind == "refused"
    assert resolution.reason_category == "policy-unresolvable"
    assert resolution.offset == 7


def test_uv_tool_run_from_option_still_resolves():
    # uv tool run is a package-form launcher, so it keeps --from and resolves the payload.
    assert resolve_command(
        lit("uv", "tool", "run", "--from", "doc-lattice", "doc-lattice", "check")
    ).invocation == ("check", False)


def test_uv_run_shared_options_still_resolve():
    # --with and --python stay recognized for uv run; only --from is form-restricted.
    assert resolve_command(
        lit("uv", "run", "--with", "pyodide-build", "doc-lattice", "check")
    ).invocation == ("check", False)
    assert resolve_command(
        lit("uv", "run", "--python", "3.13", "doc-lattice", "check")
    ).invocation == ("check", False)


def test_package_form_no_sync_refuses():
    # Only uv run accepts --no-sync; uv rejects it for uvx and uv tool run with an
    # unexpected-argument error before any dispatch, so a package-form --no-sync must not
    # certify a launch that never runs. Under lit(), --no-sync starts at offset 4 after uvx
    # and at offset 12 after uv tool run.
    for command, offset in (
        (lit("uvx", "--no-sync", "doc-lattice", "linear"), 4),
        (lit("uv", "tool", "run", "--no-sync", "doc-lattice", "linear"), 12),
    ):
        resolution = resolve_command(command)
        assert resolution.kind == "refused"
        assert resolution.reason_category == "policy-unresolvable"
        assert resolution.offset == offset


def test_uv_run_no_sync_still_resolves():
    # --no-sync stays recognized for the command-form launcher.
    assert resolve_command(lit("uv", "run", "--no-sync", "doc-lattice", "linear")).invocation == (
        "linear",
        False,
    )
