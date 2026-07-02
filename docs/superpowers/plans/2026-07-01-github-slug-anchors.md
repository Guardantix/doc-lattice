# GitHub-Slug Anchors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve `derives_from` section refs against GitHub-native heading slugs (retiring the mandatory `{#slug}` marker), with markers kept as a precedence-winning escape hatch and section resolution made file-scoped through a typed `TargetId`.

**Architecture:** Add a pure `github_slug` port of `github-slugger` plus a document-order de-duping pass in `sections.py`; introduce a typed `TargetId(file_id, anchor)` key in `model.py` and re-key the whole `Lattice` (index, dependents, ancestors, anchors_by_path) on it; migrate every command that reads a resolved id, including active `TargetId` bridging in `stale_shipped.py`'s Linear trigger builders. Resolution moves from a flat global namespace to per-file scoping: `parse_ref` replaces `split_ref`, and a bare ref now resolves only to a file id.

**Tech Stack:** Python 3.14+, `uv`, `pytest` (+`hypothesis`), `pydantic` v2, `ruff`, `ty`, `ruamel.yaml`, `typer`, `rich`.

**Binding spec:** `docs/superpowers/specs/2026-07-01-github-slug-anchors-design.md`. When code and spec disagree, the spec wins.

## Global Constraints

Every task's requirements implicitly include these (copied from the spec and `CLAUDE.md`):

- Python 3.14+; run everything through `uv` (`uv run --group dev pytest`, `uv run --group dev ruff ...`, `uv run --group dev ty check src`).
- Coverage gate: full suite must stay at or above 80 percent (`pytest` enforces it).
- `typing.Any` and `typing.cast` are allowed ONLY in boundary modules (stem `boundary`/`adapter`/`parser`/`validator`/`external`/`inbound`). `model.py`, `sections.py`, `loader.py`, `impact.py`, `stale_shipped.py` are NOT boundary modules: no `Any`, no `cast`.
- All custom exceptions extend `ProjectError` and carry a `code`; no bare `except Exception`/`except BaseException`. Messages name the file and the fix.
- Constants use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py`; import them, do not duplicate string literals.
- No `datetime.now()`/`utcnow()` outside `datetime_utils.py`.
- ruff line length 100; module docstring on every module; Google-style docstrings on public functions; NO em-dashes anywhere in drafted content (docstrings, messages, comments).
- Narrow type suppressions are `# ty: ignore[code]` (ty-native), NOT mypy-style `# type: ignore`.
- Run tests with `env -u FORCE_COLOR` to match CI when asserting on human (rich) output; `conftest.py` already scrubs `FORCE_COLOR`, so plain `uv run --group dev pytest` is fine.
- A pre-commit hook runs ruff (`--fix`), ruff-format, `ty`, the typing-boundary check, the version-sync check, and detect-secrets, and blocks direct commits to `main`. Work on a feature branch. If a hook auto-fixes a file, re-stage and re-commit.

**Branch:** all commits land on `spec/github-slug-anchors` (already checked out; it holds the spec). Do not commit to `main`.

---

## File Structure

Source files touched (all under `src/game_lattice/`):

| File | Responsibility change |
| --- | --- |
| `sections.py` | ADD `github_slug`, `_Slugger`, `anchor_ids`. `build_toc`, `Heading`, `section_span`, `section_text` unchanged. |
| `model.py` | ADD `TargetId`, `parse_ref`, `NodeMeta.id` `#`-guard. RETYPE `Edge.target_id`, `Edge.resolve`, the `Lattice` maps. REMOVE `split_ref`. |
| `loader.py` | Register every heading via `anchor_ids` as a `TargetId(file_id, anchor)`; TargetId-keyed maps; per-file duplicate scoping; `_resolve_edges` dedups on `parse_ref`. |
| `resolve.py` | `target_content(lattice, target_id: TargetId)`; error message uses `TargetId.as_ref()`. |
| `check.py` | `EdgeStatus.target_id: TargetId | None` (annotation only). |
| `impact.py` | `expand_targets -> set[TargetId]`; `parse_ref` token parsing; bridge node id to `TargetId(node_id)` mid-walk. |
| `render.py` | `stale_edges: set[tuple[str, TargetId]]` (annotations only). |
| `reconcile.py` | Match edges with `parse_ref` equality; import `parse_ref`. |
| `lint.py` | `LadderViolation.target_id`/`SkippedEdge.target_id: TargetId`; `_target_authority(lattice, target_id: TargetId)`. |
| `stale_shipped.py` | Active `TargetId` bridging in `build_audit_trigger` and `build_from_trigger`. |
| `cli.py` | `check --json` / `lint --json` serialize `target_id` via `.as_ref()`. |
| `error_types.py` | Reword `DuplicateIdError` docstring (flat namespace to file-scoped). |

Docs touched: `CLAUDE.md`, `README.md`.

Test files touched: `test_sections.py` (ADD), `test_model.py`, `test_loader.py`, `test_resolve.py`, `test_impact.py`, `test_render.py`, `test_reconcile.py`, `test_orchestrate.py`, `test_lint.py`, `test_stale_shipped.py`, `test_cli.py`. `conftest.py`'s `lattice_dir` is intentionally left unchanged (its markers still win; verified in Task 3).

---

## Task 1: GitHub slug and document-order de-duping (`sections.py`)

Pure, additive, fully green on its own. No consumer wired yet.

**Files:**
- Modify: `src/game_lattice/sections.py`
- Test: `tests/test_sections.py`

**Interfaces:**
- Consumes: `Heading` (existing: `level`, `text`, `anchor: str | None`, `line`).
- Produces:
  - `github_slug(text: str) -> str`: the `github-slugger` slug of one heading's text (no de-dup).
  - `anchor_ids(headings: list[Heading]) -> list[str]`: one addressable id per heading in document order, the explicit marker when present, else the de-duped GitHub slug. Every heading (marker or not) reserves its GitHub slug in the shared occurrence counter, matching GitHub byte-for-byte.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_sections.py`, and add `anchor_ids, github_slug` to the existing `from game_lattice.sections import ...` line:

```python
@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Slot table", "slot-table"),
        ("3.2 Slot table", "32-slot-table"),  # '.' stripped, '3' and '2' join
        ("5.7 Capability", "57-capability"),
        ("Hello, World!", "hello-world"),  # punctuation stripped
        ("A  B", "a--b"),  # runs are NOT collapsed; each space becomes one hyphen
        ("well-known term", "well-known-term"),  # existing hyphens preserved
        ("snake_case name", "snake_case-name"),  # underscores preserved
        ("Fast⚡Mode", "fastmode"),  # emoji/symbol stripped, no adjacent space
        ("Overview", "overview"),
    ],
)
def test_github_slug_matches_github_rules(text, slug):
    assert github_slug(text) == slug


def test_anchor_ids_uses_marker_when_present_else_slug():
    toc = build_toc("# Intro {#custom}\n\n## Slot table\nx\n")
    assert anchor_ids(toc) == ["custom", "slot-table"]


def test_anchor_ids_dedupes_repeated_slugs_in_document_order():
    toc = build_toc("## Notes\n\n## Notes\n\n## Notes\n")
    assert anchor_ids(toc) == ["notes", "notes-1", "notes-2"]


