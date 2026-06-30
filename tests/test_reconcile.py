"""Tests for reconcile."""

from pathlib import Path

import pytest

from game_lattice.check import check_lattice
from game_lattice.config import load_config
from game_lattice.error_types import (
    BrokenRefError,
    ProjectError,
    UnreadableDocError,
    ValidationError,
)
from game_lattice.orchestrate import load_lattice
from game_lattice.reconcile import apply_reconcile, reconcile


def _apply_plan(plan: dict[Path, dict[str, str]]) -> None:
    for path, updates in plan.items():
        new_text, _ = apply_reconcile(path.read_text(encoding="utf-8"), updates)
        path.write_text(new_text, encoding="utf-8")


def _planned_refs(plan: dict[Path, dict[str, str]]) -> set[str]:
    """Collect every target ref across all files in a reconcile plan."""
    return {ref for updates in plan.values() for ref in updates}


def test_apply_reconcile_sets_seen_and_preserves_body():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\n# Body\nkeep me\n"
    out, applied = apply_reconcile(text, {"a#x": "newhash"})
    assert "seen: newhash" in out
    assert "old" not in out
    assert out.endswith("# Body\nkeep me\n")
    assert applied == {"a#x"}


def test_apply_reconcile_adds_missing_seen():
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "h"})
    assert "seen: h" in out
    assert applied == {"a#x"}


def test_apply_reconcile_no_match_leaves_text_and_reports_nothing():
    # A ref edited away between load and write no longer matches the plan key.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#gone": "newhash"})
    assert applied == set()
    assert out == text


def test_apply_reconcile_null_derives_from_is_safe():
    text = "---\nid: d\nderives_from:\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "h"})
    assert applied == set()
    assert out == text


def test_apply_reconcile_unparseable_frontmatter_raises():
    text = "---\nfoo: [1, 2\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"})


def test_apply_reconcile_non_mapping_frontmatter_raises():
    text = "---\n- just\n- a list\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"})


def test_apply_reconcile_non_mapping_entry_raises():
    text = "---\nid: d\nderives_from:\n  - plainstring\n---\nbody\n"
    with pytest.raises(UnreadableDocError):
        apply_reconcile(text, {"a#x": "h"})


