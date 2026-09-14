"""CLI integration tests for the links command."""

import ast
import builtins
import errno
import io
import os
from collections.abc import Sequence
from io import StringIO
from pathlib import Path

import pytest
from link_gate_helpers import _requires_permission_enforcement, _write
from markdown_it import MarkdownIt
from rich.console import Console

from doc_lattice.cli import app
from doc_lattice.cli.application import create_app
from doc_lattice.cli.pipe_policy import PipeClosed
from doc_lattice.cli.runtime import CliConsole, CliRuntime, RuntimeFactory
from doc_lattice.config import load_config
from doc_lattice.link_check import select_link_sources
from doc_lattice.orchestrate import load_lattice

from .helpers import _RefusingStream, runner


def _flow(values: Sequence[str]) -> str:
    """Spell a selector list as the YAML flow sequence a config file carries."""
    return "[" + ", ".join(f"'{value}'" for value in values) + "]"


def _config(root: Path, *selectors: str) -> None:
    _write(root, ".doc-lattice.yml", f"lattice_format: 2\nlink_sources: {_flow(selectors)}\n")


def _witness(root: Path) -> None:
    """One dead fragment and one dead relative path, in one source."""
    _config(root, "*.md")
    _write(root, "README.md", "# Readme\n\n[a](GUIDE.md#nope)\n\n[b](MISSING.md)\n")
    _write(root, "GUIDE.md", "# Guide\n")


def _runtime(stdout: Console, stderr: Console, cwd: Path) -> CliRuntime:
    return CliRuntime(
        stdout=stdout, stderr=stderr, cwd=cwd, load_config=load_config, load_lattice=load_lattice
    )


def _fixed_runtime(stdout: Console, stderr: Console, cwd: Path) -> RuntimeFactory:
    """Hand every invocation the same pre-built runtime, ignoring the no-color lever.

    The consoles are constructed by the caller so one of them can refuse its writes, which is
    what these pipe cases are about; the lever has nothing left to configure.
    """

    def factory(*, no_color: bool) -> CliRuntime:
        del no_color
        return _runtime(stdout, stderr, cwd)

    return factory


def test_links_fails_with_both_witness_messages_on_stderr(tmp_path: Path, monkeypatch):
    _witness(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        "'README.md':3: fragment '#nope' matches no heading in 'GUIDE.md'\n"
        "'README.md':5: link target 'MISSING.md' does not exist\n"
    )