def test_anchor_ids_marker_heading_reserves_its_github_slug():
    # GitHub slugs '## Notes {#n}' from its literal, marker-included text to 'notes-n' and
    # reserves it; a later '## Notes n' then collides and becomes 'notes-n-1'. Reserving the
    # marker heading's slug keeps game-lattice byte-parity with GitHub in this mixed case.
    toc = build_toc("## Notes {#n}\n\n## Notes n\nx\n")
    assert anchor_ids(toc) == ["n", "notes-n-1"]


def test_anchor_ids_empty_toc_is_empty():
    assert anchor_ids([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_sections.py -k "github_slug or anchor_ids" -v`
Expected: FAIL with `ImportError: cannot import name 'anchor_ids'` (and `github_slug`).

- [ ] **Step 3: Implement `github_slug`, `_Slugger`, and `anchor_ids`**

`sections.py` already has `import re`. Add this regex constant next to the other module-level regexes (after `_FENCE_RE`):

```python
# Characters github-slugger strips: everything that is not a word char (Unicode letters,
# digits, underscore), a hyphen, or a space. Spaces are turned into hyphens afterward. This
# reproduces github-slugger's slug output for plain-text and emoji headings, which is what
# GitHub renders as a heading anchor; a heading with inline markup (links, images) whose
# rendered text differs from its source keeps an explicit {#marker} escape hatch.
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]")
```

Add these three definitions at the end of `sections.py`:

```python
def github_slug(text: str) -> str:
    """Return the github-slugger slug of a heading's text (without de-duping).

    Lowercases the text, strips punctuation, symbols, and emoji, then turns each space into
    a hyphen. Runs are not collapsed, matching github-slugger: two spaces become two hyphens.
    De-duping across a document is handled by ``anchor_ids``.

    Args:
        text: One heading's text (the marker, if any, is part of the text and is slugged).

    Returns:
        The lowercase, punctuation-stripped, hyphen-joined slug.
    """
    return _SLUG_STRIP_RE.sub("", text.lower()).replace(" ", "-")


class _Slugger:
    """Document-order slug de-duper mirroring github-slugger's occurrence counter.

    The first time a base slug appears it is emitted and reserved; each later appearance is
    suffixed ``-1``, ``-2``, and so on, and every emitted result is reserved so a later
    identical base cannot reuse it.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        """Return the unique slug for ``text`` given every slug emitted so far."""
        base = github_slug(text)
        result = base
        while result in self._seen:
            self._seen[base] += 1
            result = f"{base}-{self._seen[base]}"
        self._seen[result] = 0
        return result


def anchor_ids(headings: list[Heading]) -> list[str]:
    """Return one addressable anchor id per heading, in document order.

    A heading with an explicit ``{#marker}`` is addressed by its marker; every other heading
    is addressed by its de-duped GitHub slug. Every heading (marker or not) reserves its
    GitHub slug in the shared counter, so the markerless headings around a marker heading are
    suffixed exactly as GitHub would suffix them.

    Args:
        headings: The document TOC from ``build_toc``, in document order.

    Returns:
        A list of anchor ids positionally aligned with ``headings``.
    """
    slugger = _Slugger()
    ids: list[str] = []
    for heading in headings:
        unique = slugger.slug(heading.text)
        ids.append(heading.anchor if heading.anchor is not None else unique)
    return ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_sections.py -v`
Expected: PASS (new tests plus all existing `build_toc`/`section_*` tests, which are unchanged).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run --group dev ruff format src/game_lattice/sections.py tests/test_sections.py && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/game_lattice/sections.py tests/test_sections.py
git commit -m "feat: github_slug and document-order anchor_ids in sections"
```

---

## Task 2: `TargetId`, `parse_ref`, and the node-id guard (`model.py`)

Additive: `TargetId`, `parse_ref`, `as_ref`, and the `#`-in-id guard are new. `split_ref`, `Edge`, and the `Lattice` map types are left ALONE in this task (they migrate in Task 3), so the suite stays green.

**Files:**
- Modify: `src/game_lattice/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Produces:
  - `TargetId(file_id: str, anchor: str | None = None)`: frozen, hashable. `.as_ref() -> str` returns `"file"` or `"file#anchor"`.
  - `parse_ref(ref: str) -> TargetId`: splits on the last `#`; a bare ref is a file id.
  - `NodeMeta.id` rejects a value containing `#`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_model.py` (add `TargetId, parse_ref` to the `from game_lattice.model import (...)` block; keep the existing `split_ref` import for now):

```python
def test_parse_ref_namespaced_is_file_scoped():
    assert parse_ref("art-direction#accent") == TargetId("art-direction", "accent")


def test_parse_ref_bare_is_a_file_id():
    assert parse_ref("accent") == TargetId("accent")
    assert parse_ref("accent").anchor is None


def test_parse_ref_splits_on_last_hash():
    assert parse_ref("a#b#c") == TargetId("a#b", "c")


def test_target_id_as_ref_roundtrips():
    assert TargetId("save-format", "slot-table").as_ref() == "save-format#slot-table"
    assert TargetId("save-format").as_ref() == "save-format"


def test_target_id_is_hashable_and_frozen():
    tid = TargetId("f", "a")
    assert tid in {TargetId("f", "a")}  # hashable, value-equal
    with pytest.raises(AttributeError):
        tid.anchor = "b"  # ty: ignore[invalid-assignment]


def test_nodemeta_rejects_hash_in_id():
    with pytest.raises(PydanticValidationError):
        NodeMeta.model_validate({"id": "a#b"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_model.py -k "parse_ref or target_id or hash_in_id" -v`
Expected: FAIL with `ImportError: cannot import name 'TargetId'`.

- [ ] **Step 3: Implement `TargetId`, `parse_ref`, and the id guard**

In `model.py`, add `field_validator` to the pydantic import:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator
```

Add `TargetId` and `parse_ref` near the top (after the imports, replacing nothing yet). Put `TargetId` above `RawEdge`:

```python
@dataclass(frozen=True, slots=True)
class TargetId:
    """A resolved target: a whole file, or a file-scoped section anchor.

    ``anchor`` is None for a whole-file target; otherwise it names a section inside
    ``file_id``. The two halves are separate fields, so a file target and a section target
    can never be confused and the ``#`` separator is not overloaded inside a key.
    """

    file_id: str
    anchor: str | None = None

    def as_ref(self) -> str:
        """Return the canonical ref string: ``file`` or ``file#anchor``."""
        return self.file_id if self.anchor is None else f"{self.file_id}#{self.anchor}"


def parse_ref(ref: str) -> TargetId:
    """Parse a derives_from ref into a file-scoped TargetId.

    A ref containing ``#`` is a section ref: it splits on the last ``#`` into a file id and
    an anchor. A bare ref is a whole-file target. Parsing never consults the index and never
    fails; whether the TargetId actually resolves is decided by index membership in
    ``Edge.resolve``.

    Args:
        ref: A derives_from ref as written (``save-format#slot-table`` or ``save-format``).

    Returns:
        The TargetId the ref names.
    """
    if "#" in ref:
        file_id, anchor = ref.rsplit("#", 1)
        return TargetId(file_id, anchor)
    return TargetId(ref)
```

Add the id guard as a method inside `NodeMeta` (after the field declarations):

```python
    @field_validator("id")
    @classmethod
    def _id_has_no_hash(cls, value: str) -> str:
        """Reject a ``#`` in a node id; it separates a file id from a section anchor in a ref."""
        if "#" in value:
            msg = f"node id {value!r} must not contain '#'; '#' separates a file id from a section anchor"
            raise ValueError(msg)
        return value
```

Leave `split_ref`, `Edge`, and `Lattice` exactly as they are in this task.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_model.py -v`
Expected: PASS (new tests plus all existing `split_ref`/`Edge`/`NodeMeta` tests, still unchanged).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run --group dev ruff format src/game_lattice/model.py tests/test_model.py && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/game_lattice/model.py tests/test_model.py
git commit -m "feat: TargetId, parse_ref, and node-id hash guard"
```

---

## Task 3: File-scoped resolution migration (all consumers)

This is one atomic, cohesive change: re-keying the `Lattice` on `TargetId` forces every consumer over at once, so a half-migrated tree cannot be green. Do all source edits, then all test edits, then run the full gate. `split_ref` is removed here. Within-task, write the four genuinely-new behavior tests first (Steps 1 to 2) so their intent is captured before the mechanical migration.

**Files (source):** `model.py`, `loader.py`, `resolve.py`, `check.py`, `impact.py`, `render.py`, `reconcile.py`, `lint.py`, `stale_shipped.py`, `cli.py`.
**Files (test):** `test_model.py`, `test_loader.py`, `test_resolve.py`, `test_impact.py`, `test_render.py`, `test_reconcile.py`, `test_orchestrate.py`, `test_lint.py`, `test_stale_shipped.py`, `test_cli.py`.

**Interfaces (final signatures every step must agree on):**
- `Edge.target_id: TargetId | None`; `Edge.resolve(ref: str, seen: str | None, index: Mapping[TargetId, Location]) -> Edge`.
- `Lattice.index: Mapping[TargetId, Location]`, `dependents: Mapping[TargetId, frozenset[str]]`, `ancestors: Mapping[TargetId, tuple[TargetId, ...]]`, `anchors_by_path: Mapping[Path, frozenset[TargetId]]`, `file_id_by_path: Mapping[Path, str]` (unchanged).
- `target_content(lattice: Lattice, target_id: TargetId) -> str`.
- `expand_targets(lattice: Lattice, token: str) -> set[TargetId]`.
- `EdgeStatus.target_id: TargetId | None`; `LadderViolation.target_id: TargetId`; `SkippedEdge.target_id: TargetId`.
- `to_mermaid`/`to_dot`/`_graph_edges` take `stale_edges: set[tuple[str, TargetId]]`.

### New-behavior tests (TDD-first)

- [ ] **Step 1: Write the four new-behavior tests**

In `tests/test_loader.py` (add `from game_lattice.model import TargetId` to imports) add:

```python
def test_same_slug_in_two_files_does_not_collide():
    # The whole point of file-scoping: a plain '## Overview' in two files is two distinct ids.
    docs = [
        _doc("a.md", "## Overview\nx\n", id="a"),
        _doc("b.md", "## Overview\ny\n", id="b"),
    ]
    lat = build_lattice(docs)  # must not raise
    assert lat.index[TargetId("a", "overview")].kind == "section"
    assert lat.index[TargetId("b", "overview")].kind == "section"


def test_marker_equal_to_a_slug_in_same_file_collides():
    # Two headings in one file that resolve to the same anchor id are a real collision.
    docs = [_doc("a.md", "# Foo {#bar}\n\n## Bar\nx\n", id="a")]  # marker 'bar' == slug 'bar'
    with pytest.raises(DuplicateIdError):
        build_lattice(docs)


def test_bare_anchor_ref_is_broken_not_resolved():
    # A bare ref resolves only to a file id; a bare anchor that is not a file id is BROKEN.
    docs = [
        _doc("up.md", "## Accent\nx\n", id="up"),
        _doc("down.md", "b\n", id="down", derives_from=[RawEdge(ref="accent")]),
    ]
    lat = build_lattice(docs)
    assert lat.nodes_by_id["down"].derives_from[0].target_id is None  # BROKEN
    assert lat.nodes_by_id["down"].derives_from[0].target_ref == "accent"
```

In `tests/test_stale_shipped.py` add the `--from` file-target regression test (the gap Codex flagged; the audit add-back is already guarded by `test_target_scoping_includes_the_named_node_itself`):

```python
def test_from_mode_whole_file_dependent_has_refs():
    # leaf derives from the WHOLE FILE 'mid' (not a section). After a change to up#sec, mid is
    # affected, so mid's file target must be in the closure or leaf's justifying ref is dropped.
    up = _node("up", "# Up {#sec}\nbody\n")
    mid = _node("mid", "# Mid\nbody\n", derives=[("up#sec", None)])
    leaf = _node("leaf", "# Leaf\nb\n", derives=[("mid", None)], tickets=("PC-1",))
    lattice = build_lattice([up, mid, leaf])
    trigger = build_from_trigger(lattice, "up#sec")
    assert "leaf" in trigger
    assert trigger["leaf"] == ("mid",)  # the whole-file ref is the justifying ref
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run --group dev pytest tests/test_loader.py -k "same_slug_in_two_files or marker_equal or bare_anchor_ref" tests/test_stale_shipped.py -k "whole_file_dependent" -v`
Expected: FAIL (old flat-namespace loader still registers only markers; bare `accent` still resolves against the flat index; `stale_shipped` still string-keyed).

### Source migration

- [ ] **Step 3: Migrate `model.py` (`Edge`, `Lattice`, remove `split_ref`)**

Delete the entire `split_ref` function. Change `Edge` and `Lattice`:

```python
@dataclass(frozen=True, slots=True)
class Edge:
    """A resolved derives_from edge. ``target_id`` is None when the ref is broken."""

    target_ref: str
    target_id: TargetId | None
    seen: str | None

    @classmethod
    def resolve(cls, ref: str, seen: str | None, index: "Mapping[TargetId, Location]") -> "Edge":
        """Build an edge, resolving the ref so target_ref and target_id cannot disagree.

        Args:
            ref: The derives_from ref as written.
            seen: The locked hash from frontmatter, or None if never reconciled.
            index: The TargetId-to-Location index; a ref resolving to no id yields a broken edge.

        Returns:
            An Edge whose target_id is the resolved TargetId, or None when the ref is broken.
        """
        target_id = parse_ref(ref)
        return cls(target_ref=ref, target_id=target_id if target_id in index else None, seen=seen)
```

In `Lattice`, change the four map annotations (and refresh the docstring's "every stable id" wording to "every TargetId"):

```python
    nodes_by_id: Mapping[str, Node]
    index: Mapping[TargetId, Location]
    dependents: Mapping[TargetId, frozenset[str]]
    ancestors: Mapping[TargetId, tuple[TargetId, ...]]
    file_id_by_path: Mapping[Path, str]
    anchors_by_path: Mapping[Path, frozenset[TargetId]]
```

- [ ] **Step 4: Migrate `loader.py`**

Replace the imports and `build_lattice`/`_resolve_edges`/`_register`/`_record_ancestors` so registration keys on `TargetId` and uses `anchor_ids`. Full replacements:

Imports:

```python
from .model import Edge, Lattice, Location, Node, ParsedDoc, TargetId, parse_ref
from .sections import Heading, anchor_ids, build_toc, section_span, split_body_lines
```

`build_lattice` (replace the two loops and the path-index block):

```python
def build_lattice(docs: list[ParsedDoc]) -> Lattice:
    """Build the lattice from parsed docs.

    Args:
        docs: Tracked files with validated frontmatter and bodies.

    Returns:
        A Lattice with the TargetId index, nodes, reverse adjacency, and ancestor map.

    Raises:
        DuplicateIdError: If two file ids collide, or two headings in one file resolve to the
            same anchor id (a marker equal to a computed slug, or two equal markers).
    """
    index: dict[TargetId, Location] = {}
    sources: dict[TargetId, str] = {}
    ancestors: dict[TargetId, tuple[TargetId, ...]] = {}

    for doc in docs:
        file_id = doc.meta.id
        _register(
            TargetId(file_id),
            Location(path=doc.path, kind="file", span=(1, _line_count(doc.body))),
            index,
            sources,
            f"file {doc.path}",
        )
        toc = build_toc(doc.body)
        total_lines = _line_count(doc.body)
        anchored: list[tuple[int, Heading, TargetId]] = []
        spans: dict[TargetId, tuple[int, int]] = {}
        for i, (head, anchor) in enumerate(zip(toc, anchor_ids(toc), strict=True)):
            tid = TargetId(file_id, anchor)
            span = section_span(toc, i, total_lines)
            spans[tid] = span
            anchored.append((i, head, tid))
            _register(
                tid,
                Location(path=doc.path, kind="section", span=span),
                index,
                sources,
                f"anchor {tid.as_ref()!r} in {doc.path}",
            )
        _record_ancestors(anchored, spans, ancestors)

    nodes: dict[str, Node] = {}
    dependents: defaultdict[TargetId, set[str]] = defaultdict(set)
    for doc in docs:
        edges = _resolve_edges(doc, index)
        for edge in edges:
            if edge.target_id is not None:
                dependents[edge.target_id].add(doc.meta.id)
        nodes[doc.meta.id] = Node(
            id=doc.meta.id,
            title=doc.meta.title,
            layer=doc.meta.layer,
            authority=doc.meta.authority,
            path=doc.path,
            body=doc.body,
            derives_from=tuple(edges),
            tickets=tuple(doc.meta.tickets),
        )

    file_id_by_path = {node.path: node_id for node_id, node in nodes.items()}
    section_ids_by_path: defaultdict[Path, list[TargetId]] = defaultdict(list)
    for tid, loc in index.items():
        if loc.kind == "section":
            section_ids_by_path[loc.path].append(tid)
    anchors_by_path = {path: frozenset(section_ids_by_path[path]) for path in file_id_by_path}

    return Lattice(
        nodes_by_id=nodes,
        index=index,
        dependents={k: frozenset(v) for k, v in dependents.items()},
        ancestors=ancestors,
        file_id_by_path=file_id_by_path,
        anchors_by_path=anchors_by_path,
    )
```

Add `from pathlib import Path` to `loader.py` imports if not present (it is needed for the `defaultdict[Path, ...]` annotation). Check the top of the file; add `from pathlib import Path` under the stdlib imports if missing.

`_resolve_edges` (dedup on `parse_ref`, message via `as_ref()`):

```python
def _resolve_edges(doc: ParsedDoc, index: dict[TargetId, Location]) -> list[Edge]:
    """Resolve a node's derives_from entries to edges, deduped by resolved target.

    Edge identity is ``(source_node_id, resolved TargetId)``: a node that lists the same
    resolved target twice keeps only the last occurrence, last write wins on ``seen``, and a
    warning is raised. Resolution keys on the parsed TargetId even for a broken ref, so two
    refs to the same unresolved target collapse to one broken edge.

    Args:
        doc: The parsed source document.
        index: The TargetId-to-Location index for resolving refs.

    Returns:
        The node's edges in first-seen order, one per distinct resolved target.
    """
    deduped: dict[TargetId, Edge] = {}
    for raw in doc.meta.derives_from:
        target_id = parse_ref(raw.ref)
        if target_id in deduped:
            warnings.warn(
                f"node {doc.meta.id!r} derives from {target_id.as_ref()!r} more than once;"
                " keeping the last occurrence",
                stacklevel=2,
            )
        deduped[target_id] = Edge.resolve(raw.ref, raw.seen, index)
    return list(deduped.values())
```

`_register` signature/annotations (change `str` to `TargetId`); the body's duplicate-detection logic is unchanged:

```python
def _register(
    id_: TargetId,
    location: Location,
    index: dict[TargetId, Location],
    sources: dict[TargetId, str],
    where: str,
) -> None:
    """Record a TargetId in the shared index, failing if it collides with an existing one.

    ``sources`` tracks where each id was first seen so a duplicate names both registration
    sites in the error. A file id and a section id in different files never collide because
    their TargetIds differ; only a within-file anchor clash or a repeated file id does.
    """
    if id_ in index:
        msg = (
            f"duplicate id {id_.as_ref()!r}: already registered at {sources[id_]},"
            f" again at {where}"
        )
        raise DuplicateIdError(msg)
    index[id_] = location
    sources[id_] = where
```

`_record_ancestors` (retype the parameters; the containment logic is unchanged, keyed on `TargetId`):

```python
def _record_ancestors(
    anchored: list[tuple[int, Heading, TargetId]],
    spans: dict[TargetId, tuple[int, int]],
    ancestors: dict[TargetId, tuple[TargetId, ...]],
) -> None:
    """Record each anchor's enclosing anchored sections, outermost to innermost.

    A section encloses another when its span strictly contains the other's; ties on one
    boundary still count as enclosing. Editing a nested section propagates impact to
    dependents of its ancestors, so the order runs outermost first.
    """
    for _, _head, anchor in anchored:
        start, end = spans[anchor]
        containing: list[tuple[tuple[int, int], TargetId]] = []
        for _, _other, oid in anchored:
            if oid == anchor:
                continue
            ostart, oend = spans[oid]
            other_encloses = (ostart < start and oend >= end) or (ostart <= start and oend > end)
            if other_encloses:
                containing.append(((ostart, oend), oid))
        containing.sort(key=_span_width, reverse=True)
        ancestors[anchor] = tuple(oid for _, oid in containing)
```

`_span_width`'s annotation becomes `tuple[tuple[int, int], TargetId]`:

```python
def _span_width(span_and_id: tuple[tuple[int, int], TargetId]) -> int:
    """Return the line width (end minus start) of a ``(span, id)`` pair, for sorting."""
    (span_start, span_end), _ = span_and_id
    return span_end - span_start
```

- [ ] **Step 5: Migrate `resolve.py`**

Change the signature and error message of `target_content`:

```python
def target_content(lattice: Lattice, target_id: TargetId) -> str:
```

Add `TargetId` to the model import (`from .model import Lattice, Node, TargetId`). Change the broken-id message to use `as_ref()`:

```python
    location = lattice.index.get(target_id)
    if location is None:
        msg = f"ref resolves to unknown id {target_id.as_ref()!r}; fix the ref or add the anchor"
        raise BrokenRefError(msg)
```

- [ ] **Step 6: Migrate `check.py`**

Add `TargetId` to the import (`from .model import Edge, Lattice, TargetId`) and change one annotation on `EdgeStatus`:

```python
    target_id: TargetId | None
```

`_classify`'s body is unchanged (it already passes `edge.target_id` straight to `target_content`).

- [ ] **Step 7: Migrate `impact.py`**

Replace the imports and both functions:

```python
from .error_types import ValidationError
from .model import Lattice, Node, TargetId, parse_ref


def expand_targets(lattice: Lattice, token: str) -> set[TargetId]:
    """Expand an impact token into the full set of TargetIds it touches.

    Args:
        lattice: The built lattice.
        token: A bare file id or a ``file#anchor`` section ref.

    Returns:
        For a file id: the file target plus all section anchors in its file. For a section
        anchor: the anchor, its anchored ancestors, and the enclosing file target. Empty if
        the token resolves to no id.
    """
    target_id = parse_ref(token)
    location = lattice.index.get(target_id)
    if location is None:
        return set()
    if location.kind == "file":
        return {target_id} | lattice.anchors_by_path.get(location.path, frozenset())
    expanded = {target_id} | set(lattice.ancestors.get(target_id, ()))
    file_id = lattice.file_id_by_path.get(location.path)
    if file_id is not None:
        expanded.add(TargetId(file_id))
    return expanded


def impact(lattice: Lattice, token: str) -> list[Node]:
    """Return every downstream node affected by a change to ``token``.

    Args:
        lattice: The built lattice.
        token: A bare file id or a ``file#anchor`` section ref.

    Returns:
        Affected nodes, sorted by id, walking ``dependents`` transitively. An empty list
        means the id is known but has no dependents.

    Raises:
        ValidationError: If ``token`` resolves to no id in the lattice.
    """
    if parse_ref(token) not in lattice.index:
        msg = f"unknown impact target {token!r}; run check to list ids"
        raise ValidationError(msg)
    queue = list(expand_targets(lattice, token))
    visited_targets: set[TargetId] = set()
    affected: set[str] = set()
    while queue:
        current = queue.pop()
        if current in visited_targets:
            continue
        visited_targets.add(current)
        for source_id in lattice.dependents.get(current, frozenset()):
            if source_id in affected:
                continue
            affected.add(source_id)
            # A source node id is a whole-file target; bridge it to a TargetId to keep walking.
            queue.append(TargetId(source_id))
            node = lattice.nodes_by_id[source_id]
            queue.extend(lattice.anchors_by_path[node.path])
    return [lattice.nodes_by_id[node_id] for node_id in sorted(affected)]
```

- [ ] **Step 8: Migrate `render.py`**

Only annotations change (`_graph_edges` already looks up `edge.target_id` in the index and reads `file_id_by_path`). Add `TargetId` to the import (`from .model import Lattice, TargetId`) and change the three `set[tuple[str, str]]` annotations to `set[tuple[str, TargetId]]` in `_graph_edges`, `to_mermaid`, and `to_dot`:

```python
def _graph_edges(
    lattice: Lattice, stale_edges: set[tuple[str, TargetId]]
) -> list[tuple[str, str, bool]]:
```
```python
def to_mermaid(lattice: Lattice, stale_edges: set[tuple[str, TargetId]]) -> str:
```
```python
def to_dot(lattice: Lattice, stale_edges: set[tuple[str, TargetId]]) -> str:
```

- [ ] **Step 9: Migrate `reconcile.py`**

Change the import `from .model import Lattice, split_ref` to `from .model import Lattice, parse_ref`, and the ref-match line:

```python
            if ref is not None and parse_ref(edge.target_ref) != parse_ref(ref):
                continue
```

Update the docstring sentence that says "The match uses the resolved trailing id" to "The match uses the parsed TargetId so an identical ref selects the same edge." (`target_content(lattice, edge.target_id)` already takes a `TargetId`, no change.)

- [ ] **Step 10: Migrate `lint.py`**

Add `TargetId` to the model import (`from .model import Lattice, TargetId`). Change the two dataclass field annotations and `_target_authority`'s parameter:

```python
    target_id: TargetId  # in LadderViolation
```
```python
    target_id: TargetId  # in SkippedEdge
```
```python
def _target_authority(lattice: Lattice, target_id: TargetId) -> Authority | None:
```

The bodies are unchanged (`lattice.index[target_id]` already keys on the value it is given).

- [ ] **Step 11: Migrate `stale_shipped.py` (the Codex-flagged bridge)**

Add `TargetId` to the model import (`from .model import Lattice, TargetId`). Replace the add-back line in `build_audit_trigger` and the `closure` construction in `build_from_trigger`:

In `build_audit_trigger`, replace:

```python
        affected |= {tid for tid in expand_targets(lattice, target) if tid in lattice.nodes_by_id}
```

with the file-target bridge:

```python
        # expand_targets yields TargetIds; bridge whole-file targets back to node ids so the
        # target's own node is added to `affected`. Without this the filter is always-False
        # (a TargetId is never a str key) and a scoped audit drops the target's own tickets.
        affected |= {
            tid.file_id
            for tid in expand_targets(lattice, target)
            if tid.anchor is None and tid.file_id in lattice.nodes_by_id
        }
```

In `build_from_trigger`, replace the closure block:

```python
    affected = impact(lattice, from_id)
    closure: set[str] = set(expand_targets(lattice, from_id))
    for node in affected:
        closure.add(node.id)
        closure |= lattice.anchors_by_path.get(node.path, frozenset())
```

with the TargetId-typed closure and the whole-file bridge:

```python
    affected = impact(lattice, from_id)
    closure: set[TargetId] = set(expand_targets(lattice, from_id))
    for node in affected:
        # An affected node's whole file is in the closure; use its file target, not the bare
        # node id, so an edge deriving from the whole file matches `edge.target_id in closure`.
        closure.add(TargetId(node.id))
        closure |= lattice.anchors_by_path.get(node.path, frozenset())
```

The membership test `edge.target_id in closure` and the trigger-dict keys (node-id strings) and `Finding.drifted_refs` (raw `target_ref` strings) are unchanged.

- [ ] **Step 12: Migrate `cli.py` (`--json` serialization)**

In `check`, the JSON payload's `target_id` must serialize the `TargetId` (or None). Replace:

```python
                    "target_id": status.target_id,
```

with:

```python
                    "target_id": status.target_id.as_ref() if status.target_id else None,
```

In `lint`, do the same for both payload blocks. Replace the violations block's:

```python
                    "target_id": violation.target_id,
```

with:

```python
                    "target_id": violation.target_id.as_ref(),
```

and the skipped block's:

```python
                    "target_id": skipped.target_id,
```

with:

```python
                    "target_id": skipped.target_id.as_ref(),
```

The `graph` command's stale set (`{(s.source_id, s.target_id) ...}`) now holds `TargetId` values automatically and needs no change.

### Test migration

- [ ] **Step 13: Update `test_model.py`**

Remove the three `split_ref` tests (`test_split_ref_keys_on_trailing_id`, `test_split_ref_boundary_inputs`, `test_split_ref_strips_all_namespaces_and_is_idempotent`) and drop `split_ref` from the import. Update three coupled tests:

`test_edge_resolve_links_ref_to_index`:

```python
def test_edge_resolve_links_ref_to_index():
    index = {TargetId("art-direction", "accent"): Location(path=Path("a.md"), kind="section", span=(1, 2))}
    edge = Edge.resolve("art-direction#accent", "h", index)
    assert edge.target_ref == "art-direction#accent"
    assert edge.target_id == TargetId("art-direction", "accent")
    assert edge.seen == "h"
```

`test_dataclasses_are_frozen` (the `Edge(...)` line):

```python
    edge = Edge(target_ref="a#b", target_id=TargetId("a", "b"), seen=None)
```

`test_lattice_holds_maps` (the `index=` line):

```python
        index={TargetId("x"): Location(path=Path("x.md"), kind="file", span=(1, 1))},
```

- [ ] **Step 14: Update `test_loader.py`**

Import already has `TargetId` (added in Step 1). Apply these exact edits:

- `test_registers_file_and_anchor_ids`: `lat.index["a"]` -> `lat.index[TargetId("a")]`; `lat.index["sec"]` -> `lat.index[TargetId("a", "sec")]` (both occurrences).
- `test_resolves_edges_and_builds_dependents`: `edge.target_id == "accent"` -> `edge.target_id == TargetId("up", "accent")`; `lat.dependents["accent"]` -> `lat.dependents[TargetId("up", "accent")]`.
- `test_path_indexes_map_paths_to_ids`: `frozenset({"accent", "tone"})` -> `frozenset({TargetId("up", "accent"), TargetId("up", "tone")})`.
- Replace `test_anchor_collides_with_file_id_raises` entirely (its premise inverts: a section `a#b` and a file `b` no longer collide):

```python
def test_anchor_in_one_file_and_file_id_in_another_do_not_collide():
    # 'a#b' (a section in file a) and file id 'b' are distinct TargetIds: no collision.
    docs = [_doc("a.md", "# A {#b}\n", id="a"), _doc("b.md", "x\n", id="b")]
    lat = build_lattice(docs)  # must not raise
    assert lat.index[TargetId("a", "b")].kind == "section"
    assert lat.index[TargetId("b")].kind == "file"
```

- Replace `test_anchor_collides_with_anchor_in_other_file_raises` entirely (premise inverts to the acceptance criterion):

```python
def test_same_anchor_in_two_files_does_not_collide():
    docs = [
        _doc("a.md", "# A {#a-top}\n\n## Shared {#shared}\nx\n", id="a"),
        _doc("b.md", "# B {#b-top}\n\n## Shared {#shared}\nx\n", id="b"),
    ]
    lat = build_lattice(docs)  # must not raise
    assert lat.index[TargetId("a", "shared")].kind == "section"
    assert lat.index[TargetId("b", "shared")].kind == "section"
```

- `test_ancestors_computed_for_nested_anchor`: `lat.ancestors["child"] == ("parent",)` -> `lat.ancestors[TargetId("a", "child")] == (TargetId("a", "parent"),)`; `lat.ancestors["parent"] == ()` -> `lat.ancestors[TargetId("a", "parent")] == ()`.
- `test_duplicate_resolved_target_is_deduped_with_warning`: the two refs must resolve to the same TargetId to dedup. Change `derives_from` to two identical namespaced refs and update the assertions:

```python
            derives_from=[RawEdge(ref="up#accent", seen="h1"), RawEdge(ref="up#accent", seen="h2")],
```
```python
    with pytest.warns(UserWarning, match="derives from 'up#accent' more than once"):
        lat = build_lattice(docs)
    edges = lat.nodes_by_id["down"].derives_from
    assert len(edges) == 1
    assert edges[0].target_id == TargetId("up", "accent")
    assert edges[0].seen == "h2"
    assert lat.dependents[TargetId("up", "accent")] == frozenset({"down"})
```

- `test_two_broken_refs_to_same_id_collapse_to_one_edge`: both refs must parse to the same broken TargetId. Change to two identical bare refs and update the warning match:

```python
                RawEdge(ref="ghost", seen="h1"),
                RawEdge(ref="ghost", seen="h2"),
```
```python
    with pytest.warns(UserWarning, match="derives from 'ghost' more than once"):
```
(The `edges[0].target_id is None` and `"ghost" not in lat.dependents` assertions stay; `lat.dependents` is now TargetId-keyed but `"ghost"` is a str so the `not in` still holds.)

- `test_ancestors_ordered_outermost_to_innermost_and_siblings_excluded`: wrap every id in `TargetId("a", ...)`:

```python
    assert lat.ancestors[TargetId("a", "leaf")] == (TargetId("a", "top"), TargetId("a", "mid"))
    assert lat.ancestors[TargetId("a", "mid")] == (TargetId("a", "top"),)
    assert lat.ancestors[TargetId("a", "sib")] == (TargetId("a", "top"),)
    assert TargetId("a", "mid") not in lat.ancestors[TargetId("a", "sib")]
    assert lat.ancestors[TargetId("a", "top")] == ()
```

- `test_dependents_aggregates_multiple_sources`: d1's bare `accent` would now be BROKEN. Namespace it and update the assertion:

```python
        _doc("d1.md", "b\n", id="d1", derives_from=[RawEdge(ref="up#accent")]),
        _doc("d2.md", "b\n", id="d2", derives_from=[RawEdge(ref="up#accent")]),
```
```python
    assert lat.dependents[TargetId("up", "accent")] == frozenset({"d1", "d2"})
```

- `test_edges_keep_first_seen_order_with_dedup`: bare `accent`/`tone` become BROKEN and would not dedup against `up#accent`. Rewrite with namespaced refs so `up#accent` still dedups and `up#tone` stays distinct:

```python
            derives_from=[
                RawEdge(ref="up#accent", seen="a1"),
                RawEdge(ref="up#tone", seen="t1"),
                RawEdge(ref="up#accent", seen="a2"),  # later dup of up#accent
            ],
```
```python
    with pytest.warns(UserWarning, match="derives from 'up#accent' more than once"):
        lat = build_lattice(docs)
    edges = lat.nodes_by_id["d"].derives_from
    assert [e.target_id for e in edges] == [TargetId("up", "accent"), TargetId("up", "tone")]
    assert edges[0].seen == "a2"
```

- `test_empty_doc_set_builds_empty_lattice` and `test_empty_body_file_spans_single_line`: `lat.index["a"]` -> `lat.index[TargetId("a")]` (the empty-lattice test asserts `lat.index == {}`, unchanged).

- [ ] **Step 15: Update `test_resolve.py`**

Add `TargetId` to the model import. In `_lattice()`, key `index` and `anchors_by_path` on TargetId:

```python
        index={
            TargetId("doc"): Location(path=Path("doc.md"), kind="file", span=(1, 6)),
            TargetId("doc", "accent"): Location(path=Path("doc.md"), kind="section", span=(4, 6)),
        },
```
```python
        anchors_by_path={Path("doc.md"): frozenset({TargetId("doc", "accent")})},
```

Update the calls:
- `test_target_content_section`: `target_content(_lattice(), "accent")` -> `target_content(_lattice(), TargetId("doc", "accent"))`.
- `test_target_content_section_exact_via_build_lattice`: `target_content(lat, "accent")` -> `target_content(lat, TargetId("up", "accent"))`.
- `test_target_content_file_is_whole_body`: `target_content(lat, "doc")` -> `target_content(lat, TargetId("doc"))`.
- `test_target_content_broken_raises`: `target_content(_lattice(), "missing")` -> `target_content(_lattice(), TargetId("missing"))` (the `"missing" in str(exc)` assertion still holds via `as_ref()`).
- `test_node_for_path_returns_owning_node_for_an_anchor`: `lat.index["sec"].path` -> `lat.index[TargetId("up", "sec")].path`.

- [ ] **Step 16: Update `test_impact.py`**

Add `from game_lattice.model import TargetId`. Every bare-anchor ref and token must be namespaced, and `expand_targets` results wrapped in `TargetId`:

- `test_section_token_expands_to_ancestors_and_file`: token `"child"` -> `"a#child"`; result `{"child", "parent", "a"}` -> `{TargetId("a", "child"), TargetId("a", "parent"), TargetId("a")}`.
- `test_impact_section_reaches_whole_file_dependents`: `RawEdge(ref="up")` stays (whole-file, valid); `RawEdge(ref="sec")` -> `RawEdge(ref="up#sec")`; `impact(lat, "sec")` -> `impact(lat, "up#sec")`.
- `test_file_token_expands_to_its_anchors`: `expand_targets(lat, "a")` result `{"a", "a-top", "sec"}` -> `{TargetId("a"), TargetId("a", "a-top"), TargetId("a", "sec")}`.
- `test_impact_includes_parent_dependents_for_nested_edit`: `RawEdge(ref="parent")` -> `RawEdge(ref="up#parent")`; `RawEdge(ref="child")` -> `RawEdge(ref="up#child")`; `impact(lat, "child")` -> `impact(lat, "up#child")`.
- `test_impact_is_transitive`: `RawEdge(ref="u")` -> `RawEdge(ref="up#u")`; `RawEdge(ref="mid")` stays (whole-file id `mid`); `impact(lat, "u")` -> `impact(lat, "up#u")`.
- `test_impact_unknown_token_raises`: token `"nonexistent"` still unknown; unchanged.
- `test_impact_known_id_with_no_dependents_is_empty`: `impact(lat, "a")` unchanged (`a` is a file id).
- `test_impact_diamond_reaches_each_node_once`: `RawEdge(ref="a-sec")` -> `RawEdge(ref="a#a-sec")` (`RawEdge(ref="a")` stays); `impact(lat, "a")` unchanged.
- `test_impact_cycle_terminates`: `RawEdge(ref="b")`/`RawEdge(ref="a")` are whole-file ids; unchanged. `impact(lat, "a")` unchanged.
- `test_impact_reaches_dependents_of_an_affected_nodes_sections`: `RawEdge(ref="mid-sec")` -> `RawEdge(ref="mid#mid-sec")` (`RawEdge(ref="top")` stays); `impact(lat, "top")` unchanged.
- `test_expand_targets_unknown_id_returns_empty`: unchanged (`"ghost"` returns `set()`).
- `test_expand_targets_file_without_anchors_is_just_its_id`: `{"a"}` -> `{TargetId("a")}`.
- `test_impact_results_sorted_by_id`: `RawEdge(ref="u")` -> `RawEdge(ref="up#u")` (all three); `impact(lat, "u")` -> `impact(lat, "up#u")`.
- Replace `test_impact_accepts_namespaced_token_like_bare_id` (its premise is removed) with a bare-anchor-is-unknown test:

```python
def test_bare_anchor_token_is_unknown_but_namespaced_resolves():
    # A bare anchor token no longer resolves; the namespaced form does.
    lat = build_lattice(
        [
            _doc("a.md", "# A {#a-top}\n\n## Sec {#sec}\nx\n", id="a"),
            _doc("d.md", "x\n", id="d", derives_from=[RawEdge(ref="a#sec")]),
        ]
    )
    with pytest.raises(ValidationError):
        impact(lat, "sec")  # bare anchor is not a file id -> unknown
    assert {n.id for n in impact(lat, "a#sec")} == {"d"}
```

- [ ] **Step 17: Update `test_render.py`**

Add `from game_lattice.model import TargetId`. Namespace the bare-anchor refs and TargetId-wrap the stale-edge tuples:

- `_lattice()`: `RawEdge(ref="u")` -> `RawEdge(ref="up#u")`.
- `test_mermaid_styles_stale_edges`: `{("down", "u")}` -> `{("down", TargetId("up", "u"))}`.
- `test_dot_styles_stale_edges`: `{("down", "u")}` -> `{("down", TargetId("up", "u"))}`.
- `test_section_edge_drawn_from_owning_file_not_bare_anchor`: unchanged (it asserts on rendered node ids `up`/`down`, which are file ids).
- `test_mermaid_sanitizes_node_ids_with_spaces`: `RawEdge(ref="my doc")` stays (bare whole-file id `my doc` still resolves); unchanged.
- `test_multiple_section_edges_collapse_with_stale_or`: `RawEdge(ref="a")` -> `RawEdge(ref="up#a")`; `RawEdge(ref="b")` -> `RawEdge(ref="up#b")`; `{("down", "b")}` -> `{("down", TargetId("up", "b"))}`.
- `test_edges_emitted_in_sorted_order`: `RawEdge(ref="u1")` -> `RawEdge(ref="u#u1")` (both).
- `test_mermaid_omits_broken_edge_but_keeps_node`: unchanged (`ghost` still broken).

- [ ] **Step 18: Update `test_reconcile.py`**

The bare `--ref accent` no longer matches the namespaced stored ref. Apply:

- Replace `test_reconcile_ref_bare_matches_namespaced` entirely:

```python
def test_reconcile_ref_namespaced_matches_stored_ref(lattice_dir: Path):
    lat = load_lattice(load_config(None, lattice_dir))
    plan = reconcile(lat, "pc-design", ref="art-direction#accent", reconcile_all=False)
    assert plan, "plan must be non-empty"
    all_refs = _planned_refs(plan)
    assert "art-direction#accent" in all_refs


def test_reconcile_ref_bare_anchor_no_longer_matches(lattice_dir: Path):
    # A bare anchor ref does not match the file-scoped stored ref: reported, not a silent no-op.
    lat = load_lattice(load_config(None, lattice_dir))
    with pytest.raises(ValidationError):
        reconcile(lat, "pc-design", ref="accent", reconcile_all=False)
```

- `test_reconcile_all_skips_already_ok_edge`: `ref="accent"` -> `ref="art-direction#accent"`.
- `test_reconcile_ref_targeting_ok_edge_plans_nothing`: both `ref="accent"` occurrences -> `ref="art-direction#accent"`.
- `test_reconcile_all_with_ref_filters_without_raising`: `ref="accent"` -> `ref="art-direction#accent"`.

(All `apply_reconcile` tests use raw ref strings like `{"a#x": ...}` and are unchanged.)

- [ ] **Step 19: Update `test_orchestrate.py`**

Add `from game_lattice.model import TargetId`. In `test_load_lattice_from_dir`:

```python
    assert lat.index[TargetId("art-direction", "accent")].kind == "section"
    refs = {e.target_id for e in lat.nodes_by_id["pc-design"].derives_from}
    assert refs == {TargetId("art-direction", "accent"), TargetId("art-direction", "motion")}
```

- [ ] **Step 20: Update `test_lint.py`**

Add `from game_lattice.model import TargetId`. Wrap every `target_id` string comparison in `TargetId` (file targets have no anchor; `sec` is a section in `up`):

- `test_binding_deriving_from_derived_is_a_violation`: `(v.target_id, v.target_authority) == ("up", "derived")` -> `(v.target_id, v.target_authority) == (TargetId("up"), "derived")`.
- `test_unannotated_target_is_skipped_not_failed`: `result.skipped[0].target_id == "up"` -> `== TargetId("up")`.
- `test_both_endpoints_unannotated_reports_source_first`: `result.skipped[0].target_id == "up"` -> `== TargetId("up")`.
- `test_section_target_violation_uses_owning_file_authority`: `v.target_id == "sec"` -> `v.target_id == TargetId("up", "sec")`.
- `test_section_target_skipped_when_owning_file_unannotated`: `result.skipped[0].target_id == "sec"` -> `== TargetId("up", "sec")`.
- `test_results_are_in_node_id_then_edge_order`: `[v.target_id for v in result.violations] == ["weak1", "weak2", "weak1"]` -> `== [TargetId("weak1"), TargetId("weak2"), TargetId("weak1")]`.
- `test_skips_are_in_node_id_then_edge_order`: the list `[("a", "t1"), ("a", "t2"), ("b", "t1")]` -> `[("a", TargetId("t1")), ("a", TargetId("t2")), ("b", TargetId("t1"))]`.
- `test_single_node_classifies_each_edge_independently`: `[v.target_id ...] == ["weak"]` -> `== [TargetId("weak")]`; `[(s.target_id, s.reason) ...] == [("bare", "target-unannotated")]` -> `== [(TargetId("bare"), "target-unannotated")]`.

(All `derives=(...)` refs in `test_lint.py` are whole-file ids or already namespaced `up#sec`, so no ref rewrites are needed.)

- [ ] **Step 21: Update `test_cli.py`**

- `test_check_json_reports_all_states`: `stale["target_id"] == "accent"` -> `stale["target_id"] == "art-direction#accent"`.
- `test_impact_lists_dependents`: `["impact", "accent", "--json"]` -> `["impact", "art-direction#accent", "--json"]`.
- `test_impact_human_output_lists_tickets`: `["impact", "accent"]` -> `["impact", "art-direction#accent"]`.
- `test_linear_from_grades_downstream`: `["linear", "--from", "accent", "--json"]` -> `["linear", "--from", "art-direction#accent", "--json"]`.

(The `lint --json` CLI tests assert only `source_id`/`target_authority`/`reason`, so they need no change.)

### Verify and commit

- [ ] **Step 22: Run the new-behavior tests to verify they now pass**

Run: `uv run --group dev pytest tests/test_loader.py -k "same_slug or marker_equal or bare_anchor_ref or same_anchor_in_two_files or anchor_in_one_file" tests/test_stale_shipped.py -k "whole_file_dependent or target_scoping_includes_the_named_node" -v`
Expected: PASS (cross-file non-collision, within-file collision, bare-ref-broken, audit add-back, and the from-trigger whole-file bridge).

- [ ] **Step 23: Run the full suite and the gate**

Run: `uv run --group dev pytest`
Expected: PASS, coverage at or above 80 percent.

Run: `uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src && uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: all clean. If `ty` flags a residual `str` id anywhere, fix that call site to use `TargetId`.

- [ ] **Step 24: Commit**

```bash
git add src/game_lattice tests
git commit -m "feat: file-scoped anchor resolution via typed TargetId"
```

---

## Task 4: Documentation

No code; align the prose with the shipped behavior.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `src/game_lattice/error_types.py`

- [ ] **Step 1: Update `error_types.py` `DuplicateIdError` docstring**

```python
class DuplicateIdError(ProjectError):
    """Two file ids collide, or two headings in one file resolve to the same anchor id."""
```

- [ ] **Step 2: Update `CLAUDE.md`**

In the Architecture section, update the resolution description. Replace the paragraph beginning "A `derives_from` ref resolves on the trailing segment after the last `#`" with:

```markdown
**Refs and edge state.** A `derives_from` ref resolves file-scoped through `model.parse_ref`:
`save-format#slot-table` parses to `TargetId("save-format", "slot-table")` and resolves against
that file's headings, while a bare ref (`save-format`) is a whole-file target. A heading is
addressed by an explicit `{#marker}` when present, otherwise by its GitHub slug (byte-parity with
GitHub's rendered anchor; see `sections.github_slug`/`anchor_ids`). A ref that resolves to nothing
is not a load error: it is a normal lattice state (`target_id=None`) that `check` reports as
`BROKEN`. The same slug in two different files does not collide, because their `TargetId`s differ;
a within-file clash (a marker equal to a computed slug) is a `DuplicateIdError`.
```

In the "central structure" bullet list, update the `index` bullet:

```markdown
- `index`: every `TargetId` mapped to a `Location`. A file target is `TargetId(file_id)`; a
  section target is `TargetId(file_id, anchor)`. Section ids are file-scoped, so the same anchor
  in two files does not collide; a within-file clash is a `DuplicateIdError` (a load failure, exit 2).
```

In the "Project-specific invariants" section, update the "Paths and containment" or add a bullet noting a node `id` may not contain `#` (the file-id/anchor separator). Add under invariants:

```markdown
- **Node ids.** A frontmatter `id` may not contain `#`; `#` separates a file id from a section
  anchor in a ref. Enforced by a `NodeMeta` field validator (exit 2, names the id).
```

- [ ] **Step 3: Update `README.md`**

Find the section documenting anchored-section refs and the `{#slug}` marker. Replace the guidance that a marker is required with: markers are optional; a section is addressed by its GitHub heading slug by default, and an explicit `{#slug}` marker still wins as an escape hatch for a stable id independent of heading text. Note that section refs are file-scoped (`file#anchor`) and that a bare ref addresses a whole file. Keep the change to the specific passage; do not restructure the README.

- [ ] **Step 4: Verify docs and commit**

Run: `uv run --group dev python scripts/check_version_sync.py`
Expected: PASS (docs-only change does not affect version sync).

Confirm no em-dashes were introduced:
Run: `grep -nP "\x{2014}" CLAUDE.md README.md src/game_lattice/error_types.py || echo "no em-dashes"`
Expected: `no em-dashes`.

```bash
git add CLAUDE.md README.md src/game_lattice/error_types.py
git commit -m "docs: document file-scoped GitHub-slug anchor resolution"
```

---

## Self-Review

Run after implementation is complete.

**Spec coverage:**
- Spec 3 (github_slug byte-parity, no-collapse, emoji): Task 1 parity corpus. Covered.
- Spec 4 (marker precedence, counter reservation, every heading addressable): Task 1 `anchor_ids` + reservation test; Task 3 loader. Covered.
- Spec 5.1 to 5.5 (`TargetId`, `parse_ref`, bare-refs-broken, TargetId maps, id guard): Tasks 2 and 3. Covered.
- Spec 6 (module-by-module incl. `stale_shipped` bridge): Task 3 Steps 3 to 12. Covered.
- Spec 7 (hash-neutrality, ref stability): section_text is untouched, so removing a marker keeps the hash; Task 3 preserves this (no test regressions on `content_hash`). Covered.
- Spec 10 (parity corpus, precedence, cross-file non-collision, within-file collision, bare-broken, parse_ref, id guard, downstream layers, Linear both-target-types, conftest verification): Tasks 1 to 3. Covered. `conftest.lattice_dir` verified green unchanged in Step 23.
- Spec 12 acceptance criteria: mapped to Task 3 Steps 1, 22, 23 (resolution, precedence, non-collision, parity, Linear no-under-report, full gate).

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Every code step shows complete code; every test edit gives concrete old and new values.

**Type consistency:** `TargetId(file_id, anchor=None)`, `parse_ref`, `Edge.target_id: TargetId | None`, `expand_targets -> set[TargetId]`, `target_content(..., TargetId)`, `EdgeStatus/LadderViolation/SkippedEdge.target_id: TargetId`, and `stale_edges: set[tuple[str, TargetId]]` are used identically across Tasks 2, 3, and 4. `anchor_ids` returns `list[str]` and is consumed positionally by the loader via `zip(..., strict=True)`.
