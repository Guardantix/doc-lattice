"""Tests for load_lattice wiring."""

import json
import os
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from doc_lattice import loader, orchestrate
from doc_lattice.cache import CacheHit, CacheMiss, LookupPolicy, cache_path, lookup, store
from doc_lattice.cache.schema import Entry, reconstruct_facts
from doc_lattice.check import check_lattice, statuses_json, summarize_statuses
from doc_lattice.config import SidecarProjectConfig, load_config, load_sidecar_config
from doc_lattice.error_types import (
    DuplicateIdError,
    FrontmatterError,
    ManifestError,
    RegistrationConflictError,
    UnreadableDocError,
)
from doc_lattice.model import FileFacts, NodeMeta, ParsedDoc, TargetId
from doc_lattice.orchestrate import load_lattice
from doc_lattice.resolve import cached_target_hash


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


@pytest.mark.parametrize("trust_stat", [False, True])
@pytest.mark.parametrize("body", ["", "# Notes\n\n# Notes\n"], ids=["empty", "headings"])
@pytest.mark.parametrize(
    "case",
    [
        ("", "untracked", 1),
        ("---\nname: foreign\n---\n", "id-less", 4),
        ("---\n---\n", "untracked", 3),
        ("---\nscalar\n---\n", "untracked", 4),
        ("---\n- item\n---\n", "untracked", 4),
        ("\ufeff---\nname: foreign\n---\n", "id-less", 4),
        ("---\r\nname: foreign\r\n---\r\n", "id-less", 4),
        ("---\rname: foreign\r---\r", "id-less", 4),
    ],
)
def test_non_node_load_retains_body_and_provenance(
    tmp_path: Path, monkeypatch, trust_stat, body, case
):
    prefix, disposition, first_line = case
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "foreign.md").write_text(prefix + body, encoding="utf-8")
    cold_facts = []
    parse_facts = orchestrate._parse_file_facts

    def capture_facts(text, path):
        facts = parse_facts(text, path)
        cold_facts.append(facts)
        return facts

    monkeypatch.setattr(orchestrate, "_parse_file_facts", capture_facts)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        uncached = load_lattice(load_config(None, tmp_path))
        _with_cache(tmp_path, trust_stat=trust_stat)
        project = load_config(None, tmp_path)
        lattice = load_lattice(project)

    assert cold_facts[0] == cold_facts[1]
    assert lattice == uncached
    assert lattice.nodes_by_id == {}
    saved = json.loads(cache_path("testslot", os.environ).read_text(encoding="utf-8"))
    entry = saved["entries"]["docs/foreign.md"]
    assert entry["disposition"] == disposition
    assert "payload" in entry, "non-node entries must retain their file facts"
    payload = entry["payload"]
    assert payload["meta"] is None
    assert payload["body"] == body
    assert payload["body_first_line"] == first_line
    if body:
        assert [(r["start"], r["end"]) for r in payload["sections"]] == [(1, 2), (3, 3)]
        assert payload["sections"][0]["collision"] == [
            {"label": "Notes", "line": first_line},
            {"label": "Notes", "line": first_line + 2},
        ]
    else:
        assert payload["total_lines"] == 1
        assert payload["sections"] == []

    def forbidden(*_args, **_kwargs):
        pytest.fail("a warm load must retain non-node facts without parsing or deriving sections")

    hits = []
    resolve = lookup.resolve

    def capture_hit(*args):
        result = resolve(*args)
        assert isinstance(result, CacheHit)
        hits.append(result.facts)
        return result

    monkeypatch.setattr(orchestrate, "parse_document", forbidden)
    monkeypatch.setattr(orchestrate, "derive_file_sections", forbidden)
    monkeypatch.setattr(loader, "derive_file_sections", forbidden)
    monkeypatch.setattr(lookup, "resolve", capture_hit)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert load_lattice(project) == uncached
    assert hits == [cold_facts[0]]


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


