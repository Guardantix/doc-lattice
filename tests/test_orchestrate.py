"""Tests for load_lattice wiring."""

import os
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from doc_lattice import orchestrate
from doc_lattice.cache import cache_path
from doc_lattice.config import load_config
from doc_lattice.error_types import DuplicateIdError, FrontmatterError, UnreadableDocError
from doc_lattice.model import TargetId
from doc_lattice.orchestrate import load_lattice


def test_load_lattice_from_dir(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"art-direction", "pc-design", "gdd"}
    assert lat.index[TargetId("art-direction", "accent")].kind == "section"
    # pc-design derives from accent and motion
    refs = {e.target_id for e in lat.nodes_by_id["pc-design"].derives_from}
    assert refs == {TargetId("art-direction", "accent"), TargetId("art-direction", "motion")}
    # gdd's ghost ref is unresolved
    assert lat.nodes_by_id["gdd"].derives_from[0].target_id is None


def test_files_without_frontmatter_skipped(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plain.md").write_text("# just prose\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a file with no fence is untracked prose, not a skip
        lat = load_lattice(project)
    assert lat.nodes_by_id == {}


def _corpus(tmp_path: Path) -> dict[str, Path]:
    """Write one file of each frontmatter tier, minus the fatal one.

    The typo'd node is left out so the corpus loads; the tests that need it add it themselves.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    paths = {
        "node": docs / "node.md",
        "prose": docs / "prose.md",
        "skillish": docs / "skillish.md",
    }
    paths["node"].write_text("---\nid: up\n---\n# Up\n", encoding="utf-8")
    paths["prose"].write_text("# just prose\n\nno fence at all\n", encoding="utf-8")
    paths["skillish"].write_text(
        "---\nname: some-skill\ndescription: non-lattice frontmatter\n---\n# Skill\n",
        encoding="utf-8",
    )
    return paths


def _warnings_from_load(tmp_path: Path) -> list[str]:
    """Load the project and return every warning message it emitted, in order."""
    with warnings.catch_warnings(record=True) as caught:
        # The default filter shows one warning per source location, and this module reloads the
        # same corpus repeatedly from one emission site. Without "always", whichever load ran
        # first would swallow the rest and the result would depend on test order.
        warnings.simplefilter("always")
        load_lattice(load_config(None, tmp_path))
    return [str(w.message) for w in caught]


def test_id_less_frontmatter_warns_and_is_skipped_without_changing_the_lattice(tmp_path: Path):
    paths = _corpus(tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lattice = load_lattice(load_config(None, tmp_path))

    assert set(lattice.nodes_by_id) == {"up"}
    assert [str(w.message) for w in caught] == [
        f"skipping {str(paths['skillish'])!r}: its frontmatter declares no 'id', so it is "
        "not a lattice node"
    ]


def test_cached_and_uncached_loads_reject_unclosed_frontmatter_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    broken = docs / "broken.md"
    broken.write_text("---\nid: vanished\n# Missing close\n", encoding="utf-8")

    with pytest.raises(UnreadableDocError) as uncached:
        load_lattice(load_config(None, tmp_path))

    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ncache_key: unclosed\n", encoding="utf-8"
    )
    with pytest.raises(UnreadableDocError) as cached:
        load_lattice(load_config(None, tmp_path))

    expected = f"unclosed YAML frontmatter in {str(broken)!r}: add a closing '---' fence"
    assert str(uncached.value) == expected
    assert str(cached.value) == expected


def test_duplicate_id_propagates(tmp_path: Path):
    # Two discovered files sharing an id must collide in the shared index through the
    # full discovery -> parse -> build seam, surfacing DuplicateIdError (exit 2).
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: dup\n---\n# A\n", encoding="utf-8")
    (docs / "b.md").write_text("---\nid: dup\n---\n# B\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    with pytest.raises(DuplicateIdError) as exc:
        load_lattice(project)
    assert exc.value.code == "DUPLICATE_ID"


@pytest.mark.parametrize(
    ("text", "exc_type", "code"),
    [
        ("---\nid: x\nlayer: [unterminated\n---\n# X\n", UnreadableDocError, "UNREADABLE_DOC"),
        ("---\nid: x\nbogus_key: 1\n---\n# X\n", FrontmatterError, "FRONTMATTER_ERROR"),
    ],
)
def test_load_lattice_surfaces_parse_errors(tmp_path: Path, text, exc_type, code):
    # The orchestrate loop has no try/except, so unparseable YAML and forbidden keys
    # must propagate rather than be silently skipped.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_text(text, encoding="utf-8")
    project = load_config(None, tmp_path)
    with pytest.raises(exc_type) as exc:
        load_lattice(project)
    assert exc.value.code == code


def test_load_lattice_surfaces_non_utf8(tmp_path: Path):
    # A non-UTF-8 doc must surface UnreadableDocError, not be quietly dropped.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc.md").write_bytes(b"---\nid: x\n---\n\xff\xfe not utf-8\n")
    project = load_config(None, tmp_path)
    with pytest.raises(UnreadableDocError) as exc:
        load_lattice(project)
    assert exc.value.code == "UNREADABLE_DOC"


def test_ignore_globs_exclude_nodes(tmp_path: Path):
    # orchestrate forwards project.config.ignore_globs into discovery; a configured
    # glob must remove the matching node from the assembled lattice end to end.
    docs = tmp_path / "docs"
    (docs / "drafts").mkdir(parents=True)
    (docs / "kept.md").write_text("---\nid: kept\n---\n# Kept\n", encoding="utf-8")
    (docs / "drafts" / "wip.md").write_text("---\nid: wip\n---\n# WIP\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        'lattice_format: 2\ndocs_roots: ["docs"]\nignore_globs: ["drafts/**"]\n', encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"kept"}


def test_multiple_docs_roots_combine(tmp_path: Path):
    # load_lattice must union docs from multiple configured roots into one node set
    # and shared id namespace.
    (tmp_path / "design").mkdir()
    (tmp_path / "production").mkdir()
    (tmp_path / "design" / "a.md").write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    (tmp_path / "production" / "b.md").write_text("---\nid: b\n---\n# B\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        'lattice_format: 2\ndocs_roots: ["design", "production"]\n', encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"a", "b"}


@pytest.mark.parametrize("cache_enabled", [False, True], ids=["uncached", "cached"])
def test_load_lattice_deduplicates_in_project_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cache_enabled: bool
):
    project_root = tmp_path / "repo"
    docs = project_root / "docs"
    shared = project_root / "shared"
    docs.mkdir(parents=True)
    shared.mkdir()
    target = shared / "spec.md"
    target.write_text("---\nid: linked\n---\n# Linked\n", encoding="utf-8")
    link = docs / "linked.md"
    link.symlink_to(Path("../shared/spec.md"))

    config_lines = ["lattice_format: 2", 'docs_roots: ["docs", "shared"]']
    if cache_enabled:
        config_lines.append("cache_key: symlink-test")
    (project_root / ".doc-lattice.yml").write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    lattice = load_lattice(load_config(None, project_root))

    assert set(lattice.nodes_by_id) == {"linked"}
    assert lattice.nodes_by_id["linked"].path == link


def _with_cache(tmp_path: Path, *, trust_stat: bool = False) -> Path:
    lines = ["lattice_format: 2", "cache_key: testslot"]
    if trust_stat:
        lines.append("cache_trust_stat: true")
    (tmp_path / ".doc-lattice.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_cached_and_uncached_loads_are_structurally_equal(lattice_dir: Path, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    uncached = load_lattice(load_config(None, lattice_dir))
    _with_cache(lattice_dir)
    cold = load_lattice(load_config(None, lattice_dir))  # writes the cache
    warm = load_lattice(load_config(None, lattice_dir))  # reads it back
    assert cold == uncached
    assert warm == uncached


def test_section_compatibility_is_structurally_equal_cold_and_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    project_root = tmp_path / "project"
    docs = project_root / "docs"
    docs.mkdir(parents=True)
    (docs / "compat.md").write_text(
        """---
id: compat
---
# Top
```
## Hidden
```
## Notes
## Notes
## Привет 你好
## Stable {#stable}
##
""",
        encoding="utf-8",
    )

    uncached = load_lattice(load_config(None, project_root))
    _with_cache(project_root)
    cold = load_lattice(load_config(None, project_root))

    def reject_derivation(_body: str):
        pytest.fail("a warm cache hit must not derive sections again")

    monkeypatch.setattr(orchestrate, "derive_file_sections", reject_derivation)
    warm = load_lattice(load_config(None, project_root))

    assert cold == uncached
    assert warm == uncached
    assert {
        target.anchor: location.span
        for target, location in warm.index.items()
        if target.file_id == "compat" and target.anchor is not None
    } == {
        "top": (1, 9),
        "notes": (5, 5),
        "notes-1": (6, 6),
        "привет-你好": (7, 7),
        "stable": (8, 8),
        "": (9, 9),
    }


def test_cache_disabled_leaves_env_untouched(lattice_dir: Path):
    # With no cache_key, load_lattice must never resolve or write a cache.
    project = load_config(None, lattice_dir)
    assert project.config.cache_key is None
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"art-direction", "pc-design", "gdd"}


def test_cached_cold_run_writes_the_cache_file(lattice_dir: Path, monkeypatch, tmp_path):
    # Proof the cached branch is genuinely taken: a cold run with cache_key set must write the
    # slot's cache file to disk. A no-op alias of the uncached path would leave it absent.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _with_cache(lattice_dir)  # writes cache_key: testslot
    assert not cache_path("testslot", os.environ).exists()
    load_lattice(load_config(None, lattice_dir))
    assert cache_path("testslot", os.environ).exists()


def test_cached_load_uses_resolved_project_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    project_dir = tmp_path / "project"
    docs = project_dir / "docs"
    docs.mkdir(parents=True)
    (docs / "node.md").write_text("---\nid: node\n---\n# Node\n", encoding="utf-8")
    _with_cache(project_dir)

    project = load_config(None, project_dir)
    monkeypatch.chdir(tmp_path)
    project = replace(project, project_root=Path("project"))

    assert set(load_lattice(project).nodes_by_id) == {"node"}


def test_mixed_directory_and_file_docs_roots_load_identically_cached_and_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # GTX-1: a docs_roots entry naming a single .md file (not a directory) must resolve
    # through _load_uncached and _load_cached identically -- same node ids, document paths,
    # and edges -- cold and warm.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "spec.md").write_text(
        "---\nid: spec\nauthority: binding\n---\n# Spec\nbody\n", encoding="utf-8"
    )
    arch = tmp_path / "ARCHITECTURE.md"
    arch.write_text(
        "---\nid: arch\nauthority: derived\nderives_from: [{ref: spec}]\n---\n"
        "# Architecture\nbody\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".doc-lattice.yml"

    config_path.write_text(
        "lattice_format: 2\ndocs_roots: [docs, ARCHITECTURE.md]\n", encoding="utf-8"
    )
    uncached = load_lattice(load_config(None, tmp_path))

    config_path.write_text(
        "lattice_format: 2\ndocs_roots: [docs, ARCHITECTURE.md]\ncache_key: mixed-file-root\n",
        encoding="utf-8",
    )
    cold = load_lattice(load_config(None, tmp_path))  # writes the cache
    warm = load_lattice(load_config(None, tmp_path))  # reads it back

    assert set(uncached.nodes_by_id) == {"spec", "arch"}
    assert uncached.nodes_by_id["arch"].path == arch
    refs = {e.target_id for e in uncached.nodes_by_id["arch"].derives_from}
    assert refs == {TargetId("spec")}
    assert cold == uncached
    assert warm == uncached


def test_warm_cached_run_reparses_nothing(lattice_dir: Path, monkeypatch, tmp_path):
    # Proof the warm path serves from the cache instead of re-parsing: after a cold run populates
    # the cache, a warm run must call parse_document zero times (every file is a verify-tier hit
    # reconstructed from the cache). A no-op alias would re-parse every discovered node.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _with_cache(lattice_dir)

    calls = {"n": 0}
    real = orchestrate.parse_document

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orchestrate, "parse_document", counting)

    load_lattice(load_config(None, lattice_dir))  # cold: cache empty, every node parsed
    assert calls["n"] > 0

    calls["n"] = 0
    load_lattice(load_config(None, lattice_dir))  # warm: every node served from the cache
    assert calls["n"] == 0


def test_id_less_warning_is_identical_uncached_cold_and_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # AD-12: the cache accelerates a load, it does not change what the load reports. A warning
    # raised as a parser side effect would fire on the uncached and cold runs and vanish on the
    # warm one, because a warm run never reaches the parser.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _corpus(tmp_path)

    uncached = _warnings_from_load(tmp_path)
    _with_cache(tmp_path)
    cold = _warnings_from_load(tmp_path)  # writes the cache
    warm = _warnings_from_load(tmp_path)  # every file served from it

    assert len(uncached) == 1
    assert cold == uncached
    assert warm == uncached


def test_id_less_warning_survives_a_stat_tier_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The stat tier never opens the file, so the disposition can only come from the cache entry.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _corpus(tmp_path)
    _with_cache(tmp_path, trust_stat=True)

    cold = _warnings_from_load(tmp_path)
    warm = _warnings_from_load(tmp_path)

    assert len(cold) == 1
    assert warm == cold


def test_id_less_warning_names_the_current_checkouts_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A cache slot is shared across checkouts, so the entry stores the disposition and the
    # message is rendered from the path this run discovered. Persisting the rendered text would
    # replay the first checkout's path in the second.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_paths = _corpus(first)
    second_paths = _corpus(second)
    _with_cache(first)
    _with_cache(second)  # the same cache_key, so both checkouts share one slot

    _warnings_from_load(first)  # fills the shared slot from the first checkout
    from_second = _warnings_from_load(second)

    assert len(from_second) == 1
    assert str(second_paths["skillish"]) in from_second[0]
    assert str(first_paths["skillish"]) not in from_second[0]


# A tracked node whose frontmatter defines one anchor name twice. The anchor is on two scalar
# fields rather than on `derives_from` entries so the load emits this diagnostic alone, with no
# duplicate-edge warning from `build_lattice` mixed into the assertions below.
ANCHORED_DOC = "---\nid: anchored\ntitle: &t Anchored\nlayer: &t design\n---\n# Anchored\n"


def _anchored_corpus(root: Path) -> Path:
    """Write a one-node corpus whose frontmatter defines an anchor name twice."""
    docs = root / "docs"
    docs.mkdir()
    path = docs / "anchored.md"
    path.write_text(ANCHORED_DOC, encoding="utf-8")
    return path


def test_reused_anchor_warning_is_identical_uncached_cold_and_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The same AD-12 property the id-less skip has, for the second cached diagnostic. ruamel
    # raises its own warning as a parse side effect, so left alone it fired on the uncached and
    # cold runs and vanished on the warm one that never reaches the parser (AD-29, AD-33).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    path = _anchored_corpus(tmp_path)

    uncached = _warnings_from_load(tmp_path)
    _with_cache(tmp_path)
    cold = _warnings_from_load(tmp_path)  # writes the cache
    warm = _warnings_from_load(tmp_path)  # the node is served from it

    assert len(uncached) == 1
    assert str(path) in uncached[0]
    assert "defines an anchor name more than once" in uncached[0]
    assert cold == uncached
    assert warm == uncached


def test_reused_anchor_warning_survives_a_stat_tier_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The stat tier never opens the file, so the flag can only come from the cache entry.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _anchored_corpus(tmp_path)
    _with_cache(tmp_path, trust_stat=True)

    cold = _warnings_from_load(tmp_path)
    warm = _warnings_from_load(tmp_path)

    assert len(cold) == 1
    assert warm == cold


def test_reused_anchor_warning_names_the_current_checkouts_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The entry stores the fact, never the rendered message, so a shared cache slot cannot
    # replay the first checkout's path in the second.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_path = _anchored_corpus(first)
    second_path = _anchored_corpus(second)
    _with_cache(first)
    _with_cache(second)  # the same cache_key, so both checkouts share one slot

    _warnings_from_load(first)
    from_second = _warnings_from_load(second)

    assert len(from_second) == 1
    assert str(second_path) in from_second[0]
    assert str(first_path) not in from_second[0]


def test_a_quiet_document_reports_no_reused_anchor_warning(tmp_path: Path):
    # The diagnostic is not merely always-on: an ordinary node says nothing.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plain.md").write_text("---\nid: plain\ntitle: Plain\n---\n# Plain\n", encoding="utf-8")

    assert _warnings_from_load(tmp_path) == []


@pytest.mark.filterwarnings("ignore:(?s)skipping .*declares no 'id'")
def test_id_less_frontmatter_declaring_lattice_intent_fails_identically_across_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The typo this issue is about: `idd` plus a live edge. The node and its edge would both
    # vanish, so it is a tool error rather than a skip, and a warm run must not cache its way
    # past it.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    _corpus(tmp_path)
    typo = tmp_path / "docs" / "typo.md"
    typo.write_text("---\nidd: down\nderives_from:\n  - ref: up\n---\n# Down\n", encoding="utf-8")
    expected = (
        f"frontmatter in {str(typo)!r} declares 'derives_from' but has no 'id' key, so the "
        "file and every edge it declares would be dropped from the lattice; add an 'id' "
        "(check it for a typo) or remove the lattice keys"
    )

    with pytest.raises(FrontmatterError) as uncached:
        load_lattice(load_config(None, tmp_path))

    _with_cache(tmp_path)
    with pytest.raises(FrontmatterError) as cold:
        load_lattice(load_config(None, tmp_path))
    with pytest.raises(FrontmatterError) as warm:
        load_lattice(load_config(None, tmp_path))

    assert str(uncached.value) == expected
    assert str(cold.value) == expected
    assert str(warm.value) == expected


def test_a_comment_spelling_document_becomes_a_node(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text(
        "<!-- doc-lattice\nid: up\n-->\n# Up\n\n## Section\nbody\n", encoding="utf-8"
    )
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#section\n---\n# Down\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)

    lattice = load_lattice(project)

    assert set(lattice.nodes_by_id) == {"up", "down"}
    assert TargetId("up", "section") in lattice.index


def test_the_misplacement_warning_replays_on_every_cache_tier(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "late.md").write_text("# Title\n\n<!-- doc-lattice\nid: late\n-->\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\ncache_key: parity\ncache_trust_stat: true\n",
        encoding="utf-8",
    )
    project = load_config(None, tmp_path)

    messages = []
    for _ in range(3):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            load_lattice(project)
        messages.append([str(entry.message) for entry in captured])

    assert messages[0] == messages[1] == messages[2]
    assert any("misplaced doc-lattice envelope" in message for message in messages[0])


def test_the_shadowed_envelope_warning_replays_on_every_cache_tier(tmp_path: Path, monkeypatch):
    # The tracked half of the same contract: the file is a node, so the diagnostic rides beside
    # the disposition rather than replacing it, and the cache has to carry it either way.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "half.md").write_text(
        "---\nid: half\n---\n# Title\n\n<!-- doc-lattice\nid: other\n-->\n", encoding="utf-8"
    )
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\ncache_key: shadow\ncache_trust_stat: true\n",
        encoding="utf-8",
    )
    project = load_config(None, tmp_path)

    messages = []
    for _ in range(3):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            lattice = load_lattice(project)
        messages.append([str(entry.message) for entry in captured])

    # Tracked under the fence's id on every tier, not the envelope's.
    assert set(lattice.nodes_by_id) == {"half"}
    assert messages[0] == messages[1] == messages[2]
    assert any("shadowed doc-lattice envelope" in message for message in messages[0])


def test_the_shadowed_and_misplaced_warnings_are_separately_filterable():
    # README documents PYTHONWARNINGS=ignore:misplaced as targeting exactly one diagnostic, and
    # that filter matches on a message prefix. The two envelope warnings therefore have to open
    # with different words, or silencing one silently silences the other.
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        orchestrate._report_misplaced_envelope("misplaced-envelope", Path("a.md"))
        orchestrate._report_shadowed_envelope(True, Path("b.md"))

    first, second = (str(entry.message) for entry in captured)
    assert first.startswith("misplaced ")
    assert second.startswith("shadowed ")