def test_links_is_silent_and_exits_0_when_clean(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md")
    _write(tmp_path, "README.md", "# Readme\n\n[g](GUIDE.md#guide)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


def test_links_honors_a_configured_set_that_is_not_the_root(tmp_path: Path, monkeypatch):
    # The consumer-shaped fixture: sources live under spec/, and the root README carries a dead
    # link nobody asked the gate to check. The old hardcoded root selection is gone.
    _config(tmp_path, "spec/**/*.md")
    _write(tmp_path, "README.md", "# Readme\n\n[dead](MISSING.md)\n")
    _write(tmp_path, "spec/a.md", "# A\n\n[b](deep/b.md#b)\n")
    _write(tmp_path, "spec/deep/b.md", "# B\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["links"]).exit_code == 0

    _write(tmp_path, "spec/deep/b.md", "# Renamed\n")
    result = runner.invoke(app, ["links"])
    assert result.exit_code == 1
    assert result.stderr == "'spec/a.md':3: fragment '#b' matches no heading in 'spec/deep/b.md'\n"


def test_links_selects_sources_from_the_explicit_config_not_the_working_directory(
    tmp_path: Path, monkeypatch
):
    # Forwarding --config and rooting selection at that config's parent are separate steps, so
    # both projects declare the same '*.md' selector and only the explicit one carries a broken
    # link: the finding names the source that was read, which is what says where the run rooted.
    default_root = tmp_path / "default"
    explicit_root = tmp_path / "explicit"
    _config(default_root, "*.md")
    _write(default_root, "README.md", "# Readme\n")
    _config(explicit_root, "*.md")
    _write(explicit_root, "EXPLICIT.md", "# Explicit\n\n[a](ONLY-IN-EXPLICIT.md)\n")
    monkeypatch.chdir(default_root)

    assert runner.invoke(app, ["links"]).exit_code == 0

    result = runner.invoke(app, ["links", "--config", str(explicit_root / ".doc-lattice.yml")])

    assert result.exit_code == 1
    assert result.stderr == "'EXPLICIT.md':3: link target 'ONLY-IN-EXPLICIT.md' does not exist\n"


def test_links_prints_a_markup_shaped_filename_literally(tmp_path: Path, monkeypatch):
    # A complete Rich markup pair needs the '/' of its closing tag, so the shape under test can
    # only ever be a path, never a single filename: '[bold]red[' is a directory. The selector is
    # recursive for that reason alone.
    _config(tmp_path, "**/*.md")
    _write(tmp_path, "[bold]red[/bold].md", "# X\n\n[m](MISSING.md)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stderr.startswith("'[bold]red[/bold].md':3: ")
    assert "\x1b" not in result.stderr


def test_links_displays_a_control_byte_in_a_filename_as_its_escape(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md")
    _write(tmp_path, "esc\x1b.md", "# X\n\n[m](MISSING.md)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert "\x1b" not in result.stderr
    assert result.stderr.startswith("'esc\\x1b.md':3: ")


def test_links_prints_a_document_level_finding_without_a_line(tmp_path: Path, monkeypatch):
    # Spec 4.3's other human envelope. A finding about the document itself is raised before any
    # destination is read, so it carries no line and renders as `'path': message`.
    _config(tmp_path, "*.md")
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# o\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stderr == "'escape.md': link source leaves the project root through a symlink\n"


def test_links_exits_2_under_zero_config_naming_the_missing_file_and_key(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert result.stderr.startswith("error (CONFIG_ERROR): ")
    assert "no .doc-lattice.yml" in result.stderr
    assert "link_sources" in result.stderr


def test_links_exits_2_when_the_config_declares_no_link_sources(tmp_path: Path, monkeypatch):
    _write(tmp_path, ".doc-lattice.yml", "lattice_format: 2\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert ".doc-lattice.yml" in result.stderr
    assert "declares no link_sources" in result.stderr


def test_links_exits_2_naming_the_selector_that_matched_nothing(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md", "docs/**/*.md")
    _write(tmp_path, "README.md", "# R\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert "'docs/**/*.md'" in result.stderr
    assert "matches no file" in result.stderr


@_requires_permission_enforcement
def test_links_exits_2_on_a_directory_it_cannot_scan(tmp_path: Path, monkeypatch):
    _config(tmp_path, "docs/**/*.md")
    _write(tmp_path, "docs/a.md", "# a\n")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").chmod(0)
    try:
        result = runner.invoke(app, ["links"])
    finally:
        (tmp_path / "docs").chmod(0o755)

    assert result.exit_code == 2
    assert "could not scan" in result.stderr


def test_links_github_format_annotates_each_finding_on_stdout(tmp_path: Path, monkeypatch):
    _witness(tmp_path)
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# o\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=README.md,line=3,title=doc-lattice links::"
        "fragment '#nope' matches no heading in 'GUIDE.md'\n"
        "::error file=README.md,line=5,title=doc-lattice links::"
        "link target 'MISSING.md' does not exist\n"
        "::error file=escape.md,title=doc-lattice links::"
        "link source leaves the project root through a symlink\n"
    )
    assert result.stderr == ""


def test_links_rejects_json_and_indent(tmp_path: Path, monkeypatch):
    _witness(tmp_path)
    monkeypatch.chdir(tmp_path)

    rejected = runner.invoke(app, ["links", "--format", "json"])
    assert rejected.exit_code == 2
    assert "must be one of: github, human" in rejected.stderr

    unknown = runner.invoke(app, ["links", "--indent", "2"])
    assert unknown.exit_code == 2
    assert "No such option" in unknown.stderr


def test_links_help_names_only_its_two_formats(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["links", "--help"])

    assert result.exit_code == 0
    assert "human or github." in result.stdout
    assert "json" not in result.stdout


def test_links_github_output_to_a_departed_reader_raises_pipe_closed(tmp_path: Path):
    _witness(tmp_path)
    stdout = CliConsole(
        file=_RefusingStream(BrokenPipeError(errno.EPIPE, "Broken pipe")),
        no_color=True,
        color_system=None,
    )
    stderr = CliConsole(file=StringIO(), stderr=True, no_color=True, color_system=None)
    application = create_app(runtime_factory=_fixed_runtime(stdout, stderr, tmp_path))

    result = runner.invoke(application, ["links", "--format", "github"])

    assert isinstance(result.exception, PipeClosed)


def test_links_human_findings_to_a_departed_stderr_keep_exit_1(tmp_path: Path):
    _witness(tmp_path)
    stdout = CliConsole(file=StringIO(), no_color=True, color_system=None)
    stderr = CliConsole(
        file=_RefusingStream(BrokenPipeError(errno.EPIPE, "Broken pipe")),
        stderr=True,
        no_color=True,
        color_system=None,
    )
    application = create_app(runtime_factory=_fixed_runtime(stdout, stderr, tmp_path))

    result = runner.invoke(application, ["links"])

    assert result.exit_code == 1
    assert stderr.quiet is True


def _compat_config(root: Path, selectors: Sequence[str], compatibility: Sequence[str]) -> None:
    """Write a config carrying both source keys, spelled the way `_config` spells one."""
    _write(
        root,
        ".doc-lattice.yml",
        f"lattice_format: 2\nlink_sources: {_flow(selectors)}\n"
        f"legacy_marker_sources: {_flow(compatibility)}\n",
    )


def _marker_corpus(root: Path) -> None:
    """A legacy source and a strict one, both linking to one marker-only destination."""
    _write(root, "LEGACY.md", "# Legacy\n\n[t](GUIDE.md#legacy)\n")
    _write(root, "STRICT.md", "# Strict\n\n[t](GUIDE.md#legacy)\n")
    _write(root, "GUIDE.md", "# Guide\n\n## Topic {#legacy}\n")


def test_links_keeps_its_exit_code_contract_with_no_compatibility_declaration(
    tmp_path: Path, monkeypatch
):
    """The default path is the one an existing config runs, exit code and output included."""
    _config(tmp_path, "*.md")
    _marker_corpus(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        "'LEGACY.md':3: fragment '#legacy' matches no heading in 'GUIDE.md'\n"
        "'STRICT.md':3: fragment '#legacy' matches no heading in 'GUIDE.md'\n"
    )


def test_links_resolves_a_marker_only_for_the_declared_source(tmp_path: Path, monkeypatch):
    _compat_config(tmp_path, ["*.md"], ["LEGACY.md"])
    _marker_corpus(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stderr == ("'STRICT.md':3: fragment '#legacy' matches no heading in 'GUIDE.md'\n")


def test_links_exits_0_when_compatibility_covers_every_marker_reference(
    tmp_path: Path, monkeypatch
):
    _compat_config(tmp_path, ["*.md"], ["LEGACY.md", "STRICT.md"])
    _marker_corpus(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


def test_links_annotates_a_strict_source_under_a_partial_policy(tmp_path: Path, monkeypatch):
    """The github form carries the policy too, since the workflow is what a consumer runs."""
    _compat_config(tmp_path, ["*.md"], ["LEGACY.md"])
    _marker_corpus(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout.startswith("::error ")
    assert "STRICT.md" in result.stdout
    assert "LEGACY.md" not in result.stdout


def test_links_refuses_a_compatibility_selector_that_matches_no_selected_source(
    tmp_path: Path, monkeypatch
):
    """Exit 2, not a quiet policy over nothing: an ineffective declaration is a config error."""
    _compat_config(tmp_path, ["*.md"], ["docs/**"])
    _marker_corpus(tmp_path)
    _write(tmp_path, "docs/OTHER.md", "# Other\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert "legacy_marker_sources" in result.stderr
    assert "matches no link_sources file" in result.stderr


def test_links_refuses_an_ineffective_declaration_before_parsing_any_source(
    tmp_path: Path, monkeypatch
):
    """The refusal is not contingent on a source carrying a link the gate would have reported."""
    _compat_config(tmp_path, ["*.md"], ["GONE.md"])
    _write(tmp_path, "README.md", "# Readme\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert "'GONE.md'" in result.stderr


def test_links_refuses_a_declared_empty_compatibility_list(tmp_path: Path, monkeypatch):
    _compat_config(tmp_path, ["*.md"], [])
    _write(tmp_path, "README.md", "# Readme\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert "names no selector" in result.stderr


def test_links_refuses_a_malformed_compatibility_entry_naming_its_own_key(
    tmp_path: Path, monkeypatch
):
    _compat_config(tmp_path, ["*.md"], ["docs\\\\a.md"])
    _write(tmp_path, "README.md", "# Readme\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert "legacy_marker_sources entry" in result.stderr
    assert "backslash" in result.stderr


def test_links_applies_compatibility_to_a_recursive_selector(tmp_path: Path, monkeypatch):
    """Matching is against the retained spellings, so a `**` declaration reaches every depth."""
    _compat_config(tmp_path, ["*.md", "docs/**/*.md"], ["docs/**"])
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Topic {#legacy}\n")
    _write(tmp_path, "README.md", "# Readme\n\n[t](GUIDE.md#legacy)\n")
    _write(tmp_path, "docs/a.md", "# A\n\n[t](../GUIDE.md#legacy)\n")
    _write(tmp_path, "docs/deep/b.md", "# B\n\n[t](../../GUIDE.md#legacy)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stderr == ("'README.md':3: fragment '#legacy' matches no heading in 'GUIDE.md'\n")


def test_links_listing_preserves_engine_selection_order_and_spelling(tmp_path: Path, monkeypatch):
    selectors = ["**/*.md", "docs/**/*.md", "real.md"]
    _config(tmp_path, *selectors)
    for name in ["real.md", "docs/a.md", "docs/deep/b.md", ".hidden/x.md", "a/z.md", "a!.md"]:
        _write(tmp_path, name, "# Document\n")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--list-sources"])

    assert (result.exit_code, result.stderr) == (0, "")
    decoded = [ast.literal_eval(line) for line in result.stdout.splitlines()]
    assert decoded == [
        source.relative_to(tmp_path).as_posix()
        for source in select_link_sources(tmp_path, selectors)
    ]
    assert decoded == [".hidden/x.md", "a!.md", "a/z.md", "alias.md", "docs/a.md", "docs/deep/b.md"]


@pytest.mark.parametrize("format_args", [[], ["--format", "human"]])
def test_links_listing_uses_explicit_config_from_another_directory(
    tmp_path: Path, monkeypatch, format_args
):
    _config(tmp_path, "*.md")
    _write(tmp_path, "DEFAULT.md", "# Default\n")
    explicit = tmp_path / "explicit"
    _compat_config(explicit, ["*.md"], ["EXPLICIT.md"])
    _write(explicit, "EXPLICIT.md", "[dead](missing.md)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["links", "--list-sources", "--config", str(explicit / ".doc-lattice.yml"), *format_args],
    )

    assert (result.exit_code, result.stdout, result.stderr) == (0, "'EXPLICIT.md'\n", "")


def test_links_listing_keeps_each_outside_root_alias(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    _config(root, "*.md")
    _write(tmp_path, "outside.md", "[dead](missing.md)\n")
    for name in ["alias.md", "other.md"]:
        (root / name).symlink_to(tmp_path / "outside.md")
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["links", "--list-sources"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "'alias.md'\n'other.md'\n", "")


def test_links_listing_does_not_check_document_contents(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md")
    (tmp_path / "bytes.md").write_bytes(b"\xff\xfe")
    _write(tmp_path, "dead.md", "[dead](missing.md)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--list-sources"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "'bytes.md'\n'dead.md'\n", "")


@pytest.mark.parametrize("forbidden", ["checker", "parser", "builtins.open", "io.open", "os.open"])
def test_links_listing_never_checks_parses_or_opens_sources(tmp_path: Path, monkeypatch, forbidden):
    _compat_config(tmp_path, ["*.md"], ["README.md"])
    source = tmp_path / "README.md"
    source.write_text("[dead](missing.md)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def refuse(*_args, **_kwargs):
        pytest.fail(f"listing called {forbidden}")

    if forbidden == "checker":
        monkeypatch.setattr("doc_lattice.cli.commands.links.check_links", refuse)
    elif forbidden == "parser":
        monkeypatch.setattr(MarkdownIt, "parse", refuse)
    else:
        module = {"builtins.open": builtins, "io.open": io, "os.open": os}[forbidden]
        real_open = module.open

        def guarded_open(file, *args, **kwargs):
            if not isinstance(file, int) and Path(os.fsdecode(file)).absolute() == source:
                pytest.fail(f"listing opened source through {forbidden}")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(module, "open", guarded_open)

    result = runner.invoke(app, ["links", "--list-sources"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "'README.md'\n", "")


@pytest.mark.skipif(os.name != "posix", reason="filenames contain POSIX-only characters")
def test_links_listing_round_trips_quoted_backslashed_and_control_filenames(tmp_path, monkeypatch):
    names = ["single'.md", 'double".md', "back\\slash.md", "new\nline.md", "esc\x1b\x07\x7f\x85.md"]
    _config(tmp_path, "*.md")
    for name in names:
        _write(tmp_path, name, "# Document\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--list-sources"])

    assert (result.exit_code, result.stderr) == (0, "")
    lines = result.stdout.splitlines()
    assert len(lines) == len(names)
    assert [ast.literal_eval(line) for line in lines] == sorted(names)
    assert result.stdout.endswith("\n")
    assert all(ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F for line in lines for char in line)


def _assert_listing_refusal_matches_gate(*args: str) -> None:
    ordinary = runner.invoke(app, ["links", *args])
    listing = runner.invoke(app, ["links", "--list-sources", *args])
    assert ordinary.exit_code == listing.exit_code == 2
    assert ordinary.stdout == listing.stdout == ""
    assert ordinary.stderr == listing.stderr
    assert ordinary.stderr.startswith("error (")


@pytest.mark.parametrize(
    "config_text",
    [
        None,
        "[invalid yaml",
        "lattice_format: 2\n",
        "lattice_format: 2\nlink_sources: []\n",
        "lattice_format: 2\nlink_sources: null\n",
        "lattice_format: 2\nlink_sources: wrong\n",
        "lattice_format: 2\nlink_sources: [7]\n",
        "lattice_format: 2\nlink_sources: ['../*.md']\n",
        "lattice_format: 2\nlink_sources: ['missing/*.md']\n",
        *[
            f"lattice_format: 2\nlink_sources: ['*.md']\nlegacy_marker_sources: {value}\n"
            for value in [
                "null",
                "[]",
                "wrong",
                "[7]",
                "['../*.md']",
                "['missing.md']",
                "['real.md']",
            ]
        ],
    ],
)
def test_links_listing_preserves_config_and_selector_refusals(tmp_path, monkeypatch, config_text):
    if config_text is not None:
        _write(tmp_path, ".doc-lattice.yml", config_text)
    _write(tmp_path, "real.md", "# Real\n")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    monkeypatch.chdir(tmp_path)

    _assert_listing_refusal_matches_gate()


def test_links_listing_preserves_explicit_missing_config_refusal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _assert_listing_refusal_matches_gate("--config", str(tmp_path / "missing.yml"))


@_requires_permission_enforcement
@pytest.mark.parametrize("unreadable", ["config", "directory"])
def test_links_listing_preserves_permission_refusals(tmp_path, monkeypatch, unreadable):
    _config(tmp_path, "docs/**/*.md")
    _write(tmp_path, "docs/a.md", "# A\n")
    monkeypatch.chdir(tmp_path)
    target = tmp_path / (".doc-lattice.yml" if unreadable == "config" else "docs")
    mode = target.stat().st_mode
    target.chmod(0)
    try:
        _assert_listing_refusal_matches_gate()
    finally:
        target.chmod(mode)


@pytest.mark.parametrize("operation", ["resolve", "stat", "scan", "inspect"])
def test_links_listing_preserves_filesystem_refusals(tmp_path, monkeypatch, operation):
    _config(tmp_path, "*.md")
    source = tmp_path / "source.md"
    _write(tmp_path, "source.md", "# Source\n")
    monkeypatch.chdir(tmp_path)
    if operation in {"resolve", "stat"}:
        original = getattr(Path, operation)

        def refusing_path(path, *args, **kwargs):
            if path == source:
                raise PermissionError(errno.EACCES, "test inspection refused", str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, operation, refusing_path)
    elif operation == "scan":
        original_scan = os.scandir

        def refusing_scan(path):
            if path == tmp_path:
                raise PermissionError(errno.EACCES, "test scan refused", str(path))
            return original_scan(path)

        monkeypatch.setattr(os, "scandir", refusing_scan)
    else:
        original_is_dir = os.DirEntry.is_dir

        def refusing_inspection(entry, **kwargs):
            if entry.name == source.name:
                raise PermissionError(errno.EACCES, "test entry refused", entry.path)
            return original_is_dir(entry, **kwargs)

        monkeypatch.setattr(os.DirEntry, "is_dir", refusing_inspection)

    _assert_listing_refusal_matches_gate()


@pytest.mark.parametrize("kind", ["dangling", "directory", "fifo"])
def test_links_listing_preserves_nonregular_source_refusals(tmp_path, monkeypatch, kind):
    _config(tmp_path, "*.md")
    selected = tmp_path / "selected.md"
    if kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs unavailable")
        os.mkfifo(selected)
    elif kind == "directory":
        (tmp_path / "directory").mkdir()
        selected.symlink_to(tmp_path / "directory", target_is_directory=True)
    else:
        selected.symlink_to(tmp_path / "missing")
    monkeypatch.chdir(tmp_path)

    _assert_listing_refusal_matches_gate()


@pytest.mark.parametrize("dangling", [False, True])
def test_links_listing_rejects_github_before_selection(tmp_path, monkeypatch, dangling):
    _config(tmp_path, "*.md")
    if dangling:
        (tmp_path / "source.md").symlink_to(tmp_path / "missing")
    else:
        _write(tmp_path, "source.md", "# Source\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--list-sources", "--format", "github"])

    assert (result.exit_code, result.stdout, result.stderr) == (
        2,
        "",
        "error: --list-sources cannot be combined with --format github\n",
    )


def test_links_listing_rejects_github_before_loading_config(monkeypatch):
    def refuse(*_args, **_kwargs):
        pytest.fail("format conflict loaded configuration")

    monkeypatch.setattr("doc_lattice.cli.runtime.load_config", refuse)
    result = runner.invoke(app, ["links", "--list-sources", "--format", "github"])

    assert (result.exit_code, result.stdout, result.stderr) == (
        2,
        "",
        "error: --list-sources cannot be combined with --format github\n",
    )


def test_links_listing_still_validates_format():
    result = runner.invoke(app, ["links", "--list-sources", "--format", "json"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "must be one of: github, human" in result.stderr