@pytest.mark.parametrize("trust_stat", [False, True])
def test_cached_and_uncached_loads_are_structurally_equal(
    lattice_dir: Path, monkeypatch, tmp_path, trust_stat
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    uncached = load_lattice(load_config(None, lattice_dir))
    _with_cache(lattice_dir, trust_stat=trust_stat)
    cold = load_lattice(load_config(None, lattice_dir))  # writes the cache
    warm = load_lattice(load_config(None, lattice_dir))  # reads it back
    assert cold == uncached
    assert warm == uncached
    expected_hashes = {
        target: cached_target_hash(uncached, target, {}) for target in uncached.index
    }
    expected_statuses = check_lattice(uncached)
    expected_output = statuses_json(expected_statuses, summarize_statuses(expected_statuses))
    for lattice in (cold, warm):
        assert {target: cached_target_hash(lattice, target, {}) for target in lattice.index} == (
            expected_hashes
        )
        statuses = check_lattice(lattice)
        assert statuses_json(statuses, summarize_statuses(statuses)) == expected_output


def _hashes_for_facts(facts: FileFacts, path: Path):
    # This synthetic node exercises production whole-file and section hashing without adding
    # registration to the load path, which still leaves these foreign files out of the graph.
    lattice = loader.build_lattice(
        [ParsedDoc(path, NodeMeta(id="foreign"), facts.body, facts.sections)]
    )
    return {target: cached_target_hash(lattice, target, {}) for target in lattice.index}


def _stored_entry(slot: Path, key: str) -> Entry:
    snapshot = store.load(slot)
    assert snapshot.cache is not None
    return snapshot.cache.entries[key]


@pytest.mark.parametrize("trust_stat", [False, True])
@pytest.mark.parametrize("foreign_yaml", ["name: foreign\n", "scalar\n"])
def test_foreign_frontmatter_edit_refreshes_collision_lines_without_changing_hashes(
    tmp_path, monkeypatch, trust_stat, foreign_yaml
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "foreign.md"
    body = "Product A\n---------\n\n## Notes\none\n\n## Notes\ntwo\n"
    path.write_text(f"---\n{foreign_yaml}---\n{body}", encoding="utf-8")
    _with_cache(tmp_path, trust_stat=trust_stat)
    project = load_config(None, tmp_path)
    slot = cache_path("testslot", os.environ)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        load_lattice(project)
    before = _stored_entry(slot, "docs/foreign.md")
    original = reconstruct_facts(before)
    original_stat = path.stat()
    policy = LookupPolicy(str(tmp_path.resolve()), trust_stat)

    path.write_text(f"---\n{foreign_yaml}# added foreign line\n---\n{body}", encoding="utf-8")
    assert path.stat().st_size != original_stat.st_size
    assert isinstance(lookup.resolve(before, path, policy), CacheMiss)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        load_lattice(project)
    after = _stored_entry(slot, "docs/foreign.md")
    refreshed = reconstruct_facts(after)

    assert after.file_sha256 != before.file_sha256
    assert refreshed.parsed == original.parsed
    assert refreshed.body == original.body == body
    assert original.body_first_line == 4
    assert refreshed.body_first_line == 5
    assert [replace(r, collision=None) for r in refreshed.sections.sections] == [
        replace(r, collision=None) for r in original.sections.sections
    ]
    original_members = original.sections.sections[0].collision
    refreshed_members = refreshed.sections.sections[0].collision
    assert original_members is not None
    assert refreshed_members is not None
    assert [m.line for m in original_members] == [7, 10]
    assert [m.line for m in refreshed_members] == [8, 11]
    assert _hashes_for_facts(refreshed, path) == _hashes_for_facts(original, path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("the refreshed facts must survive the next warm load")

    monkeypatch.setattr(orchestrate, "parse_document", forbidden)
    monkeypatch.setattr(orchestrate, "derive_file_sections", forbidden)
    monkeypatch.setattr(loader, "derive_file_sections", forbidden)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        load_lattice(project)
    hit = lookup.resolve(after, path, policy)
    assert isinstance(hit, CacheHit)
    assert hit.facts == refreshed


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


@pytest.mark.parametrize("trust_stat", [False, True])
def test_the_misplacement_warning_replays_on_every_cache_tier(
    tmp_path: Path, monkeypatch, trust_stat
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "late.md").write_text("# Title\n\n<!-- doc-lattice\nid: late\n-->\n", encoding="utf-8")
    messages = [_warnings_from_load(tmp_path)]
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\ncache_key: parity\n"
        f"cache_trust_stat: {str(trust_stat).lower()}\n",
        encoding="utf-8",
    )
    project = load_config(None, tmp_path)

    for _ in range(2):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            load_lattice(project)
        messages.append([str(entry.message) for entry in captured])

    assert messages[0] == messages[1] == messages[2]
    assert any("misplaced doc-lattice envelope" in message for message in messages[0])


@pytest.mark.parametrize("trust_stat", [False, True])
def test_the_shadowed_envelope_warning_replays_on_every_cache_tier(
    tmp_path: Path, monkeypatch, trust_stat
):
    # The tracked half of the same contract: the file is a node, so the diagnostic rides beside
    # the disposition rather than replacing it, and the cache has to carry it either way.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "half.md").write_text(
        "---\nid: half\n---\n# Title\n\n<!-- doc-lattice\nid: other\n-->\n", encoding="utf-8"
    )
    messages = [_warnings_from_load(tmp_path)]
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots:\n  - docs\ncache_key: shadow\n"
        f"cache_trust_stat: {str(trust_stat).lower()}\n",
        encoding="utf-8",
    )
    project = load_config(None, tmp_path)

    for _ in range(2):
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


