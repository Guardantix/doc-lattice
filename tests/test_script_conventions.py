"""Convention enforcement over `scripts/`, which the sdist does not carry.

`tests/test_conventions.py` owns the same rules over `src/` and ships with the archive. This
module reads repository-only files, so it is named in that archive's denial set and in the sdist
manifest's exclude list, which `tests/test_package_metadata.py` holds to each other.
"""

from pathlib import Path

from clock_scan import clock_readers, current_time_calls

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
# The scripts allowed to spell a current-time call themselves, keyed by path relative to
# `scripts/` and carrying the number of readings each may make. Paths rather than names, for the
# reason `check_typing_boundaries.py` holds AD-3's boundaries as exact source-root-relative
# paths: opening a boundary is an edit someone makes, never one a file earns by being named a
# certain way. Counts for the reason `PIN_MANIFEST` carries one: the rule is a *single*
# substitutable clock, so a second reading in an exempt file is a second clock however it is
# spelled. AD-50 records why this root needs an entry at all.
_CLOCK_SCRIPTS = {"audit_action_runtimes.py": 1}


def _script_files() -> list[Path]:
    """Every contributor script, recursively, excluding bytecode caches."""
    return [p for p in SCRIPTS_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_scripts_read_the_clock_only_where_the_manifest_says():
    """AD-2's ban one directory over, where AD-2's boundary cannot be imported.

    `scripts/` was never scanned, so the rule CLAUDE.md states without qualification held over
    `src/` alone and a second clock could enter here unnoticed. It has to be a manifest rather
    than a blanket ban because one script genuinely needs the reading: the runtime audit measures
    a run's age, and it runs under `uv run --no-project`, where nothing in the package imports.
    """
    assert current_time_calls("datetime.now(tz=UTC)")  # positive control: arg'd form caught
    assert _script_files(), "no scripts found; this suite reads the repository checkout"

    assert clock_readers(_script_files(), SCRIPTS_DIR) == _CLOCK_SCRIPTS
