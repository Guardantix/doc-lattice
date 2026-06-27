"""Tests for reconcile."""

from pathlib import Path

import pytest

from game_lattice.check import check_lattice
from game_lattice.config import load_config
from game_lattice.error_types import BrokenRefError
from game_lattice.orchestrate import load_lattice
from game_lattice.reconcile import apply_reconcile, reconcile


def test_apply_reconcile_sets_seen_and_preserves_body():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\n# Body\nkeep me\n"
    out = apply_reconcile(text, {"a#x": "newhash"})
    assert "seen: newhash" in out
    assert "old" not in out
    assert out.endswith("# Body\nkeep me\n")


def test_apply_reconcile_adds_missing_seen():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n---\nbody\n"
    out = apply_reconcile(text, {"a#x": "h"})
    assert "seen: h" in out


def test_reconcile_clears_drift_for_node(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    writes = reconcile(lat, "pc-design", ref=None, reconcile_all=False)
    # Apply the planned writes to disk.
    for path, updates in writes.items():
        path.write_text(
            apply_reconcile(path.read_text(encoding="utf-8"), updates), encoding="utf-8"
        )
    # Reload and confirm pc-design no longer drifts.
    relat = load_lattice(load_config(None, lattice_dir))
    pc_states = [s.state for s in check_lattice(relat) if s.source_id == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_preserves_concurrent_body_edit():
    text_initial = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nORIGINAL\n"
    # Simulate a concurrent body edit before the in-place write.
    text_fresh = text_initial.replace("ORIGINAL", "EDITED LATER")
    out = apply_reconcile(text_fresh, {"a#x": "newhash"})
    assert "EDITED LATER" in out
    assert "seen: newhash" in out


def test_reconcile_refuses_broken(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(BrokenRefError):
        reconcile(lat, "gdd", ref=None, reconcile_all=False)