def _sidecar_project(tmp_path, *, cache=False, trust_stat=False, manifests=("nodes.yml",)):
    """Write sidecar config while leaving documents and manifests to the caller."""

    config = "lattice_format: 2\nsidecar_manifests:\n"
    config += "".join(f"  - {name}\n" for name in manifests)
    if cache:
        config += f"cache_key: testslot\ncache_trust_stat: {str(trust_stat).lower()}\n"
    (tmp_path / ".doc-lattice.yml").write_text(config, encoding="utf-8")
    return load_sidecar_config(None, tmp_path)


def _manifest(tmp_path, records, name="nodes.yml"):
    """Write literal record metadata using JSON, which is also valid YAML."""
    (tmp_path / name).write_text(json.dumps({"nodes": records}), encoding="utf-8")


@pytest.mark.parametrize("cache_policy", [None, False, True], ids=["uncached", "verify", "stat"])
@pytest.mark.parametrize("component_is_file", [False, True], ids=["missing", "not-directory"])
def test_external_collapsible_path_reads_target_and_retains_identity(
    tmp_path, monkeypatch, cache_policy, component_is_file
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    if component_is_file:
        (tmp_path / "component").write_text("not a directory\n")
    target = tmp_path / "doc.md"
    target.write_text("# Body\n")
    declared = "component/../doc.md"
    identity = tmp_path / declared
    _manifest(tmp_path, [{"path": declared, "meta": {"id": "external"}}])
    project = _sidecar_project(
        tmp_path, cache=cache_policy is not None, trust_stat=cache_policy is True
    )

    for require_verified in (False, False, True):
        lattice = load_lattice(project, require_verified=require_verified)
        node = lattice.nodes_by_id["external"]
        assert node.path == identity
        assert lattice.index[TargetId("external", "body")].path == identity
        assert node.body == "# Body\n"
        assert node.origin is not None
        assert node.origin.markdown_path == identity
        assert node.origin.declaration is not None
        assert node.origin.declaration.declared_path == declared
    if cache_policy is not None:
        snapshot = store.load(cache_path("testslot", os.environ))
        assert snapshot.cache is not None
        assert set(snapshot.cache.entries) == {declared}

    target.write_text("---\nderives_from: []\n---\n")
    with pytest.raises(FrontmatterError) as caught:
        load_lattice(project)
    assert caught.value.source == identity
    assert "nodes[0]" in "; ".join(caught.value.__notes__)


@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "---\nname: skill\ndescription: foreign\n---\n",
        "---\n---\n",
        "---\nscalar\n---\n",
        "---\n- item\n---\n",
        "\ufeff---\nname: skill\n---\n",
        "---\r\nname: skill\r\n---\r\n",
        "---\rname: skill\r---\r",
    ],
)
def test_external_enrollment_preserves_file_and_consumes_foreign_envelope(
    tmp_path, monkeypatch, cache, prefix
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "skill.md"
    data = (prefix + "# Skill\n\n## Detail {#detail}\n").encode()
    path.write_bytes(data)
    _manifest(tmp_path, [{"path": "./skill.md", "meta": {"id": "skill"}}])
    project = _sidecar_project(tmp_path, cache=cache)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        lattice = load_lattice(project)
    node = lattice.nodes_by_id["skill"]
    assert node.path == tmp_path / "skill.md"
    assert node.body == "# Skill\n\n## Detail {#detail}\n"
    assert node.origin is not None
    assert node.origin.declaration is not None
    assert node.origin.markdown_path == path
    assert node.origin.declaration.manifest_path == "nodes.yml"
    assert node.origin.declaration.record_index == 0
    assert node.origin.declaration.declared_path == "./skill.md"
    assert TargetId("skill", "detail") in lattice.index
    assert path.read_bytes() == data


@pytest.mark.parametrize(
    "text",
    [
        "---\nid: inline\n---\n# Body\n",
        "# Body\n<!-- doc-lattice\nid: inline\n-->\n",
        "---\nname: skill\n---\n<!-- doc-lattice\nid: inline\n-->\n",
    ],
)
def test_external_enrollment_refuses_other_ownership(tmp_path, text):

    (tmp_path / "skill.md").write_text(text)
    _manifest(tmp_path, [{"path": "skill.md", "meta": {"id": "skill"}}])
    with pytest.raises(RegistrationConflictError, match=r"nodes\[0\]"):
        load_lattice(_sidecar_project(tmp_path))


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("---\nname: skill\n", UnreadableDocError),
        ("---\nname: [\n---\n", UnreadableDocError),
        ("---\nderives_from: []\n---\n", FrontmatterError),
        ("---\nid: inline\nname: skill\n---\n", FrontmatterError),
        ("\ufeff<!-- doc-lattice\nid: inline\n-->\n", FrontmatterError),
    ],
)
def test_external_parser_errors_preserve_document_source_with_record_note(tmp_path, text, error):
    path = tmp_path / "skill.md"
    path.write_text(text)
    _manifest(tmp_path, [{"path": "skill.md", "meta": {"id": "skill"}}])
    with pytest.raises(error) as caught:
        load_lattice(_sidecar_project(tmp_path))
    assert caught.value.source == path
    assert "nodes[0]" in "; ".join(caught.value.__notes__)
    assert "nodes.yml" in "; ".join(caught.value.__notes__)


