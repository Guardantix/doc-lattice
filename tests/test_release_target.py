"""Behavior tests for the release target step-output script."""

import os
import subprocess
import sys
from pathlib import Path

from doc_lattice import __version__

_ROOT = Path(__file__).resolve().parents[1]
_TARGET = _ROOT / "scripts/release_target.py"


def _run(output_path: Path | None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("GITHUB_OUTPUT", None)
    if output_path is not None:
        environment["GITHUB_OUTPUT"] = str(output_path)
    return subprocess.run(  # noqa: S603 - controlled test interpreter arguments
        (sys.executable, str(_TARGET)),
        cwd=_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_target_writes_version_and_tag_outputs(tmp_path):
    output_path = tmp_path / "github_output"
    output_path.touch()

    result = _run(output_path)

    assert result.returncode == 0, result.stderr
    assert output_path.read_text(encoding="utf-8") == (
        f"version={__version__}\ntag=v{__version__}\n"
    )


def test_release_target_appends_to_existing_output(tmp_path):
    output_path = tmp_path / "github_output"
    output_path.write_text("existing=value\n", encoding="utf-8")

    result = _run(output_path)

    assert result.returncode == 0, result.stderr
    assert output_path.read_text(encoding="utf-8").startswith("existing=value\n")


def test_release_target_fails_without_github_output():
    result = _run(None)

    assert result.returncode == 1
    assert "GITHUB_OUTPUT is not set" in result.stderr
