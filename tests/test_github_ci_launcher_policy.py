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
