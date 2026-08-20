"""Tests for the dependency-free broken-pipe primitives."""

import ast
import errno
import io
import os
import subprocess
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
    # is the one outcome it must never produce. Asserting the postcondition rather than only
    # the absence of a raise is deliberate: an earlier version of this test checked just that
    # `neutralize` returned, and passed while the call was leaving the descriptor closed.
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    fd = handle.fileno()
    handle.close()

    neutralize(fd)

    assert _fd_is_open(fd), "neutralize left the descriptor closed instead of writable"
    os.write(fd, b"discarded")


@pytest.mark.skipif(os.name != "posix", reason="descriptor numbering is POSIX-only")
def test_neutralize_keeps_the_target_open_when_devnull_reuses_it():
    """The reuse case, on the exact descriptor the entry point's fallback passes.

    ``os.open`` returns the lowest unused descriptor, so when the target is not merely dead but
    *closed*, the open returns the target itself and ``dup2`` becomes a documented no-op. Closing
    the duplicate would then close the descriptor the call was asked to make writable, and the
    interpreter's shutdown flush would answer with the very 120 this exists to prevent.

    A subprocess, because the scenario needs file descriptor 2 itself closed while 0 and 1 stay
    open, which is what makes 2 the lowest unused one. In-process, whichever descriptors happen
    to be free decides what ``os.open`` returns, so the reuse would be incidental rather than
    forced and the test would pass without exercising anything.
    """
    program = """
import os, sys
sys.path.insert(0, sys.argv[1])
from doc_lattice.cli.pipe_policy import STDERR_FILENO, neutralize

os.close(STDERR_FILENO)
neutralize(STDERR_FILENO)

try:
    os.fstat(STDERR_FILENO)
except OSError:
    os.write(1, b"CLOSED")
else:
    os.write(STDERR_FILENO, b"discarded by devnull")
    os.write(1, b"OPEN")
"""
    completed = subprocess.run(  # noqa: S603 (fixed interpreter and static test program)
        [sys.executable, "-c", program, str(_MODULE.parents[2])],
        capture_output=True,
        check=False,
    )

    assert completed.stdout == b"OPEN", (
        "neutralize closed the descriptor it was asked to neutralize; "
        f"child exited {completed.returncode}"
    )


def test_neutralize_survives_a_nonsense_descriptor():
    neutralize(-1)


def _fd_is_open(fd: int) -> bool:
    """Report whether one file descriptor is currently open in this process."""
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


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
