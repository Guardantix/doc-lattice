"""Tests for the CLI."""

from typer.testing import CliRunner

from game_lattice.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_hello():
    result = runner.invoke(app, ["hello", "--name", "gx"])
    assert result.exit_code == 0
    assert "Hello, gx!" in result.output
