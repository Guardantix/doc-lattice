"""Tests for load_lattice wiring."""

from pathlib import Path

from game_lattice.config import load_config
from game_lattice.orchestrate import load_lattice


def test_load_lattice_from_dir(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"art-direction", "pc-design", "gdd"}
    assert lat.index["accent"].kind == "section"
    # pc-design derives from accent and motion
    refs = {e.target_id for e in lat.nodes_by_id["pc-design"].derives_from}
    assert refs == {"accent", "motion"}
    # gdd's ghost ref is unresolved
    assert lat.nodes_by_id["gdd"].derives_from[0].target_id is None


def test_files_without_frontmatter_skipped(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plain.md").write_text("# just prose\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    lat = load_lattice(project)
    assert lat.nodes_by_id == {}