@pytest.mark.parametrize("trust_stat", [False, True])
def test_external_warm_hits_rejoin_manifest_metadata_and_origins(tmp_path, monkeypatch, trust_stat):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "skill.md").write_text("---\nname: foreign\n---\n# Body\n")
    record = {"path": "skill.md", "meta": {"id": "external", "title": "Before"}}
    _manifest(tmp_path, [record])
    project = _sidecar_project(tmp_path, cache=True, trust_stat=trust_stat)
    initial = load_lattice(project)
    initial_hash = cached_target_hash(initial, TargetId("external"), {})

    def forbidden(*_args, **_kwargs):
        pytest.fail("warm facts must be reused without parsing or section derivation")

    monkeypatch.setattr(orchestrate, "parse_document", forbidden)
    monkeypatch.setattr(orchestrate, "derive_file_sections", forbidden)
    monkeypatch.setattr(loader, "derive_file_sections", forbidden)
    assert load_lattice(project) == initial
    record["meta"]["title"] = "After"
    _manifest(tmp_path, [record])
    refreshed = load_lattice(project)
    assert refreshed.nodes_by_id["external"].title == "After"
    assert cached_target_hash(refreshed, TargetId("external"), {}) == initial_hash
    _manifest(tmp_path, [record], "moved.yml")
    moved_project = _sidecar_project(
        tmp_path, cache=True, trust_stat=trust_stat, manifests=("./moved.yml",)
    )
    moved = load_lattice(moved_project)
    moved_origin = moved.nodes_by_id["external"].origin
    assert moved_origin is not None
    assert moved_origin.declaration is not None
    assert moved_origin.declaration.manifest_path == "./moved.yml"
    assert cached_target_hash(moved, TargetId("external"), {}) == initial_hash


