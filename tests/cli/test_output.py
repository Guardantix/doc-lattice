"""Tests for shared CLI output selection and exact writers."""

import json
from io import StringIO
from pathlib import Path

import pytest
import typer
from rich.console import Console

from doc_lattice.cli.output import (
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
