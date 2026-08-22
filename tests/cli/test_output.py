"""Tests for shared CLI output selection and exact writers."""

import json
from pathlib import Path

import pytest
import typer

from doc_lattice.cli.output import select_output, write_json
from doc_lattice.cli.runtime import CliRuntime
from doc_lattice.constants import VALID_REPORT_FORMATS

from .helpers import _contents, _stub_runtime


@pytest.fixture
def runtime(tmp_path: Path) -> CliRuntime:
    return _stub_runtime(tmp_path, "output policy")


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
