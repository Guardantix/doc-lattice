"""Tests for shared CLI output selection and exact writers."""

import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
import typer
from rich.console import Console

from doc_lattice.cli.output import (
    annotation_root,
    escape_github_message,
    escape_github_property,
    github_annotation,
    select_output,
    write_json,
)
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.config import ProjectConfig
from doc_lattice.constants import VALID_REPORT_FORMATS
from doc_lattice.model import Lattice


def _contents(console: Console) -> str:
    stream = console.file
    assert isinstance(stream, StringIO)
    return stream.getvalue()


@pytest.fixture
def runtime(tmp_path: Path) -> CliRuntime:
    def unexpected_config(_config: Path | None, _cwd: Path) -> ProjectConfig:
        raise AssertionError("output policy must not load config")

    def unexpected_lattice(
        project: ProjectConfig,
        *,
        require_verified: bool = False,
        persist_cache: bool = True,
    ) -> Lattice:
        del project
        raise AssertionError(
            f"output policy must not load lattice {require_verified=} {persist_cache=}"
        )

    return CliRuntime(
        stdout=Console(file=StringIO(), no_color=True),
        stderr=Console(file=StringIO(), stderr=True, no_color=True),
        cwd=tmp_path,
        load_config=unexpected_config,
        load_lattice=unexpected_lattice,
    )


def test_explicit_json_format_resolves(runtime: CliRuntime):
    selection = select_output(
        runtime,
        fmt="json",
        valid=VALID_REPORT_FORMATS,
        indent=2,
    )

    assert selection.format == "json"
    assert selection.indent == 2


def test_unknown_format_is_rejected(runtime: CliRuntime):
    with pytest.raises(typer.Exit) as raised:
        select_output(
            runtime,
            fmt="yaml",
            valid=VALID_REPORT_FORMATS,
        )

    assert raised.value.exit_code == 2
    assert "--format 'yaml' must be one of" in _contents(runtime.stderr)


def test_indent_requires_effective_json(runtime: CliRuntime):
    with pytest.raises(typer.Exit) as raised:
        select_output(
            runtime,
            fmt="human",
            valid=VALID_REPORT_FORMATS,
            indent=2,
        )

    assert raised.value.exit_code == 2
    assert _contents(runtime.stderr) == "error: --indent requires --format json\n"


def test_zero_indent_is_supported_for_effective_json(runtime: CliRuntime):
    selection = select_output(
        runtime,
        fmt="json",
        valid=VALID_REPORT_FORMATS,
        indent=0,
    )

    assert selection.format == "json"
    assert selection.indent == 0


def test_write_json_uses_exact_injected_stdout(runtime: CliRuntime):
    write_json(runtime, {"a": [1]}, indent=2)

    assert _contents(runtime.stdout) == '{\n  "a": [\n    1\n  ]\n}\n'
    assert json.loads(_contents(runtime.stdout)) == {"a": [1]}


def test_github_annotation_uses_escaped_absolute_path_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "outside%:,\nfile.md"

    result = github_annotation(outside, tmp_path, "title", "message")

    assert result == (f"::error file={escape_github_property(str(outside))},title=title::message")


def test_github_annotation_escapes_all_workflow_metacharacters(tmp_path: Path):
    result = github_annotation(
        tmp_path / "sub%:,\nline.md",
        tmp_path,
        "title%:,\r\nline",
        "message%:,\r\nline",
    )

    assert result == (
        "::error file=sub%25%3A%2C%0Aline.md,title=title%25%3A%2C%0D%0Aline::message%25:,%0D%0Aline"
    )


def test_escape_github_message_encodes_workflow_command_metacharacters():
    assert escape_github_message("100%\rfirst\nsecond: a,b") == ("100%25%0Dfirst%0Asecond: a,b")


def test_escape_github_property_encodes_message_and_property_metacharacters():
    assert escape_github_property("100%\rfirst\nsecond: a,b") == (
        "100%25%0Dfirst%0Asecond%3A a%2Cb"
    )


def test_annotation_root_prefers_a_workspace_that_contains_the_document(
    runtime: CliRuntime, tmp_path: Path
):
    # Under Actions the checkout root is the base that makes an annotation land on the file
    # in the diff, whatever subdirectory the command was invoked from.
    workspace = tmp_path / "checkout"
    nested = workspace / "packages" / "game"
    nested.mkdir(parents=True)
    document = nested / "docs" / "down.md"

    in_workspace = replace(runtime, cwd=nested, workspace=workspace)

    assert annotation_root(in_workspace, document) == workspace


def test_annotation_root_falls_back_to_cwd_when_the_workspace_excludes_the_document(
    runtime: CliRuntime, tmp_path: Path
):
    # A set but non-containing GITHUB_WORKSPACE must not reach the renderer: it would emit an
    # absolute path rather than taking the cwd fallback the selection exists to preserve.
    workspace = tmp_path / "other-checkout"
    workspace.mkdir()
    cwd = tmp_path / "elsewhere"
    document = cwd / "docs" / "down.md"

    outside = replace(runtime, cwd=cwd, workspace=workspace)

    assert annotation_root(outside, document) == cwd


def test_annotation_root_falls_back_to_cwd_when_no_workspace_is_set(
    runtime: CliRuntime, tmp_path: Path
):
    document = tmp_path / "docs" / "down.md"

    assert runtime.workspace is None
    assert annotation_root(runtime, document) == tmp_path