@pytest.mark.parametrize("trust_stat", [False, True])
def test_external_warm_content_and_foreign_metadata_refresh(tmp_path, monkeypatch, trust_stat):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "skill.md"
    path.write_text("---\nname: foreign\n---\n# A\n# A\n")
    _manifest(tmp_path, [{"path": "skill.md", "meta": {"id": "external"}}])
    project = _sidecar_project(tmp_path, cache=True, trust_stat=trust_stat)
    first = load_lattice(project)
    hash_ = cached_target_hash(first, TargetId("external"), {})
    path.write_text("---\nname: changed\ndescription: extra\n---\n# A\n# A\n")
    second = load_lattice(project)
    assert cached_target_hash(second, TargetId("external"), {}) == hash_
    assert [member.line for member in second.collisions[TargetId("external", "a")]] == [5, 6]
    path.write_text("---\nname: changed\n---\n# Different content\n")
    third = load_lattice(project)
    assert cached_target_hash(third, TargetId("external"), {}) != hash_


@pytest.mark.parametrize("trust_stat", [False, True])
def test_external_warm_enrollment_removal_restores_skip_warning(tmp_path, monkeypatch, trust_stat):

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "skill.md"
    path.write_text("---\nname: skill\n---\n# Body\n")
    _manifest(tmp_path, [{"path": "docs/skill.md", "meta": {"id": "external"}}])
    enrolled = _sidecar_project(tmp_path, cache=True, trust_stat=trust_stat)
    removed = SidecarProjectConfig(enrolled.project, ())
    with pytest.warns(UserWarning, match="declares no"):
        assert load_lattice(removed).nodes_by_id == {}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert set(load_lattice(enrolled).nodes_by_id) == {"external"}
    with pytest.warns(UserWarning, match="declares no"):
        assert load_lattice(removed).nodes_by_id == {}


@pytest.mark.parametrize("trust_stat", [False, True])
@pytest.mark.parametrize("transition", ["owner", "inline", "retarget", "delete"])
def test_external_warm_ownership_checks_run_again(tmp_path, monkeypatch, trust_stat, transition):

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("# Identical\n")
    b.write_text("# Identical\n")
    alias = tmp_path / "alias.md"
    alias.symlink_to(a)
    records = [
        {"path": "alias.md", "meta": {"id": "a"}},
        {"path": "b.md", "meta": {"id": "b"}},
    ]
    _manifest(tmp_path, records)
    project = _sidecar_project(tmp_path, cache=True, trust_stat=trust_stat)
    load_lattice(project)
    saved = cache_path("testslot", os.environ).read_bytes()
    if transition == "owner":
        records.append({"path": "a.md", "meta": {"id": "second-owner"}})
        _manifest(tmp_path, records)
    elif transition == "inline":
        a.write_text("---\nid: inline\n---\n# Identical\n")
    elif transition == "retarget":
        alias.unlink()
        alias.symlink_to(b)
    else:
        (tmp_path / "nodes.yml").unlink()
    error = ManifestError if transition == "delete" else RegistrationConflictError
    with pytest.raises(error):
        load_lattice(project)
    assert cache_path("testslot", os.environ).read_bytes() == saved


def test_registered_spelling_wins_over_earlier_alias_and_ignore_globs(tmp_path):

    docs = tmp_path / "docs"
    docs.mkdir()
    original = docs / "z.md"
    original.write_text("# Body\n")
    alias = docs / "a.md"
    alias.symlink_to(original)
    _manifest(tmp_path, [{"path": "./docs/z.md", "meta": {"id": "external"}}])
    project = _sidecar_project(tmp_path)
    assert load_lattice(project).nodes_by_id["external"].path == original
    ignored = SidecarProjectConfig(
        replace(
            project.project,
            config=project.project.config.model_copy(update={"ignore_globs": ["*.md"]}),
        ),
        project.sidecar_manifests,
    )
    assert load_lattice(ignored).nodes_by_id["external"].path == original


def test_external_and_inline_references_resolve_in_both_directions(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "inline.md").write_text(
        "---\nid: inline\nderives_from:\n  - ref: external#detail\n---\n# Inner {#inner}\n"
    )
    (tmp_path / "external.md").write_text("# Detail {#detail}\n")
    _manifest(
        tmp_path,
        [
            {
                "path": "external.md",
                "meta": {
                    "id": "external",
                    "derives_from": [{"ref": "inline#inner"}],
                },
            }
        ],
    )
    lattice = load_lattice(_sidecar_project(tmp_path))
    assert lattice.nodes_by_id["inline"].derives_from[0].target_id == TargetId("external", "detail")
    assert lattice.nodes_by_id["external"].derives_from[0].target_id == TargetId("inline", "inner")


