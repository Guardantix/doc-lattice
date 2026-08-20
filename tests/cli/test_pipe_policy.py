"""Tests for the dependency-free broken-pipe primitives."""

import ast
import errno
import io
import os
import sys
from pathlib import Path

import pytest

from doc_lattice.cli.pipe_policy import (
    STDERR_FILENO,
    STDOUT_FILENO,
    PipeClosed,
    file_descriptor,
    neutralize,
)

_MODULE = Path(__file__).resolve().parents[2] / "src/doc_lattice/cli/pipe_policy.py"


def test_pipe_closed_carries_no_errno():
    # The whole reason a direct stream write is re-raised as this class. Typer's `_main` wraps
    # every command in `except OSError` and converts a member of that family whose errno is
    # EPIPE into sys.exit(1) -- the code check and lint reserve for drift. An errno of None is
    # what makes Typer re-raise instead, so the entry point can answer with 141. A constructor
    # that started passing an errno through would restore the collision silently.
    assert PipeClosed().errno is None
    assert isinstance(PipeClosed(), BrokenPipeError)


def test_pipe_closed_is_not_mistaken_for_an_epipe_oserror():
    # The exact test Typer's handler applies, asserted here rather than left to the dependency,
    # because this project cannot see that branch move.
    assert PipeClosed().errno != errno.EPIPE


def test_the_standard_descriptor_literals_match_the_running_process():
    # They are last-resort values for a stream with no fileno() of its own, so they only have to
    # be right about POSIX; pinning them against the live interpreter is what catches a typo.
    assert sys.__stdout__ is not None
    assert sys.__stderr__ is not None
    assert sys.__stdout__.fileno() == STDOUT_FILENO
    assert sys.__stderr__.fileno() == STDERR_FILENO


def test_pipe_policy_imports_nothing_but_the_standard_library():
    """The module's whole purpose is being importable when everything else failed to import.

    ``cli/__init__.py`` reaches this module from its *unguarded* import block, so that the
    pre-renderer fallback can neutralize file descriptor 2 in the very case where ``rich``,
    ``typer``, and most of the engine raised an escalated warning on the way in. An import added
    here that reached any of them would make the fallback unreachable exactly where it is
    needed, and would do so without failing any behavioral test.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"), filename=str(_MODULE))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import has no module root to inspect; any at all is a violation.
            assert node.level == 0, f"relative import at line {node.lineno}"
            assert node.module is not None
            imported.add(node.module.split(".")[0])

    assert imported <= set(sys.stdlib_module_names), (
        f"pipe_policy imports outside the standard library: "
        f"{sorted(imported - set(sys.stdlib_module_names))}"
    )
    assert "doc_lattice" not in imported


def test_file_descriptor_reads_a_real_stream(tmp_path: Path):
    target = tmp_path / "out.txt"
    with target.open("w", encoding="utf-8") as handle:
        assert file_descriptor(handle) == handle.fileno()


@pytest.mark.parametrize(
    "stream",
    [io.StringIO(), object(), None],
    ids=["in-memory", "no-fileno-attribute", "none"],
)
def test_file_descriptor_answers_none_when_there_is_nothing_to_neutralize(stream):
    # io.UnsupportedOperation derives from both OSError and ValueError; an object with no
    # fileno at all has no attribute to call. Both mean "no descriptor", never a raise into a
    # caller that is already handling a failed write.
    assert file_descriptor(stream) is None


def test_file_descriptor_answers_none_for_a_closed_stream(tmp_path: Path):
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    handle.close()

    assert file_descriptor(handle) is None


@pytest.mark.skipif(os.name != "posix", reason="descriptor redirection is POSIX-only")
def test_neutralize_redirects_only_the_descriptor_it_is_given(tmp_path: Path):
    """The precise failure Rich's own hook has: it redirects fd 1 whichever stream broke."""
    kept = tmp_path / "kept.txt"
    dropped = tmp_path / "dropped.txt"
    with kept.open("w", encoding="utf-8") as keep, dropped.open("w", encoding="utf-8") as drop:
        neutralize(drop.fileno())
        drop.write("swallowed")
        keep.write("preserved")

    assert kept.read_text(encoding="utf-8") == "preserved"
    assert dropped.read_text(encoding="utf-8") == ""


@pytest.mark.skipif(os.name != "posix", reason="descriptor redirection is POSIX-only")
def test_neutralize_leaks_no_descriptor(tmp_path: Path):
    # It opens os.devnull on every call and runs on a failing process, so a leak here would
    # accumulate silently. The duplicate is closed once dup2 has copied it over the target.
    with (tmp_path / "out.txt").open("w", encoding="utf-8") as handle:
        before = _open_descriptor_count()
        neutralize(handle.fileno())
        assert _open_descriptor_count() == before


def test_neutralize_survives_a_descriptor_that_is_already_closed(tmp_path: Path):
    # It runs while the process is already failing, so a second exception raised over the first
    # is the one outcome it must never produce.
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    fd = handle.fileno()
    handle.close()

    neutralize(fd)


def test_neutralize_survives_a_nonsense_descriptor():
    neutralize(-1)


def _open_descriptor_count() -> int:
    """Count this process's open file descriptors, without assuming /proc exists."""
    count = 0
    for candidate in range(256):
        try:
            os.fstat(candidate)
        except OSError:
            continue
        count += 1
    return count
