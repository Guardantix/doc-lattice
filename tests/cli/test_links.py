"""CLI integration tests for the links command."""

import errno
import os
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from doc_lattice.cli import app
from doc_lattice.cli.application import create_app
from doc_lattice.cli.pipe_policy import PipeClosed
from doc_lattice.cli.runtime import CliConsole, CliRuntime, RuntimeFactory
from doc_lattice.config import load_config
from doc_lattice.orchestrate import load_lattice

from .helpers import runner

_requires_permission_enforcement = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX filesystem that enforces modes"
)


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _config(root: Path, *selectors: str) -> None:
    listed = ", ".join(f"'{selector}'" for selector in selectors)
    _write(root, ".doc-lattice.yml", f"lattice_format: 2\nlink_sources: [{listed}]\n")


def _witness(root: Path) -> None:
    """One dead fragment and one dead relative path, in one source."""
    _config(root, "*.md")
    _write(root, "README.md", "# Readme\n\n[a](GUIDE.md#nope)\n\n[b](MISSING.md)\n")
    _write(root, "GUIDE.md", "# Guide\n")


class _RefusingStream(StringIO):
    def __init__(self, error: OSError) -> None:
        super().__init__()
        self._error = error

    def write(self, _text: str) -> int:
        raise self._error


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