@pytest.mark.parametrize("other_external", [False, True])
def test_external_duplicate_ids_name_markdown_and_manifest(tmp_path, other_external):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "external.md").write_text("# Body\n")
    records = [{"path": "external.md", "meta": {"id": "duplicate"}}]
    if other_external:
        (docs / "second.md").write_text("# Other\n")
        records.append({"path": "docs/second.md", "meta": {"id": "duplicate"}})
    else:
        (docs / "second.md").write_text("---\nid: duplicate\n---\n# Other\n")
    _manifest(tmp_path, records)
    with pytest.raises(DuplicateIdError) as caught:
        load_lattice(_sidecar_project(tmp_path))
    for text in ["external.md", "second.md", "nodes.yml", "nodes[0]"]:
        assert text in str(caught.value)
    if other_external:
        assert "nodes[1]" in str(caught.value)


def test_external_duplicate_explicit_anchors_name_manifest(tmp_path):
    (tmp_path / "external.md").write_text("# A {#repeat}\n# B {#repeat}\n")
    _manifest(tmp_path, [{"path": "external.md", "meta": {"id": "external"}}])
    with pytest.raises(DuplicateIdError, match=r"nodes\[0\]"):
        load_lattice(_sidecar_project(tmp_path))


def test_external_duplicate_edge_warning_names_manifest(tmp_path):
    (tmp_path / "external.md").write_text("# Body\n")
    _manifest(
        tmp_path,
        [
            {
                "path": "external.md",
                "meta": {
                    "id": "external",
                    "derives_from": [{"ref": "unknown"}, {"ref": "unknown"}],
                },
            }
        ],
    )
    with pytest.warns(UserWarning, match=r"nodes\[0\]"):
        lattice = load_lattice(_sidecar_project(tmp_path))
    assert len(lattice.nodes_by_id["external"].derives_from) == 1