def test_reconcile_clears_drift_for_node(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    plan = reconcile(lat, "pc-design", ref=None, reconcile_all=False)
    _apply_plan(plan)
    # Reload and confirm pc-design no longer drifts.
    relat = load_lattice(load_config(None, lattice_dir))
    pc_states = [s.state for s in check_lattice(relat) if s.source_id == "pc-design"]
    assert pc_states == ["OK", "OK"]


def test_reconcile_preserves_concurrent_body_edit():
    text_initial = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nORIGINAL\n"
    # Simulate a concurrent body edit before the in-place write.
    text_fresh = text_initial.replace("ORIGINAL", "EDITED LATER")
    out, applied = apply_reconcile(text_fresh, {"a#x": "newhash"})
    assert "EDITED LATER" in out
    assert "seen: newhash" in out
    assert applied == {"a#x"}


def test_reconcile_node_skips_broken_edge(lattice_dir: Path):
    # gdd's only edge is broken; a node-level reconcile skips it without raising.
    lat = load_lattice(load_config(None, lattice_dir))
    assert reconcile(lat, "gdd", ref=None, reconcile_all=False) == {}


def test_reconcile_ref_targeting_broken_raises(lattice_dir: Path):
    # Aiming --ref directly at a broken edge is still refused.
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(BrokenRefError):
        reconcile(lat, "gdd", ref="ghost", reconcile_all=False)


def test_reconcile_node_with_stale_and_broken_reconciles_stale(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nsec body\n", encoding="utf-8")
    (docs / "d.md").write_text(
        "---\nid: d\nderives_from:\n"
        "  - ref: up#sec\n    seen: stalestalestalestalestalestale00\n"
        "  - ref: ghost\n---\n# D\nbody\n",
        encoding="utf-8",
    )
    lat = load_lattice(load_config(None, tmp_path))
    plan = reconcile(lat, "d", ref=None, reconcile_all=False)
    all_refs = _planned_refs(plan)
    assert "up#sec" in all_refs  # the stale edge is reconciled
    assert "ghost" not in all_refs  # the unrelated broken edge is skipped, not raised


def test_reconcile_unknown_id_raises(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(ValidationError) as exc_info:
        reconcile(lat, "does-not-exist", ref=None, reconcile_all=False)
    assert isinstance(exc_info.value, ProjectError)


def test_reconcile_ref_bare_matches_namespaced(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    # "accent" (bare) should match the stored ref "art-direction#accent" (namespaced)
    plan = reconcile(lat, "pc-design", ref="accent", reconcile_all=False)
    assert plan, "plan must be non-empty"
    all_refs = _planned_refs(plan)
    assert "art-direction#accent" in all_refs


def test_reconcile_all_skips_broken_and_ok(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    # Must not raise despite gdd's BROKEN edge
    plan = reconcile(lat, "", ref=None, reconcile_all=True)  # id ignored under reconcile_all
    all_refs = _planned_refs(plan)
    # pc-design's two drifting edges should be in the plan
    assert "art-direction#accent" in all_refs
    assert "art-direction#motion" in all_refs
    # gdd's broken ghost ref must NOT be in the plan
    assert "ghost" not in all_refs


def test_apply_reconcile_preserves_comments_key_order_and_untargeted_edges():
    # The only mutating command must rewrite just the targeted seen, leaving comments,
    # key order, and a second (untargeted) edge's seen intact. Guards against a regression
    # to a non-round-trip YAML dump.
    text = (
        "---\n"
        "id: d  # the node id\n"
        "derives_from:\n"
        "  - ref: a#x\n    seen: oldx\n"
        "  - ref: b#y\n    seen: oldy\n"
        "tickets: [T-1]\n"
        "---\n# Body\nkeep\n"
    )
    out, applied = apply_reconcile(text, {"a#x": "newx"})
    assert applied == {"a#x"}
    assert "seen: newx" in out
    assert "seen: oldy" in out  # the untargeted edge is untouched
    assert "# the node id" in out  # the comment survives
    assert out.index("id: d") < out.index("derives_from") < out.index("tickets")  # key order
    assert out.endswith("# Body\nkeep\n")


def test_apply_reconcile_no_change_when_seen_already_matches():
    # A planned ref whose seen already equals the new value is a no-op: not reported,
    # text returned unchanged.
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: same\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "same"})
    assert applied == set()
    assert out == text


def test_reconcile_ref_no_match_raises(lattice_dir: Path):
    # A --ref that names no edge on the node is reported, not a silent exit-0 no-op.
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(ValidationError):
        reconcile(lat, "pc-design", ref="does-not-exist", reconcile_all=False)


def test_reconcile_node_second_run_skips_already_ok_edges(lattice_dir: Path):
    # After a node is reconciled, a second single-node reconcile plans nothing, since
    # restamping an already-OK edge to the same hash is a no-op.
    project = load_config(None, lattice_dir)
    _apply_plan(reconcile(load_lattice(project), "pc-design", ref=None, reconcile_all=False))
    relat = load_lattice(load_config(None, lattice_dir))
    assert reconcile(relat, "pc-design", ref=None, reconcile_all=False) == {}


def test_reconcile_all_skips_already_ok_edge(lattice_dir: Path):
    # Make pc-design's accent edge OK, leave motion UNRECONCILED, then --all must plan
    # only motion (the OK edge is skipped at reconcile.py's new_seen == seen guard).
    project = load_config(None, lattice_dir)
    _apply_plan(reconcile(load_lattice(project), "pc-design", ref="accent", reconcile_all=False))
    relat = load_lattice(load_config(None, lattice_dir))
    plan = reconcile(relat, "", ref=None, reconcile_all=True)  # id ignored under reconcile_all
    refs = _planned_refs(plan)
    assert "art-direction#accent" not in refs  # already OK -> skipped
    assert "art-direction#motion" in refs  # still UNRECONCILED -> planned


def test_apply_reconcile_no_frontmatter_returns_unchanged():
    # No opening fence: a concurrent edit stripped the frontmatter entirely.
    text = "no frontmatter here\njust body\n"
    out, applied = apply_reconcile(text, {"a#x": "h"})
    assert out == text
    assert applied == set()


def test_apply_reconcile_empty_frontmatter_returns_unchanged():
    # An empty fence block (yaml.load -> None) is safe, not a crash.
    text = "---\n---\nbody\n"
    out, applied = apply_reconcile(text, {"a#x": "h"})
    assert out == text
    assert applied == set()


def test_apply_reconcile_non_list_derives_from_raises():
    # derives_from present but not a list is a distinct error branch from a non-mapping entry.
    text = "---\nid: d\nderives_from: oops\n---\nbody\n"
    with pytest.raises(UnreadableDocError) as exc_info:
        apply_reconcile(text, {"a#x": "h"})
    assert exc_info.value.code == "UNREADABLE_DOC"


def test_reconcile_ref_targeting_ok_edge_plans_nothing(lattice_dir: Path):
    # Reconcile accent to OK, then re-target it with --ref: the edge matched so this
    # must return an empty plan, NOT the no-match ValidationError.
    project = load_config(None, lattice_dir)
    _apply_plan(reconcile(load_lattice(project), "pc-design", ref="accent", reconcile_all=False))
    relat = load_lattice(load_config(None, lattice_dir))
    assert reconcile(relat, "pc-design", ref="accent", reconcile_all=False) == {}


def test_reconcile_all_plans_every_drifting_file(tmp_path: Path):
    # Two distinct drifting downstream nodes must each get their own path key under --all.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Up {#sec}\nbody\n", encoding="utf-8")
    (docs / "d1.md").write_text(
        "---\nid: d1\nderives_from:\n  - ref: up#sec\n---\n# D1\nx\n", encoding="utf-8"
    )
    (docs / "d2.md").write_text(
        "---\nid: d2\nderives_from:\n  - ref: up#sec\n---\n# D2\ny\n", encoding="utf-8"
    )
    lat = load_lattice(load_config(None, tmp_path))
    plan = reconcile(lat, "", ref=None, reconcile_all=True)
    assert {p.name for p in plan} == {"d1.md", "d2.md"}
    assert all("up#sec" in updates for updates in plan.values())


def test_reconcile_all_with_ref_filters_without_raising(lattice_dir: Path):
    # --all with --ref narrows edges across all nodes; the raise guards stay suppressed.
    lat = load_lattice(load_config(None, lattice_dir))
    plan = reconcile(lat, "", ref="accent", reconcile_all=True)
    refs = _planned_refs(plan)
    assert "art-direction#accent" in refs  # ref-matched edge planned
    assert "art-direction#motion" not in refs  # filtered out by ref, no raise
