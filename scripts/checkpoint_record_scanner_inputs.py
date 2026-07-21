"""Pytest plugin that records every source scanned by the legacy shell scanner suite.

Loaded with ``pytest -p scripts.checkpoint_record_scanner_inputs`` while running
``tests/test_github_ci_shell_scanner.py``. It wraps both public scanner entry points and the
``_ShellScanner`` constructor before test modules import them, deduplicates recorded sources by
SHA-256, and writes the frozen replay inventory on exit when ``CHECKPOINT_REPLAY_OUT`` is set.

Three capture points together cover every input the suite exercises. The
``scan_doc_lattice_invocations`` and ``direct_doc_lattice_invocations`` wrappers record the raw
script at each public call, which stays primary because ``scan_doc_lattice_invocations``
short-circuits over-cap sources before it ever constructs ``_ShellScanner``. A recording
``_ShellScanner`` subclass captures sources handed straight to the constructor by tests, such as
the scan-budget case. A module-level flag, raised by the ``scan_`` wrapper, suppresses the
subclass while a public call is in flight so the normalized source that
``scan_doc_lattice_invocations`` constructs internally is never recorded a second time. The suite
is single-threaded, so a plain module flag is sufficient.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from doc_lattice.github_ci import shell_scanner

_RECORDS: dict[str, str] = {}
_ORIGINAL_DIRECT = shell_scanner.direct_doc_lattice_invocations
_ORIGINAL_SCAN = shell_scanner.scan_doc_lattice_invocations
_ORIGINAL_SCANNER = shell_scanner._ShellScanner

_IN_PUBLIC_CALL = False


def _record(source: str) -> None:
    """Store ``source`` keyed by its SHA-256 digest, deduplicating repeated scripts."""
    _RECORDS[hashlib.sha256(source.encode()).hexdigest()] = source


def _recording(
    script: str,
    *,
    context: str | None = None,
) -> tuple[shell_scanner._Invocation, ...]:
    """Record ``script`` and delegate to the real direct entry point."""
    _record(script)
    return _ORIGINAL_DIRECT(script, context=context)


def _recording_scan(script: str) -> shell_scanner.ShellScanResult:
    """Record ``script``, then run the real scan while the in-public-call flag is raised."""
    global _IN_PUBLIC_CALL  # noqa: PLW0603 (single-threaded suite; module-level flag)
    _record(script)
    _IN_PUBLIC_CALL = True
    try:
        return _ORIGINAL_SCAN(script)
    finally:
        _IN_PUBLIC_CALL = False


class _RecordingShellScanner(shell_scanner._ShellScanner):
    """``_ShellScanner`` subclass that records sources handed directly to the constructor."""

    def __init__(
        self,
        source: str,
        *,
        budget: shell_scanner._ScanBudget | None = None,
        invocations: list[shell_scanner._Invocation] | None = None,
        classify_commands: bool = True,
    ) -> None:
        """Record ``source`` for raw test constructions, then delegate to the real scanner.

        The public ``scan_`` wrapper raises ``_IN_PUBLIC_CALL`` around the real scan, so the
        source that ``scan_doc_lattice_invocations`` normalizes and its recursive child scanners
        are skipped here and only the raw test-level construction is recorded.
        """
        if not _IN_PUBLIC_CALL:
            _record(source)
        super().__init__(
            source,
            budget=budget,
            invocations=invocations,
            classify_commands=classify_commands,
        )


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001 (pytest hook signature)
    """Install the recording wrappers before test modules import the scanner."""
    shell_scanner.direct_doc_lattice_invocations = _recording  # ty: ignore[invalid-assignment]
    shell_scanner.scan_doc_lattice_invocations = _recording_scan  # ty: ignore[invalid-assignment]
    shell_scanner._ShellScanner = _RecordingShellScanner  # ty: ignore[invalid-assignment]


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa: ARG001 (pytest hook signature)
    """Write the inventory and restore the original entry points and constructor."""
    shell_scanner.direct_doc_lattice_invocations = _ORIGINAL_DIRECT
    shell_scanner.scan_doc_lattice_invocations = _ORIGINAL_SCAN
    shell_scanner._ShellScanner = _ORIGINAL_SCANNER
    out = os.environ.get("CHECKPOINT_REPLAY_OUT")
    if not out:
        return
    hashes = sorted(_RECORDS)
    entries = [
        {"id": f"replay-{index:04d}", "sha256": digest, "source": _RECORDS[digest]}
        for index, digest in enumerate(hashes, start=1)
    ]
    inventory = {
        "count": len(entries),
        "aggregate_sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
        "entries": entries,
    }
    Path(out).write_text(json.dumps(inventory, indent=2) + "\n")