@pytest.mark.parametrize("cache", [False, True])
@pytest.mark.parametrize("declared_path", ["external.md", "absent/../external.md"])
def test_external_decode_error_preserves_source_and_record_note(
    tmp_path, monkeypatch, cache, declared_path
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "external.md"
    path.write_bytes(b"\xff")
    _manifest(tmp_path, [{"path": declared_path, "meta": {"id": "external"}}])
    with pytest.raises(UnreadableDocError) as caught:
        load_lattice(_sidecar_project(tmp_path, cache=cache))
    assert caught.value.source == tmp_path / declared_path
    assert "nodes[0]" in "; ".join(caught.value.__notes__)


@pytest.mark.parametrize("trust_stat", [False, True])
def test_external_verified_load_bypasses_stat_staleness_without_persisting(
    tmp_path, monkeypatch, trust_stat
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    path = tmp_path / "external.md"
    path.write_text("# First\n")
    _manifest(tmp_path, [{"path": "external.md", "meta": {"id": "external"}}])
    project = _sidecar_project(tmp_path, cache=True, trust_stat=trust_stat)
    load_lattice(project)
    saved = cache_path("testslot", os.environ).read_bytes()
    stat = path.stat()
    path.write_text("# Other\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    verified = load_lattice(project, require_verified=True, persist_cache=False)
    assert verified.nodes_by_id["external"].body == "# Other\n"
    assert cache_path("testslot", os.environ).read_bytes() == saved


def _twin_files(first: Path, first_text: str, second: Path, second_text: str) -> None:
    """Write two distinct files whose size and nanosecond mtime both match."""
    assert len(first_text.encode()) == len(second_text.encode())
    first.write_text(first_text)
    second.write_text(second_text)
    ns = first.stat().st_mtime_ns
    os.utime(first, ns=(ns, ns))
    os.utime(second, ns=(ns, ns))


def test_retargeted_registered_symlink_misses_the_stat_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    targets = tmp_path / "targets"
    targets.mkdir()
    _twin_files(targets / "a.md", "# Alpha\n", targets / "b.md", "# Bravo\n")
    link = tmp_path / "link.md"
    link.symlink_to(Path("targets") / "a.md")
    _manifest(tmp_path, [{"path": "link.md", "meta": {"id": "external"}}])
    project = _sidecar_project(tmp_path, cache=True, trust_stat=True)
    assert load_lattice(project).nodes_by_id["external"].body == "# Alpha\n"
    link.unlink()
    link.symlink_to(Path("targets") / "b.md")
    assert load_lattice(project).nodes_by_id["external"].body == "# Bravo\n"


def test_retargeted_registered_symlink_cannot_hide_inline_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    targets = tmp_path / "targets"
    targets.mkdir()
    inline = "---\nid: inline\n---\n"
    _twin_files(targets / "a.md", " " * len(inline), targets / "b.md", inline)
    link = tmp_path / "link.md"
    link.symlink_to(Path("targets") / "a.md")
    _manifest(tmp_path, [{"path": "link.md", "meta": {"id": "external"}}])
    project = _sidecar_project(tmp_path, cache=True, trust_stat=True)
    load_lattice(project)
    link.unlink()
    link.symlink_to(Path("targets") / "b.md")
    with pytest.raises(RegistrationConflictError, match="already tracked by its inline metadata"):
        load_lattice(project)


def test_retargeted_discovered_symlink_misses_the_stat_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    targets = tmp_path / "targets"
    targets.mkdir()
    _twin_files(
        targets / "a.md",
        "---\nid: x\n---\n# Alpha\n",
        targets / "b.md",
        "---\nid: x\n---\n# Bravo\n",
    )
    link = docs / "link.md"
    link.symlink_to(Path("..") / "targets" / "a.md")
    _with_cache(tmp_path, trust_stat=True)
    project = load_config(None, tmp_path)
    assert load_lattice(project).nodes_by_id["x"].body == "# Alpha\n"
    link.unlink()
    link.symlink_to(Path("..") / "targets" / "b.md")
    assert load_lattice(project).nodes_by_id["x"].body == "# Bravo\n"


def test_manifest_registered_as_node_is_refused_before_reading(tmp_path):

    _manifest(tmp_path, [{"path": "nodes.md", "meta": {"id": "manifest"}}], "nodes.md")
    with pytest.raises(RegistrationConflictError, match="must never be a node"):
        load_lattice(_sidecar_project(tmp_path, manifests=("nodes.md",)))


@pytest.mark.parametrize("trust_stat", [False, True])
def test_cached_inline_alias_of_manifest_is_refused(tmp_path, monkeypatch, trust_stat):
    """Fresh valid manifests cannot parse inline; genuine stale stat facts can still be tracked."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    target = tmp_path / "nodes.yml"
    manifest = json.dumps({"nodes": [{"path": "external.md", "meta": {"id": "external"}}]})
    old = "---\nid: stale\n---\n" + " " * (len(manifest) - len("---\nid: stale\n---\n"))
    target.write_text(old)
    stat = target.stat()
    (docs / "alias.md").symlink_to(target)
    _with_cache(tmp_path, trust_stat=True)
    load_lattice(load_config(None, tmp_path))
    target.write_text(manifest)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    (tmp_path / "external.md").write_text("# Body\n")
    project = _sidecar_project(tmp_path, cache=True, trust_stat=True)
    if not trust_stat:
        # Force the same genuine cached facts through the acquisition boundary to exercise
        # the join independently of the policy's deliberate stale-stat allowance.
        resolve = lookup.resolve

        def stale_hit(entry, path, policy):
            return resolve(entry, path, replace(policy, trust_stat=True))

        monkeypatch.setattr(lookup, "resolve", stale_hit)
    with pytest.raises(RegistrationConflictError, match="inline node"):
        load_lattice(project, require_verified=not trust_stat)


@pytest.mark.parametrize(
    ("text", "explanation"),
    [
        ("---\nid: inline\n---\n# Body\n", "tracked"),
        ("# Body\n<!-- doc-lattice\nid: inline\n-->\n", "first line"),
        ("---\nid: inline\n---\n<!-- doc-lattice\nid: shadowed\n-->\n", "ignored"),
    ],
)
def test_external_ownership_refusal_explains_inline_classification(tmp_path, text, explanation):
    (tmp_path / "external.md").write_text(text)
    _manifest(tmp_path, [{"path": "external.md", "meta": {"id": "external"}}])
    with pytest.raises(RegistrationConflictError) as caught:
        load_lattice(_sidecar_project(tmp_path))
    assert explanation in str(caught.value)
    assert "external.md" in str(caught.value)
    assert "nodes[0]" in str(caught.value)
