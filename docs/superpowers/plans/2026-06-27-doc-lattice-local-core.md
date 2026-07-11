# doc-lattice Local Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic local engine of doc-lattice: parse lattice frontmatter and anchored sections from a doc set, derive an id-indexed edge graph on demand, and expose the `impact`, `check`, `reconcile`, and `graph` commands.

**Architecture:** A pure pipeline (config to discovery to parse to a `Lattice` of nodes, an id index, and reverse adjacency) feeds four read-mostly commands. Untyped YAML is converted to typed models only in boundary-named modules. The single mutating command, `reconcile`, edits the `seen` scalar in place against a fresh read at write time. No network, no secrets, no LLM.

**Tech Stack:** Python 3.14, typer (CLI), rich (output), pydantic (validation), ruamel.yaml (round-trip YAML), pytest + hypothesis (tests).

## Global Constraints

- Python `>=3.14`; ruff line length 100; Google-style docstrings on every public function; module docstring on every module.
- No em-dashes in any drafted content (docstrings, messages, comments).
- `typing.Any` and `typing.cast` only in modules whose name ends in `_parser`/`_boundary`/`_validator` (enforced by `scripts/check_typing_boundaries.py`). Here that means `frontmatter_parser.py` only.
- All custom exceptions extend `ProjectError`. No bare `except Exception`/`except BaseException`. Error messages name the file and the fix.
- No `datetime.now()` outside `datetime_utils.py`. No hardcoded secrets. User-provided paths go through `safe_resolve()`.
- Constants use the `Literal` + `get_args()` + `frozenset` pattern in `constants.py`; import elsewhere.
- Tests use pytest, mirror source files (`src/doc_lattice/foo.py` -> `tests/test_foo.py`), use `tmp_path` for filesystem tests. Coverage gate is 80 percent (`fail_under = 80`).
- Commit after every task. Conventional commit messages, no attribution trailers.
- `seen` hash = `sha256(canonical_utf8)` truncated to 32 hex chars (128 bits).

**Spec:** `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md`. Every task's requirements implicitly include this section.

---

## Type vocabulary (locked across all tasks)

These names and signatures are fixed. Later tasks rely on them verbatim.

- `constants.py`: `Layer = Literal["design","technical","production"]`, `Authority = Literal["binding","derived","exploratory"]`, `LocationKind = Literal["file","section"]`, `EdgeState = Literal["OK","STALE","UNRECONCILED","BROKEN"]`, plus matching `VALID_*` frozensets.
- `error_types.py`: `DuplicateIdError`, `BrokenRefError`, `UnreadableDocError` (extend `ProjectError`); `ConfigError` already exists.
- `model.py`:
  - `RawEdge(ref: str, seen: str | None = None)` (pydantic)
  - `NodeMeta(id: str, title: str | None, layer: Layer | None, authority: Authority | None, derives_from: list[RawEdge], tickets: list[str])` (pydantic, `extra="forbid"`)
  - `Edge(target_ref: str, target_id: str | None, seen: str | None)` (frozen dataclass)
  - `Location(path: Path, kind: LocationKind, span: tuple[int, int])` (frozen dataclass; span is inclusive 1-indexed)
  - `Node(id: str, title: str | None, layer: Layer | None, authority: Authority | None, path: Path, body: str, derives_from: tuple[Edge, ...], tickets: tuple[str, ...])` (frozen dataclass)
  - `ParsedDoc(path: Path, meta: NodeMeta, body: str)` (frozen dataclass; loader input unit)
  - `Lattice(nodes_by_id: dict[str, Node], index: dict[str, Location], dependents: dict[str, frozenset[str]], ancestors: dict[str, tuple[str, ...]])` (frozen dataclass)
- `hashing.py`: `canonicalize(text: str) -> str`, `content_hash(text: str) -> str`
- `sections.py`: `Heading(level: int, text: str, anchor: str | None, line: int)`, `build_toc(body: str) -> list[Heading]`, `section_span(headings: list[Heading], idx: int, total_lines: int) -> tuple[int, int]`, `section_text(body: str, span: tuple[int, int]) -> str`
- `resolve.py`: `split_ref(ref: str) -> str`, `target_content(lattice: Lattice, target_id: str) -> str`
- `loader.py`: `build_lattice(docs: list[ParsedDoc]) -> Lattice`
- `config.py`: `Config(...)` (pydantic), `ProjectConfig(config: Config, project_root: Path, resolved_roots: tuple[Path, ...])`, `load_config(config_path: Path | None, cwd: Path) -> ProjectConfig`
- `frontmatter_parser.py`: `split_frontmatter(text: str) -> tuple[str | None, str]`, `parse_meta(raw_meta: str | None, source: Path) -> NodeMeta | None`
- `discovery.py`: `discover_doc_paths(roots: Sequence[Path], ignore_globs: Sequence[str]) -> list[Path]`, `read_doc(path: Path) -> str`
- `orchestrate.py`: `load_lattice(project: ProjectConfig) -> Lattice`
- `check.py`: `EdgeStatus(source_id: str, target_ref: str, target_id: str | None, state: EdgeState, expected: str | None, actual: str | None)`, `check_lattice(lattice: Lattice) -> list[EdgeStatus]`, `has_drift(statuses: list[EdgeStatus]) -> bool`
- `impact.py`: `expand_targets(lattice: Lattice, token: str) -> set[str]`, `impact(lattice: Lattice, token: str) -> list[Node]`
- `reconcile.py`: `apply_reconcile(current_file_text: str, updates: dict[str, str]) -> str`, `reconcile(lattice: Lattice, downstream_id: str, *, ref: str | None, reconcile_all: bool) -> dict[Path, dict[str, str]]`
- `render.py`: `to_mermaid(lattice: Lattice, stale_edges: set[tuple[str, str]]) -> str`, `to_dot(lattice: Lattice, stale_edges: set[tuple[str, str]]) -> str`

---

## Task 1: Constants and error types

**Files:**
- Modify: `src/doc_lattice/constants.py`
- Modify: `src/doc_lattice/error_types.py`
- Test: `tests/test_constants.py`, `tests/test_error_types.py`

**Interfaces:**
- Produces: the `Layer`/`Authority`/`LocationKind`/`EdgeState` literals and `VALID_*` frozensets; `DuplicateIdError`, `BrokenRefError`, `UnreadableDocError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_constants.py`:

```python
from doc_lattice.constants import (
    VALID_AUTHORITIES,
    VALID_EDGE_STATES,
    VALID_LAYERS,
    Authority,
    EdgeState,
    Layer,
)


def test_layers_match_literal():
    assert frozenset(get_args(Layer)) == VALID_LAYERS
    assert "design" in VALID_LAYERS


def test_authorities_match_literal():
    assert frozenset(get_args(Authority)) == VALID_AUTHORITIES
    assert "binding" in VALID_AUTHORITIES


def test_edge_states_match_literal():
    assert frozenset(get_args(EdgeState)) == VALID_EDGE_STATES
    assert {"OK", "STALE", "UNRECONCILED", "BROKEN"} == set(VALID_EDGE_STATES)
```

Append to `tests/test_error_types.py`:

```python
from doc_lattice.error_types import (
    BrokenRefError,
    DuplicateIdError,
    ProjectError,
    UnreadableDocError,
)


def test_new_errors_extend_project_error():
    for exc in (DuplicateIdError("x"), BrokenRefError("x"), UnreadableDocError("x")):
        assert isinstance(exc, ProjectError)


def test_error_codes():
    assert DuplicateIdError("x").code == "DUPLICATE_ID"
    assert BrokenRefError("x").code == "BROKEN_REF"
    assert UnreadableDocError("x").code == "UNREADABLE_DOC"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_constants.py tests/test_error_types.py -v`
Expected: FAIL with ImportError on the new names.

- [ ] **Step 3: Implement**

Append to `src/doc_lattice/constants.py`:

```python
Layer = Literal["design", "technical", "production"]
VALID_LAYERS: frozenset[str] = frozenset(get_args(Layer))

Authority = Literal["binding", "derived", "exploratory"]
VALID_AUTHORITIES: frozenset[str] = frozenset(get_args(Authority))

LocationKind = Literal["file", "section"]
VALID_LOCATION_KINDS: frozenset[str] = frozenset(get_args(LocationKind))

EdgeState = Literal["OK", "STALE", "UNRECONCILED", "BROKEN"]
VALID_EDGE_STATES: frozenset[str] = frozenset(get_args(EdgeState))
```

Append to `src/doc_lattice/error_types.py`:

```python
class DuplicateIdError(ProjectError):
    """Two lattice ids collide in the flat namespace."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="DUPLICATE_ID")


class BrokenRefError(ProjectError):
    """A derives_from ref resolves to no id in the index."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="BROKEN_REF")


class UnreadableDocError(ProjectError):
    """A doc cannot be read as UTF-8 or its YAML cannot be parsed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNREADABLE_DOC")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_constants.py tests/test_error_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/constants.py src/doc_lattice/error_types.py tests/test_constants.py tests/test_error_types.py
git commit -m "feat: add lattice constants and error types"
```

---

## Task 2: Domain model

**Files:**
- Create: `src/doc_lattice/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes: constants `Layer`, `Authority`, `LocationKind`.
- Produces: `RawEdge`, `NodeMeta`, `Edge`, `Location`, `Node`, `ParsedDoc`, `Lattice`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_model.py`:

```python
"""Tests for domain model."""

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from doc_lattice.model import Edge, Lattice, Location, Node, NodeMeta, ParsedDoc, RawEdge


def test_nodemeta_validates_and_defaults():
    meta = NodeMeta.model_validate({"id": "pc-design"})
    assert meta.id == "pc-design"
    assert meta.derives_from == []
    assert meta.tickets == []


def test_nodemeta_forbids_extra_keys():
    with pytest.raises(PydanticValidationError):
        NodeMeta.model_validate({"id": "x", "typoo": 1})


def test_nodemeta_parses_edges():
    meta = NodeMeta.model_validate(
        {"id": "x", "derives_from": [{"ref": "a#b", "seen": "deadbeef"}]}
    )
    assert meta.derives_from[0] == RawEdge(ref="a#b", seen="deadbeef")


def test_dataclasses_are_frozen():
    edge = Edge(target_ref="a#b", target_id="b", seen=None)
    with pytest.raises(AttributeError):
        edge.seen = "x"  # type: ignore[misc]


def test_lattice_holds_maps():
    node = Node(
        id="x", title=None, layer=None, authority=None, path=Path("x.md"),
        body="", derives_from=(), tickets=(),
    )
    lat = Lattice(
        nodes_by_id={"x": node},
        index={"x": Location(path=Path("x.md"), kind="file", span=(1, 1))},
        dependents={},
        ancestors={},
    )
    assert lat.nodes_by_id["x"].id == "x"
    assert ParsedDoc(path=Path("x.md"), meta=NodeMeta(id="x"), body="").meta.id == "x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --group dev pytest tests/test_model.py -v`
Expected: FAIL with ModuleNotFoundError for `doc_lattice.model`.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/model.py`:

```python
"""Domain types for the lattice graph."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .constants import Authority, Layer, LocationKind


class RawEdge(BaseModel):
    """One derives_from entry as written in frontmatter."""

    model_config = ConfigDict(strict=True, extra="forbid")

    ref: str
    seen: str | None = None


class NodeMeta(BaseModel):
    """Validated lattice frontmatter for one tracked file."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str
    title: str | None = None
    layer: Layer | None = None
    authority: Authority | None = None
    derives_from: list[RawEdge] = Field(default_factory=list)
    tickets: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Edge:
    """A resolved derives_from edge. ``target_id`` is None when the ref is broken."""

    target_ref: str
    target_id: str | None
    seen: str | None


@dataclass(frozen=True, slots=True)
class Location:
    """Where an id lives. ``span`` is an inclusive 1-indexed line range."""

    path: Path
    kind: LocationKind
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Node:
    """One tracked file assembled from its frontmatter and body."""

    id: str
    title: str | None
    layer: Layer | None
    authority: Authority | None
    path: Path
    body: str
    derives_from: tuple[Edge, ...]
    tickets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """A discovered file with validated frontmatter and its raw body."""

    path: Path
    meta: NodeMeta
    body: str


@dataclass(frozen=True, slots=True)
class Lattice:
    """The whole derived graph.

    ``index`` maps every stable id to a Location. ``dependents`` maps a target id
    to the set of source node ids that derive from it. ``ancestors`` maps a section
    anchor id to the anchored sections (outermost to innermost) whose spans contain it.
    """

    nodes_by_id: dict[str, Node]
    index: dict[str, Location]
    dependents: dict[str, frozenset[str]]
    ancestors: dict[str, tuple[str, ...]]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --group dev pytest tests/test_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/model.py tests/test_model.py
git commit -m "feat: add lattice domain model"
```

---

## Task 3: Hashing

**Files:**
- Create: `src/doc_lattice/hashing.py`
- Test: `tests/test_hashing.py`

**Interfaces:**
- Produces: `canonicalize(text) -> str`, `content_hash(text) -> str` (32 hex chars).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hashing.py`:

```python
"""Tests for hashing."""

from hypothesis import given
from hypothesis import strategies as st

from doc_lattice.hashing import canonicalize, content_hash


def test_canonicalize_strips_trailing_ws_and_blank_edges():
    assert canonicalize("\n\n  hi  \nthere \n\n") == "  hi\nthere"


def test_content_hash_is_32_hex_chars():
    h = content_hash("anything")
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)


def test_crlf_and_final_newline_do_not_change_hash():
    base = "# Title\n\nbody line\n"
    assert content_hash(base) == content_hash("# Title\r\n\r\nbody line")
    assert content_hash(base) == content_hash("# Title\n\nbody line\n\n\n")


def test_substantive_change_changes_hash_examples():
    assert content_hash("accent: blue") != content_hash("accent: red")
    assert content_hash("a\nb") != content_hash("a\nb\nc")


@given(st.text())
def test_canonicalize_is_idempotent(text: str):
    once = canonicalize(text)
    assert canonicalize(once) == once


@given(st.text())
def test_trailing_whitespace_invariant(text: str):
    # Appending trailing spaces to each line must not change the hash.
    noisy = "\n".join(line + "   " for line in text.split("\n"))
    assert content_hash(text) == content_hash(noisy)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_hashing.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/hashing.py`:

```python
"""Canonicalize section content and compute its content hash."""

import hashlib

_HASH_HEX_LEN = 32


def canonicalize(text: str) -> str:
    """Normalize content so cosmetic edits do not change the hash.

    Args:
        text: Raw section or file content.

    Returns:
        Line endings normalized to ``\\n``, trailing whitespace stripped per line,
        and leading and trailing blank lines trimmed. Internal blank lines are kept.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    start = 0
    end = len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return "\n".join(lines[start:end])


def content_hash(text: str) -> str:
    """Return the 128-bit (32 hex char) SHA-256 hash of the canonicalized text.

    Args:
        text: Raw section or file content.

    Returns:
        The first 32 hex characters of ``sha256(canonicalize(text))``.
    """
    canonical = canonicalize(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_hashing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/hashing.py tests/test_hashing.py
git commit -m "feat: add content hashing"
```

---

## Task 4: Section extraction

**Files:**
- Create: `src/doc_lattice/sections.py`
- Test: `tests/test_sections.py`

**Interfaces:**
- Produces: `Heading`, `build_toc(body) -> list[Heading]`, `section_span(headings, idx, total_lines) -> tuple[int, int]`, `section_text(body, span) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sections.py`:

```python
"""Tests for section extraction."""

from doc_lattice.sections import build_toc, section_span, section_text

DOC = """# Top {#top}
intro

## Accent {#accent}
accent body

### Nested {#nested}
nested body

## Other {#other}
other body
"""


def test_build_toc_extracts_levels_and_anchors():
    toc = build_toc(DOC)
    assert [(h.level, h.anchor, h.line) for h in toc] == [
        (1, "top", 1),
        (2, "accent", 4),
        (3, "nested", 7),
        (2, "other", 10),
    ]


def test_build_toc_anchorless_heading():
    toc = build_toc("## Plain Heading\nbody\n")
    assert toc[0].anchor is None
    assert toc[0].text == "Plain Heading"


def test_section_span_stops_at_same_or_higher_level():
    toc = build_toc(DOC)
    total = len(DOC.splitlines())
    # "accent" (index 1) spans through its nested child until "## Other" at line 10.
    assert section_span(toc, 1, total) == (4, 9)
    # "nested" (index 2) spans until "## Other".
    assert section_span(toc, 2, total) == (7, 9)
    # "other" (index 3) spans to EOF.
    assert section_span(toc, 3, total) == (10, total)


def test_section_text_strips_anchor_from_heading_line():
    toc = build_toc(DOC)
    text = section_text(DOC, section_span(toc, 1, len(DOC.splitlines())))
    assert text.startswith("## Accent\n")
    assert "{#accent}" not in text
    assert "nested body" in text  # nested content is part of the parent span
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_sections.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/sections.py`:

```python
"""Heading-TOC and anchored-section extraction.

Section-span semantics are adapted from gx-linear-skills' binding_slicer: a section
spans from its heading line through the line before the next heading of equal or higher
level, or to end of file.
"""

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ANCHOR_RE = re.compile(r"\s*\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}\s*")


@dataclass(frozen=True, slots=True)
class Heading:
    """One markdown heading. ``line`` is 1-indexed. ``text`` keeps the anchor marker."""

    level: int
    text: str
    anchor: str | None
    line: int


def build_toc(body: str) -> list[Heading]:
    """Return all ATX headings in ``body`` in document order.

    Args:
        body: Markdown document text.

    Returns:
        A list of Heading, each with its level, text, optional ``{#anchor}`` id, and
        1-indexed line number.
    """
    headings: list[Heading] = []
    for i, line in enumerate(body.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        raw_text = match.group(2)
        anchor_match = _ANCHOR_RE.search(raw_text)
        anchor = anchor_match.group(1) if anchor_match else None
        headings.append(Heading(level=level, text=raw_text, anchor=anchor, line=i))
    return headings


def section_span(headings: list[Heading], idx: int, total_lines: int) -> tuple[int, int]:
    """Return the inclusive 1-indexed line range for ``headings[idx]``.

    Args:
        headings: The document TOC from ``build_toc``.
        idx: Index into ``headings`` of the section of interest.
        total_lines: Total line count of the document.

    Returns:
        ``(start, end)`` from the heading line through the line before the next heading
        of equal or higher level, or to ``total_lines``.
    """
    head = headings[idx]
    end = total_lines
    for nxt in headings[idx + 1 :]:
        if nxt.level <= head.level:
            end = nxt.line - 1
            break
    return (head.line, end)


def section_text(body: str, span: tuple[int, int]) -> str:
    """Return the text of a section span with the heading's ``{#anchor}`` marker removed.

    Args:
        body: Markdown document text.
        span: Inclusive 1-indexed ``(start, end)`` line range.

    Returns:
        The joined lines of the span, with the anchor marker stripped from the first
        (heading) line.
    """
    lines = body.splitlines()
    start, end = span
    chunk = lines[start - 1 : end]
    if chunk:
        chunk[0] = _ANCHOR_RE.sub(" ", chunk[0]).rstrip()
    return "\n".join(chunk)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_sections.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/sections.py tests/test_sections.py
git commit -m "feat: add heading-TOC and section extraction"
```

---

## Task 5: Ref resolution and content lookup

**Files:**
- Create: `src/doc_lattice/resolve.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: `Lattice`, `Location`, `Node` from `model`; `section_text` (via stored body); `BrokenRefError`.
- Produces: `split_ref(ref) -> str`, `target_content(lattice, target_id) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resolve.py`:

```python
"""Tests for ref resolution and content lookup."""

from pathlib import Path

import pytest

from doc_lattice.error_types import BrokenRefError
from doc_lattice.model import Lattice, Location, Node
from doc_lattice.resolve import split_ref, target_content


def test_split_ref_keys_on_trailing_id():
    assert split_ref("art-direction#accent") == "accent"
    assert split_ref("accent") == "accent"
    assert split_ref("a#b#c") == "c"


def _lattice() -> Lattice:
    body = "# Doc {#doc}\nfile body\n\n## Accent {#accent}\naccent body\n"
    node = Node(
        id="doc", title=None, layer=None, authority=None, path=Path("doc.md"),
        body=body, derives_from=(), tickets=(),
    )
    return Lattice(
        nodes_by_id={"doc": node},
        index={
            "doc": Location(path=Path("doc.md"), kind="file", span=(1, 6)),
            "accent": Location(path=Path("doc.md"), kind="section", span=(4, 6)),
        },
        dependents={},
        ancestors={},
    )


def test_target_content_section():
    assert "accent body" in target_content(_lattice(), "accent")
    assert "{#accent}" not in target_content(_lattice(), "accent")


def test_target_content_file_is_whole_body():
    assert target_content(_lattice(), "doc") == _lattice().nodes_by_id["doc"].body


def test_target_content_broken_raises():
    with pytest.raises(BrokenRefError):
        target_content(_lattice(), "missing")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_resolve.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/resolve.py`:

```python
"""Resolve refs to ids and fetch the current content a target id covers."""

from .error_types import BrokenRefError
from .model import Lattice
from .sections import section_text


def split_ref(ref: str) -> str:
    """Return the stable id a ref points at.

    Args:
        ref: A ref written bare (``accent``) or namespaced (``art-direction#accent``).

    Returns:
        The trailing id after the last ``#``; the namespace prefix is display-only.
    """
    return ref.rsplit("#", 1)[-1]


def target_content(lattice: Lattice, target_id: str) -> str:
    """Return the content a target id covers, for hashing.

    Args:
        lattice: The built lattice.
        target_id: A resolved stable id present in ``lattice.index``.

    Returns:
        The whole node body for a ``file`` location, or the anchored section text for a
        ``section`` location.

    Raises:
        BrokenRefError: If ``target_id`` is not in the index.
    """
    location = lattice.index.get(target_id)
    if location is None:
        msg = f"ref resolves to unknown id {target_id!r}; fix the ref or add the anchor"
        raise BrokenRefError(msg)
    node = _node_for_path(lattice, location.path)
    if location.kind == "file":
        return node.body
    return section_text(node.body, location.span)


def _node_for_path(lattice: Lattice, path: object) -> "object":
    for node in lattice.nodes_by_id.values():
        if node.path == path:
            return node
    msg = f"no node owns location path {path!r}"
    raise BrokenRefError(msg)
```

Note: keep `_node_for_path` returning the matching `Node`; the loose annotation avoids a forward-reference cycle and is internal only. (If `ty` flags it, annotate the return as `Node` and import `Node` at top.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_resolve.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/resolve.py tests/test_resolve.py
git commit -m "feat: add ref resolution and content lookup"
```

---

## Task 6: Loader (build_lattice)

**Files:**
- Create: `src/doc_lattice/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: `ParsedDoc`, `NodeMeta`, `RawEdge`, `Edge`, `Node`, `Location`, `Lattice` from `model`; `build_toc`, `section_span` from `sections`; `split_ref` from `resolve`; `DuplicateIdError`.
- Produces: `build_lattice(docs: list[ParsedDoc]) -> Lattice`.

**Algorithm:** Two passes. Pass 1 registers every file id and every `{#anchor}` id into the index (raising `DuplicateIdError` on collision) and records each file's section spans and ancestor relationships. Pass 2 builds `Node`s, resolving each `RawEdge.ref` via `split_ref` to a `target_id` (or `None` if absent from the index) and accumulating `dependents` from resolved edges only.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_loader.py`:

```python
"""Tests for build_lattice."""

from pathlib import Path

import pytest

from doc_lattice.error_types import DuplicateIdError
from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge


def _doc(path: str, body: str, **meta) -> ParsedDoc:
    return ParsedDoc(path=Path(path), meta=NodeMeta(**meta), body=body)


def test_registers_file_and_anchor_ids():
    docs = [_doc("a.md", "# A {#sec}\nbody\n", id="a")]
    lat = build_lattice(docs)
    assert lat.index["a"].kind == "file"
    assert lat.index["sec"].kind == "section"
    assert lat.index["sec"].span == (1, 2)


def test_resolves_edges_and_builds_dependents():
    docs = [
        _doc("up.md", "# Up {#accent}\nx\n", id="up"),
        _doc("down.md", "body\n", id="down",
             derives_from=[RawEdge(ref="up#accent", seen="h")]),
    ]
    lat = build_lattice(docs)
    edge = lat.nodes_by_id["down"].derives_from[0]
    assert edge.target_id == "accent"
    assert lat.dependents["accent"] == frozenset({"down"})


def test_broken_ref_is_none_not_error():
    docs = [_doc("d.md", "b\n", id="d", derives_from=[RawEdge(ref="ghost")])]
    lat = build_lattice(docs)
    assert lat.nodes_by_id["d"].derives_from[0].target_id is None
    assert "ghost" not in lat.dependents


def test_duplicate_id_raises():
    docs = [_doc("a.md", "b\n", id="dup"), _doc("b.md", "c\n", id="dup")]
    with pytest.raises(DuplicateIdError):
        build_lattice(docs)


def test_anchor_collides_with_file_id_raises():
    docs = [_doc("a.md", "# A {#b}\n", id="a"), _doc("b.md", "x\n", id="b")]
    with pytest.raises(DuplicateIdError):
        build_lattice(docs)


def test_ancestors_computed_for_nested_anchor():
    body = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert lat.ancestors["child"] == ("parent",)
    assert lat.ancestors["parent"] == ()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_loader.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/loader.py`:

```python
"""Assemble parsed docs into a Lattice. Pure: no filesystem access."""

from collections import defaultdict
from pathlib import Path

from .error_types import DuplicateIdError
from .model import Edge, Lattice, Location, Node, ParsedDoc
from .resolve import split_ref
from .sections import Heading, build_toc, section_span


def build_lattice(docs: list[ParsedDoc]) -> Lattice:
    """Build the lattice from parsed docs.

    Args:
        docs: Tracked files with validated frontmatter and bodies.

    Returns:
        A Lattice with the id index, nodes, reverse adjacency, and ancestor map.

    Raises:
        DuplicateIdError: If any two ids (file or anchor) collide.
    """
    index: dict[str, Location] = {}
    sources: dict[str, str] = {}
    ancestors: dict[str, tuple[str, ...]] = {}

    for doc in docs:
        _register(doc.meta.id, Location(path=doc.path, kind="file", span=(1, _line_count(doc.body))), index, sources, f"file {doc.path}")
        toc = build_toc(doc.body)
        total = _line_count(doc.body)
        anchored = [(i, h) for i, h in enumerate(toc) if h.anchor is not None]
        spans: dict[str, tuple[int, int]] = {}
        for i, head in anchored:
            span = section_span(toc, i, total)
            spans[head.anchor] = span  # type: ignore[index]
            _register(head.anchor, Location(path=doc.path, kind="section", span=span), index, sources, f"anchor in {doc.path}")  # type: ignore[arg-type]
        _record_ancestors(anchored, spans, ancestors)

    nodes: dict[str, Node] = {}
    dependents: defaultdict[str, set[str]] = defaultdict(set)
    for doc in docs:
        edges: list[Edge] = []
        for raw in doc.meta.derives_from:
            tid = split_ref(raw.ref)
            target_id = tid if tid in index else None
            edges.append(Edge(target_ref=raw.ref, target_id=target_id, seen=raw.seen))
            if target_id is not None:
                dependents[target_id].add(doc.meta.id)
        nodes[doc.meta.id] = Node(
            id=doc.meta.id, title=doc.meta.title, layer=doc.meta.layer,
            authority=doc.meta.authority, path=doc.path, body=doc.body,
            derives_from=tuple(edges), tickets=tuple(doc.meta.tickets),
        )

    return Lattice(
        nodes_by_id=nodes,
        index=index,
        dependents={k: frozenset(v) for k, v in dependents.items()},
        ancestors=ancestors,
    )


def _line_count(body: str) -> int:
    return max(1, len(body.splitlines()))


def _register(
    id_: str, location: Location, index: dict[str, Location],
    sources: dict[str, str], where: str,
) -> None:
    if id_ in index:
        msg = f"duplicate id {id_!r}: already registered at {sources[id_]}, again at {where}"
        raise DuplicateIdError(msg)
    index[id_] = location
    sources[id_] = where


def _record_ancestors(
    anchored: list[tuple[int, Heading]],
    spans: dict[str, tuple[int, int]],
    ancestors: dict[str, tuple[str, ...]],
) -> None:
    for _, head in anchored:
        anchor = head.anchor
        if anchor is None:
            continue
        start, end = spans[anchor]
        containing: list[tuple[tuple[int, int], str]] = []
        for _, other in anchored:
            oid = other.anchor
            if oid is None or oid == anchor:
                continue
            ostart, oend = spans[oid]
            if ostart < start and oend >= end or (ostart <= start and oend > end):
                containing.append(((ostart, oend), oid))
        containing.sort(key=lambda item: item[0][1] - item[0][0], reverse=True)
        ancestors[anchor] = tuple(oid for _, oid in containing)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_loader.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/loader.py tests/test_loader.py
git commit -m "feat: add lattice loader with id index and reverse adjacency"
```

---

## Task 7: Config

**Files:**
- Create: `src/doc_lattice/config.py`
- Modify: `pyproject.toml` (add `ruamel.yaml` dependency)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ConfigError`, `safe_resolve`.
- Produces: `Config`, `ProjectConfig`, `load_config(config_path, cwd) -> ProjectConfig`.

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`, in `[project].dependencies`, add the line so the list reads:

```toml
dependencies = [
    "typer>=0.12",
    "rich>=13",
    "pydantic>=2",
    "ruamel.yaml>=0.18",
]
```

Run: `uv sync --group dev`
Expected: resolves and installs `ruamel.yaml`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_config.py`:

```python
"""Tests for config loading."""

from pathlib import Path

import pytest

from doc_lattice.config import load_config
from doc_lattice.error_types import ConfigError


def test_absent_config_uses_defaults(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.docs_roots == ["docs"]
    assert project.project_root == tmp_path.resolve()
    assert project.resolved_roots == (tmp_path.resolve() / "docs",)


def test_loads_and_resolves_roots(tmp_path: Path):
    (tmp_path / "design").mkdir()
    (tmp_path / ".doc-lattice.yml").write_text(
        "docs_roots: [design]\nignore_globs: ['**/x/**']\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.ignore_globs == ["**/x/**"]
    assert project.resolved_roots == (tmp_path.resolve() / "design",)


def test_root_escaping_project_is_rejected(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['../outside']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_absolute_outside_root_is_rejected(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text(
        "docs_roots: ['/etc']\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_unknown_key_rejected(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_missing_explicit_config_path_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yml", tmp_path)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_config.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 4: Implement**

Create `src/doc_lattice/config.py`:

```python
"""Load and validate .doc-lattice.yml, with project-root containment of docs_roots."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .error_types import ConfigError
from .path_utils import safe_resolve

DEFAULT_CONFIG_NAME = ".doc-lattice.yml"


class Config(BaseModel):
    """The validated shape of .doc-lattice.yml."""

    model_config = ConfigDict(strict=True, extra="forbid")

    docs_roots: list[str] = Field(default_factory=lambda: ["docs"])
    ignore_globs: list[str] = Field(default_factory=list)
    linear_team: str | None = None
    binding_layers: list[str] | None = None


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """A loaded config plus the project root and the resolved, contained docs roots."""

    config: Config
    project_root: Path
    resolved_roots: tuple[Path, ...]


def load_config(config_path: Path | None, cwd: Path) -> ProjectConfig:
    """Load config and resolve docs roots inside the project boundary.

    Args:
        config_path: Explicit ``--config`` path, or None to look in ``cwd``.
        cwd: The current working directory.

    Returns:
        A ProjectConfig with validated config, project root, and contained roots.

    Raises:
        ConfigError: If the file is missing, invalid, has unknown keys, or names a
            docs root that resolves outside the project root.
    """
    if config_path is not None:
        if not config_path.exists():
            msg = f"config file not found: {config_path}"
            raise ConfigError(msg)
        raw = _read_yaml(config_path)
        project_root = config_path.resolve().parent
    else:
        candidate = cwd / DEFAULT_CONFIG_NAME
        if candidate.exists():
            raw = _read_yaml(candidate)
            project_root = candidate.resolve().parent
        else:
            raw = {}
            project_root = cwd.resolve()

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        msg = f"invalid config: {exc}"
        raise ConfigError(msg) from exc

    roots = _resolve_roots(config.docs_roots, project_root)
    return ProjectConfig(config=config, project_root=project_root, resolved_roots=roots)


def _read_yaml(path: Path) -> object:
    yaml = YAML(typ="safe")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read config {path}: {exc}"
        raise ConfigError(msg) from exc
    try:
        data = yaml.load(text)
    except YAMLError as exc:
        msg = f"cannot parse config {path}: {exc}"
        raise ConfigError(msg) from exc
    return data if data is not None else {}


def _resolve_roots(roots: list[str], project_root: Path) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for entry in roots:
        candidate = Path(entry)
        full = candidate if candidate.is_absolute() else project_root / candidate
        try:
            safe = safe_resolve(full, project_root)
        except ValueError as exc:
            msg = (
                f"docs_roots entry {entry!r} resolves outside the project root "
                f"{project_root}; roots must stay inside the project"
            )
            raise ConfigError(msg) from exc
        resolved.append(safe)
    return tuple(resolved)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/doc_lattice/config.py tests/test_config.py
git commit -m "feat: add config loading with project-root containment"
```

---

## Task 8: Frontmatter parser

**Files:**
- Create: `src/doc_lattice/frontmatter_parser.py`
- Test: `tests/test_frontmatter_parser.py`

**Interfaces:**
- Consumes: `NodeMeta` from `model`; `ConfigError`, `UnreadableDocError`.
- Produces: `split_frontmatter(text) -> tuple[str | None, str]`, `parse_meta(raw_meta, source) -> NodeMeta | None`.

This is the one module permitted to use `Any` (its name ends in `_parser`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_frontmatter_parser.py`:

```python
"""Tests for frontmatter parsing."""

from pathlib import Path

import pytest

from doc_lattice.error_types import ConfigError, UnreadableDocError
from doc_lattice.frontmatter_parser import parse_meta, split_frontmatter

DOC = "---\nid: pc\ntitle: PC\n---\n# Body\ntext\n"


def test_split_frontmatter_separates_meta_and_body():
    raw, body = split_frontmatter(DOC)
    assert raw == "id: pc\ntitle: PC\n"
    assert body == "# Body\ntext\n"


def test_split_frontmatter_none_when_absent():
    raw, body = split_frontmatter("# No frontmatter\n")
    assert raw is None
    assert body == "# No frontmatter\n"


def test_split_frontmatter_tolerates_bom():
    raw, body = split_frontmatter("﻿---\nid: x\n---\nbody\n")
    assert raw == "id: x\n"


def test_parse_meta_returns_node():
    meta = parse_meta("id: pc\ntitle: PC\n", Path("a.md"))
    assert meta is not None
    assert meta.id == "pc"


def test_parse_meta_none_without_id():
    assert parse_meta("title: no id here\n", Path("a.md")) is None
    assert parse_meta(None, Path("a.md")) is None


def test_parse_meta_unknown_key_raises():
    with pytest.raises(ConfigError):
        parse_meta("id: x\nbogus: 1\n", Path("a.md"))


def test_parse_meta_bad_yaml_raises():
    with pytest.raises(UnreadableDocError):
        parse_meta("id: [unclosed\n", Path("a.md"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_frontmatter_parser.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/frontmatter_parser.py`:

```python
"""Boundary module: split and validate untyped YAML frontmatter into typed NodeMeta."""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .error_types import ConfigError, UnreadableDocError
from .model import NodeMeta

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a document into its YAML frontmatter block and body.

    Args:
        text: The full file text.

    Returns:
        ``(raw_meta, body)`` where ``raw_meta`` is the YAML between the opening and
        closing ``---`` fences (or None if the file does not open with a fence), and
        ``body`` is everything after the closing fence (the whole text if no fence).
    """
    stripped = text.lstrip("﻿")
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            raw_meta = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            return raw_meta + "\n" if raw_meta else "", body
    return None, text


def parse_meta(raw_meta: str | None, source: Path) -> NodeMeta | None:
    """Validate a raw frontmatter block into NodeMeta, or None if not a lattice node.

    Args:
        raw_meta: The YAML frontmatter text, or None.
        source: The file the frontmatter came from, for error messages.

    Returns:
        A validated NodeMeta, or None when there is no frontmatter or no ``id`` key.

    Raises:
        UnreadableDocError: If the YAML cannot be parsed.
        ConfigError: If the frontmatter has an unknown or malformed key.
    """
    if raw_meta is None:
        return None
    yaml = YAML(typ="safe")
    try:
        data: Any = yaml.load(raw_meta)
    except YAMLError as exc:
        msg = f"cannot parse frontmatter in {source}: {exc}"
        raise UnreadableDocError(msg) from exc
    if not isinstance(data, dict) or "id" not in data:
        return None
    try:
        return NodeMeta.model_validate(data)
    except ValidationError as exc:
        msg = f"invalid lattice frontmatter in {source}: {exc}"
        raise ConfigError(msg) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_frontmatter_parser.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the boundary check still passes**

Run: `uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: `PASS: typing.Any/typing.cast restricted to boundary modules`.

- [ ] **Step 6: Commit**

```bash
git add src/doc_lattice/frontmatter_parser.py tests/test_frontmatter_parser.py
git commit -m "feat: add frontmatter parser boundary module"
```

---

## Task 9: Discovery

**Files:**
- Create: `src/doc_lattice/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: `safe_resolve`, `UnreadableDocError`.
- Produces: `discover_doc_paths(roots, ignore_globs) -> list[Path]`, `read_doc(path) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discovery.py`:

```python
"""Tests for discovery."""

from pathlib import Path

import pytest

from doc_lattice.discovery import discover_doc_paths, read_doc
from doc_lattice.error_types import UnreadableDocError


def test_discovers_markdown_sorted(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "b.md").write_text("b", encoding="utf-8")
    (root / "a.md").write_text("a", encoding="utf-8")
    (root / "note.txt").write_text("x", encoding="utf-8")
    found = discover_doc_paths([root], [])
    assert [p.name for p in found] == ["a.md", "b.md"]


def test_ignore_globs_exclude(tmp_path: Path):
    root = tmp_path / "docs"
    (root / "archive").mkdir(parents=True)
    (root / "keep.md").write_text("k", encoding="utf-8")
    (root / "archive" / "old.md").write_text("o", encoding="utf-8")
    found = discover_doc_paths([root], ["**/archive/**"])
    assert [p.name for p in found] == ["keep.md"]


def test_read_doc_returns_text(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_text("hello", encoding="utf-8")
    assert read_doc(p) == "hello"


def test_read_doc_non_utf8_raises(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(UnreadableDocError):
        read_doc(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_discovery.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/discovery.py`:

```python
"""Discover candidate markdown docs under contained roots, and read them as UTF-8."""

from collections.abc import Sequence
from pathlib import Path

from .error_types import UnreadableDocError


def discover_doc_paths(roots: Sequence[Path], ignore_globs: Sequence[str]) -> list[Path]:
    """Return every ``.md`` path under the roots, minus ignored matches, sorted.

    Args:
        roots: Already project-contained docs roots (from ``ProjectConfig``).
        ignore_globs: Glob patterns (relative to each root) to skip.

    Returns:
        A sorted, de-duplicated list of markdown file paths.
    """
    found: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            if _ignored(path, root, ignore_globs):
                continue
            found.add(path)
    return sorted(found)


def _ignored(path: Path, root: Path, ignore_globs: Sequence[str]) -> bool:
    rel = path.relative_to(root)
    return any(rel.match(pattern) or path.match(pattern) for pattern in ignore_globs)


def read_doc(path: Path) -> str:
    """Read a doc as UTF-8.

    Args:
        path: The file to read.

    Returns:
        The file contents as text.

    Raises:
        UnreadableDocError: If the file cannot be read or is not valid UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read doc {path}: {exc}"
        raise UnreadableDocError(msg) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_discovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/discovery.py tests/test_discovery.py
git commit -m "feat: add doc discovery and reading"
```

---

## Task 10: Orchestrate (load_lattice) and shared fixture

**Files:**
- Create: `src/doc_lattice/orchestrate.py`
- Modify: `tests/conftest.py` (add a `lattice_dir` fixture)
- Test: `tests/test_orchestrate.py`

**Interfaces:**
- Consumes: `ProjectConfig` from `config`; `discover_doc_paths`, `read_doc` from `discovery`; `split_frontmatter`, `parse_meta` from `frontmatter_parser`; `build_lattice` from `loader`; `ParsedDoc` from `model`.
- Produces: `load_lattice(project: ProjectConfig) -> Lattice`; pytest fixture `lattice_dir`.

- [ ] **Step 1: Add the shared fixture**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def lattice_dir(tmp_path: Path) -> Path:
    """Write a small synthetic lattice and return the project root.

    Layout under docs/:
      art-direction.md  -> sections {#accent} and {#motion}
      pc-design.md       -> derives_from accent (STALE) and motion (UNRECONCILED)
      gdd.md             -> derives_from a ghost ref (BROKEN)
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "art-direction.md").write_text(
        "---\nid: art-direction\nlayer: design\n---\n"
        "# Art Direction {#art-direction-top}\n\n"
        "## Accent {#accent}\naccent body v2\n\n"
        "## Motion {#motion}\nmotion body\n",
        encoding="utf-8",
    )
    (docs / "pc-design.md").write_text(
        "---\nid: pc-design\nlayer: design\n"
        "derives_from:\n"
        "  - ref: art-direction#accent\n    seen: staleseenhashstaleseenhashstale00\n"
        "  - ref: art-direction#motion\n"
        "tickets: [PC-228]\n---\n# PC Design\nbody\n",
        encoding="utf-8",
    )
    (docs / "gdd.md").write_text(
        "---\nid: gdd\nlayer: design\nderives_from:\n  - ref: ghost\n---\n# GDD\nbody\n",
        encoding="utf-8",
    )
    return tmp_path
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_orchestrate.py`:

```python
"""Tests for load_lattice wiring."""

from pathlib import Path

from doc_lattice.config import load_config
from doc_lattice.orchestrate import load_lattice


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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run --group dev pytest tests/test_orchestrate.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 4: Implement**

Create `src/doc_lattice/orchestrate.py`:

```python
"""Wire config, discovery, parsing, and loading into a Lattice."""

from .config import ProjectConfig
from .discovery import discover_doc_paths, read_doc
from .frontmatter_parser import parse_meta, split_frontmatter
from .loader import build_lattice
from .model import Lattice, ParsedDoc


def load_lattice(project: ProjectConfig) -> Lattice:
    """Discover, parse, and assemble the lattice for a project.

    Args:
        project: The loaded project config with contained docs roots.

    Returns:
        The built Lattice. Files without lattice frontmatter (no ``id``) are skipped.
    """
    parsed: list[ParsedDoc] = []
    for path in discover_doc_paths(project.resolved_roots, project.config.ignore_globs):
        text = read_doc(path)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, path)
        if meta is None:
            continue
        parsed.append(ParsedDoc(path=path, meta=meta, body=body))
    return build_lattice(parsed)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --group dev pytest tests/test_orchestrate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc_lattice/orchestrate.py tests/conftest.py tests/test_orchestrate.py
git commit -m "feat: add load_lattice orchestration and shared fixture"
```

---

## Task 11: Check

**Files:**
- Create: `src/doc_lattice/check.py`
- Test: `tests/test_check.py`

**Interfaces:**
- Consumes: `Lattice`, `EdgeState` types; `target_content` from `resolve`; `content_hash` from `hashing`.
- Produces: `EdgeStatus`, `check_lattice(lattice) -> list[EdgeStatus]`, `has_drift(statuses) -> bool`.

**Classification:** for each edge: `target_id is None` -> BROKEN; `seen is None` -> UNRECONCILED; `content_hash(target_content(...)) == seen` -> OK; else STALE.

- [ ] **Step 1: Write the failing test**

Create `tests/test_check.py`:

```python
"""Tests for check."""

from pathlib import Path

from doc_lattice.check import check_lattice, has_drift
from doc_lattice.config import load_config
from doc_lattice.orchestrate import load_lattice


def test_check_classifies_each_state(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    by_pair = {(s.source_id, s.target_ref): s.state for s in check_lattice(lat)}
    assert by_pair[("pc-design", "art-direction#accent")] == "STALE"
    assert by_pair[("pc-design", "art-direction#motion")] == "UNRECONCILED"
    assert by_pair[("gdd", "ghost")] == "BROKEN"


def test_has_drift_true_when_any_non_ok(lattice_dir: Path):
    project = load_config(None, lattice_dir)
    lat = load_lattice(project)
    assert has_drift(check_lattice(lat)) is True


def test_has_drift_false_when_all_ok():
    from doc_lattice.loader import build_lattice
    from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge
    from doc_lattice.hashing import content_hash
    from doc_lattice.sections import build_toc, section_span, section_text

    up_body = "# Up {#accent}\naccent\n"
    span = section_span(build_toc(up_body), 0, len(up_body.splitlines()))
    seen = content_hash(section_text(up_body, span))
    docs = [
        ParsedDoc(Path("up.md"), NodeMeta(id="up"), up_body),
        ParsedDoc(Path("down.md"), NodeMeta(id="down", derives_from=[RawEdge(ref="accent", seen=seen)]), "x\n"),
    ]
    statuses = check_lattice(build_lattice(docs))
    assert all(s.state == "OK" for s in statuses)
    assert has_drift(statuses) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --group dev pytest tests/test_check.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/check.py`:

```python
"""Classify every derives_from edge against its locked seen hash."""

from dataclasses import dataclass

from .constants import EdgeState
from .hashing import content_hash
from .model import Lattice
from .resolve import target_content


@dataclass(frozen=True, slots=True)
class EdgeStatus:
    """The classification of one edge."""

    source_id: str
    target_ref: str
    target_id: str | None
    state: EdgeState
    expected: str | None
    actual: str | None


def check_lattice(lattice: Lattice) -> list[EdgeStatus]:
    """Classify every edge in the lattice.

    Args:
        lattice: The built lattice.

    Returns:
        One EdgeStatus per edge, in node-id then edge order.
    """
    statuses: list[EdgeStatus] = []
    for node_id in sorted(lattice.nodes_by_id):
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            statuses.append(_classify(lattice, node_id, edge.target_ref, edge.target_id, edge.seen))
    return statuses


def _classify(
    lattice: Lattice, source_id: str, target_ref: str, target_id: str | None, seen: str | None
) -> EdgeStatus:
    if target_id is None:
        return EdgeStatus(source_id, target_ref, None, "BROKEN", seen, None)
    actual = content_hash(target_content(lattice, target_id))
    if seen is None:
        return EdgeStatus(source_id, target_ref, target_id, "UNRECONCILED", None, actual)
    state: EdgeState = "OK" if actual == seen else "STALE"
    return EdgeStatus(source_id, target_ref, target_id, state, seen, actual)


def has_drift(statuses: list[EdgeStatus]) -> bool:
    """Return True if any edge is not OK.

    Args:
        statuses: Output of ``check_lattice``.

    Returns:
        True when any edge is STALE, UNRECONCILED, or BROKEN.
    """
    return any(s.state != "OK" for s in statuses)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --group dev pytest tests/test_check.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/check.py tests/test_check.py
git commit -m "feat: add edge drift check"
```

---

## Task 12: Impact

**Files:**
- Create: `src/doc_lattice/impact.py`
- Test: `tests/test_impact.py`

**Interfaces:**
- Consumes: `Lattice`, `Node`; `split_ref` from `resolve`.
- Produces: `expand_targets(lattice, token) -> set[str]`, `impact(lattice, token) -> list[Node]`.

**Expansion:** a file-id token expands to that id plus all section anchors in the same file; a section-anchor token expands to that anchor plus its anchored ancestors (`lattice.ancestors`). Then reverse-walk `dependents` transitively, enqueueing each affected node's id and its anchors as further targets.

- [ ] **Step 1: Write the failing test**

Create `tests/test_impact.py`:

```python
"""Tests for impact."""

from pathlib import Path

from doc_lattice.impact import expand_targets, impact
from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge


def _doc(path: str, body: str, **meta) -> ParsedDoc:
    return ParsedDoc(Path(path), NodeMeta(**meta), body)


def test_section_token_expands_to_ancestors():
    body = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert expand_targets(lat, "child") == {"child", "parent"}


def test_file_token_expands_to_its_anchors():
    body = "# A {#a-top}\n\n## Sec {#sec}\nx\n"
    lat = build_lattice([_doc("a.md", body, id="a")])
    assert expand_targets(lat, "a") == {"a", "a-top", "sec"}


def test_impact_includes_parent_dependents_for_nested_edit():
    parent = "# Parent {#parent}\n\n## Child {#child}\nx\n"
    lat = build_lattice([
        _doc("up.md", parent, id="up"),
        _doc("d-parent.md", "x\n", id="d-parent", derives_from=[RawEdge(ref="parent")]),
        _doc("d-child.md", "x\n", id="d-child", derives_from=[RawEdge(ref="child")]),
    ])
    affected = {n.id for n in impact(lat, "child")}
    assert affected == {"d-parent", "d-child"}


def test_impact_is_transitive():
    lat = build_lattice([
        _doc("up.md", "# Up {#u}\nx\n", id="up"),
        _doc("mid.md", "x\n", id="mid", derives_from=[RawEdge(ref="u")]),
        _doc("low.md", "x\n", id="low", derives_from=[RawEdge(ref="mid")]),
    ])
    assert {n.id for n in impact(lat, "u")} == {"mid", "low"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --group dev pytest tests/test_impact.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/impact.py`:

```python
"""Reverse-walk the lattice to find every doc affected by a change to a target."""

from .model import Lattice, Node
from .resolve import split_ref


def expand_targets(lattice: Lattice, token: str) -> set[str]:
    """Expand an impact token into the full set of target ids it touches.

    Args:
        lattice: The built lattice.
        token: A bare id or ``namespace#id`` ref naming a file or section anchor.

    Returns:
        For a file id: the id plus all section anchors in its file. For a section
        anchor: the anchor plus its anchored ancestors. Empty if the id is unknown.
    """
    target_id = split_ref(token)
    location = lattice.index.get(target_id)
    if location is None:
        return set()
    if location.kind == "file":
        return {target_id} | _anchors_in_file(lattice, location.path)
    return {target_id} | set(lattice.ancestors.get(target_id, ()))


def impact(lattice: Lattice, token: str) -> list[Node]:
    """Return every downstream node affected by a change to ``token``.

    Args:
        lattice: The built lattice.
        token: A bare id or ``namespace#id`` ref.

    Returns:
        Affected nodes, sorted by id, walking ``dependents`` transitively.
    """
    queue = list(expand_targets(lattice, token))
    visited_targets: set[str] = set()
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
            queue.append(source_id)
            node = lattice.nodes_by_id.get(source_id)
            if node is not None:
                queue.extend(_anchors_in_file(lattice, node.path))
    return [lattice.nodes_by_id[i] for i in sorted(affected)]


def _anchors_in_file(lattice: Lattice, path: object) -> set[str]:
    return {
        anchor
        for anchor, loc in lattice.index.items()
        if loc.kind == "section" and loc.path == path
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --group dev pytest tests/test_impact.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/impact.py tests/test_impact.py
git commit -m "feat: add impact reverse-walk with ancestor expansion"
```

---

## Task 13: Reconcile

**Files:**
- Create: `src/doc_lattice/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `Lattice`; `target_content` from `resolve`; `content_hash` from `hashing`; `split_frontmatter` from `frontmatter_parser`; `BrokenRefError`.
- Produces: `apply_reconcile(current_file_text, updates) -> str`, `reconcile(lattice, downstream_id, *, ref, reconcile_all) -> dict[Path, dict[str, str]]`.

**Behavior:** `reconcile` selects edges (one node, one ref, or all STALE/UNRECONCILED), computes each new `seen` from the loaded lattice's upstream content, then groups by downstream file. For each file it re-reads fresh, calls `apply_reconcile` (which round-trips only the frontmatter and reattaches the fresh body), and writes atomically. A BROKEN edge cannot be reconciled.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reconcile.py`:

```python
"""Tests for reconcile."""

from pathlib import Path

import pytest

from doc_lattice.check import check_lattice, has_drift
from doc_lattice.config import load_config
from doc_lattice.error_types import BrokenRefError
from doc_lattice.orchestrate import load_lattice
from doc_lattice.reconcile import apply_reconcile, reconcile


def test_apply_reconcile_sets_seen_and_preserves_body():
    text = (
        "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\n"
        "# Body\nkeep me\n"
    )
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
        path.write_text(apply_reconcile(path.read_text(encoding="utf-8"), updates), encoding="utf-8")
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --group dev pytest tests/test_reconcile.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/reconcile.py`:

```python
"""Reconcile edges: recompute upstream hashes and rewrite seen scalars in place."""

import io
from collections import defaultdict
from pathlib import Path

from ruamel.yaml import YAML

from .error_types import BrokenRefError
from .frontmatter_parser import split_frontmatter
from .hashing import content_hash
from .model import Lattice
from .resolve import target_content


def reconcile(
    lattice: Lattice, downstream_id: str, *, ref: str | None, reconcile_all: bool
) -> dict[Path, dict[str, str]]:
    """Plan the seen-scalar updates needed to clear drift for the selection.

    Args:
        lattice: The built lattice (its upstream content is the reconcile snapshot).
        downstream_id: The node whose edges to reconcile (ignored if ``reconcile_all``).
        ref: A single upstream ref to narrow to, or None for all of the node's edges.
        reconcile_all: Reconcile every node's STALE or UNRECONCILED edges.

    Returns:
        A mapping of downstream file path to ``{target_ref: new_seen}`` updates. The
        caller applies these via ``apply_reconcile`` and an atomic write (the CLI does).

    Raises:
        BrokenRefError: If a selected edge has no resolvable target.
    """
    node_ids = sorted(lattice.nodes_by_id) if reconcile_all else [downstream_id]
    plan: dict[Path, dict[str, str]] = defaultdict(dict)
    for node_id in node_ids:
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            if ref is not None and edge.target_ref != ref:
                continue
            if edge.target_id is None:
                msg = f"cannot reconcile broken ref {edge.target_ref!r} on {node_id}; fix the ref first"
                raise BrokenRefError(msg)
            new_seen = content_hash(target_content(lattice, edge.target_id))
            plan[node.path][edge.target_ref] = new_seen
    return dict(plan)


def apply_reconcile(current_file_text: str, updates: dict[str, str]) -> str:
    """Return ``current_file_text`` with matching edges' seen scalars set.

    Args:
        current_file_text: A fresh read of the downstream file at write time.
        updates: ``{target_ref: new_seen}`` for edges in this file.

    Returns:
        The file text with only the matching ``seen`` scalars changed; the body after
        the closing fence is reattached verbatim from ``current_file_text``.
    """
    raw_meta, body = split_frontmatter(current_file_text)
    if raw_meta is None:
        return current_file_text
    yaml = YAML(typ="rt")
    data = yaml.load(raw_meta)
    for entry in data.get("derives_from", []):
        if entry.get("ref") in updates:
            entry["seen"] = updates[entry["ref"]]
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    return f"---\n{buffer.getvalue()}---\n{body}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --group dev pytest tests/test_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/reconcile.py tests/test_reconcile.py
git commit -m "feat: add reconcile with in-place fresh-read rewrite"
```

---

## Task 14: Render

**Files:**
- Create: `src/doc_lattice/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `Lattice`.
- Produces: `to_mermaid(lattice, stale_edges) -> str`, `to_dot(lattice, stale_edges) -> str`. `stale_edges` is a set of `(source_id, target_id)` pairs styled distinctly.

- [ ] **Step 1: Write the failing test**

Create `tests/test_render.py`:

```python
"""Tests for graph rendering."""

from pathlib import Path

from doc_lattice.loader import build_lattice
from doc_lattice.model import NodeMeta, ParsedDoc, RawEdge
from doc_lattice.render import to_dot, to_mermaid


def _lattice():
    return build_lattice([
        ParsedDoc(Path("up.md"), NodeMeta(id="up", title="Up"), "# Up {#u}\nx\n"),
        ParsedDoc(Path("down.md"), NodeMeta(id="down"), "x\n") if False else
        ParsedDoc(Path("down.md"), NodeMeta(id="down", derives_from=[RawEdge(ref="u")]), "x\n"),
    ])


def test_mermaid_has_nodes_and_edges():
    out = to_mermaid(_lattice(), set())
    assert out.startswith("graph TD")
    assert "up" in out and "down" in out
    assert "-->" in out


def test_mermaid_styles_stale_edges():
    out = to_mermaid(_lattice(), {("down", "u")})
    assert "-.->" in out  # dashed arrow for stale


def test_dot_is_digraph():
    out = to_dot(_lattice(), set())
    assert out.startswith("digraph lattice")
    assert "->" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --group dev pytest tests/test_render.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement**

Create `src/doc_lattice/render.py`:

```python
"""Render the lattice as Mermaid or DOT."""

from .model import Lattice


def _label(lattice: Lattice, node_id: str) -> str:
    node = lattice.nodes_by_id.get(node_id)
    title = node.title if node is not None and node.title else node_id
    return title.replace('"', "'")


def to_mermaid(lattice: Lattice, stale_edges: set[tuple[str, str]]) -> str:
    """Render a Mermaid ``graph TD``.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw with a dashed arrow.

    Returns:
        Mermaid source. Edges run upstream (target) to downstream (source).
    """
    lines = ["graph TD"]
    for node_id in sorted(lattice.nodes_by_id):
        lines.append(f'    {node_id}["{_label(lattice, node_id)}"]')
    for node_id in sorted(lattice.nodes_by_id):
        for edge in lattice.nodes_by_id[node_id].derives_from:
            if edge.target_id is None:
                continue
            arrow = "-.->" if (node_id, edge.target_id) in stale_edges else "-->"
            lines.append(f"    {edge.target_id} {arrow} {node_id}")
    return "\n".join(lines) + "\n"


def to_dot(lattice: Lattice, stale_edges: set[tuple[str, str]]) -> str:
    """Render a Graphviz DOT digraph.

    Args:
        lattice: The built lattice.
        stale_edges: ``(source_id, target_id)`` pairs to draw dashed.

    Returns:
        DOT source. Edges run upstream (target) to downstream (source).
    """
    lines = ["digraph lattice {"]
    for node_id in sorted(lattice.nodes_by_id):
        lines.append(f'    "{node_id}" [label="{_label(lattice, node_id)}"];')
    for node_id in sorted(lattice.nodes_by_id):
        for edge in lattice.nodes_by_id[node_id].derives_from:
            if edge.target_id is None:
                continue
            style = ' [style=dashed]' if (node_id, edge.target_id) in stale_edges else ""
            lines.append(f'    "{edge.target_id}" -> "{node_id}"{style};')
    lines.append("}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --group dev pytest tests/test_render.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc_lattice/render.py tests/test_render.py
git commit -m "feat: add Mermaid and DOT rendering"
```

---

## Task 15: CLI

**Files:**
- Modify: `src/doc_lattice/cli.py` (remove the scaffold `hello` command, add the four commands)
- Modify: `tests/test_cli.py` (replace the `hello` test)

**Interfaces:**
- Consumes: every command module and `load_config`/`load_lattice`.
- Produces: `impact`, `check`, `reconcile`, `graph` typer commands with `--json` and meaningful exit codes.

**Exit codes for `check`:** 0 all OK; 1 drift (STALE/UNRECONCILED/BROKEN); 2 tool/config error. `ProjectError` raised by any command maps to exit 2 with the message on stderr.

- [ ] **Step 1: Write the failing tests**

Replace the contents of `tests/test_cli.py` with:

```python
"""Tests for the CLI."""

import json
from pathlib import Path

from typer.testing import CliRunner

from doc_lattice.cli import app

runner = CliRunner()


def test_check_exits_1_on_drift(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1


def test_check_json_reports_states(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["check", "--json"])
    payload = json.loads(result.stdout)
    states = {(e["source_id"], e["target_ref"]): e["state"] for e in payload["edges"]}
    assert states[("gdd", "ghost")] == "BROKEN"


def test_check_exits_2_on_bad_config(tmp_path: Path, monkeypatch):
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots: ['../x']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 2


def test_impact_lists_dependents(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["impact", "accent", "--json"])
    payload = json.loads(result.stdout)
    assert "pc-design" in {n["id"] for n in payload["affected"]}


def test_graph_emits_mermaid(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert result.stdout.startswith("graph TD")


def test_reconcile_then_check_clean(lattice_dir: Path, monkeypatch):
    monkeypatch.chdir(lattice_dir)
    assert runner.invoke(app, ["reconcile", "pc-design"]).exit_code == 0
    after = runner.invoke(app, ["check"])
    # gdd's BROKEN ref still drifts, so check is still 1; pc-design itself is clean.
    pc = runner.invoke(app, ["check", "--json"])
    payload = json.loads(pc.stdout)
    pc_states = [e["state"] for e in payload["edges"] if e["source_id"] == "pc-design"]
    assert pc_states == ["OK", "OK"]
    assert after.exit_code == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cli.py -v`
Expected: FAIL (no such commands / import errors).

- [ ] **Step 3: Implement**

Replace the contents of `src/doc_lattice/cli.py` with:

```python
"""Command-line interface."""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .check import check_lattice, has_drift
from .config import load_config
from .error_types import ProjectError
from .impact import impact as impact_walk
from .orchestrate import load_lattice
from .reconcile import apply_reconcile, reconcile as plan_reconcile
from .render import to_dot, to_mermaid

app = typer.Typer(no_args_is_help=True, add_completion=False)
_out = Console()
_err = Console(stderr=True)

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to .doc-lattice.yml.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _version_callback(value: bool) -> None:
    if value:
        _out.print(__version__)
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True,
                     help="Show the version and exit."),
    ] = False,
) -> None:
    """doc-lattice: documentation traceability engine."""


def _load(config: Path | None):
    project = load_config(config, Path.cwd())
    return load_lattice(project)


@app.command()
def check(config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """Classify every edge; exit 1 on drift, 2 on tool error."""
    try:
        lattice = _load(config)
        statuses = check_lattice(lattice)
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {exc} ({exc.code})")
        raise typer.Exit(2) from exc
    if json_out:
        payload = {"edges": [
            {"source_id": s.source_id, "target_ref": s.target_ref,
             "target_id": s.target_id, "state": s.state,
             "expected": s.expected, "actual": s.actual}
            for s in statuses
        ]}
        _out.print_json(json.dumps(payload))
    else:
        for s in statuses:
            color = {"OK": "green", "STALE": "yellow", "UNRECONCILED": "yellow", "BROKEN": "red"}[s.state]
            _out.print(f"[{color}]{s.state:<13}[/{color}] {s.source_id} -> {s.target_ref}")
    raise typer.Exit(1 if has_drift(statuses) else 0)


@app.command()
def impact(token: str, config: ConfigOpt = None, json_out: JsonOpt = False) -> None:
    """List every downstream doc affected by a change to TOKEN."""
    try:
        lattice = _load(config)
        affected = impact_walk(lattice, token)
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {exc} ({exc.code})")
        raise typer.Exit(2) from exc
    if json_out:
        payload = {"affected": [
            {"id": n.id, "title": n.title, "path": str(n.path), "tickets": list(n.tickets)}
            for n in affected
        ]}
        _out.print_json(json.dumps(payload))
    else:
        for n in affected:
            tickets = ", ".join(n.tickets) if n.tickets else "-"
            _out.print(f"{n.id}  ({n.path})  tickets: {tickets}")


@app.command()
def reconcile(
    downstream_id: str,
    ref: Annotated[str | None, typer.Option("--ref", help="Reconcile only this upstream ref.")] = None,
    reconcile_all: Annotated[bool, typer.Option("--all", help="Reconcile every drifting edge.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Set seen to current upstream hashes for the selected edges."""
    try:
        lattice = _load(config)
        plan = plan_reconcile(lattice, downstream_id, ref=ref, reconcile_all=reconcile_all)
        for path, updates in plan.items():
            fresh = path.read_text(encoding="utf-8")
            new_text = apply_reconcile(fresh, updates)
            _atomic_write(path, new_text)
            for target_ref in updates:
                _out.print(f"reconciled {path.name}: {target_ref}")
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {exc} ({exc.code})")
        raise typer.Exit(2) from exc


@app.command()
def graph(
    fmt: Annotated[str, typer.Option("--format", help="mermaid or dot.")] = "mermaid",
    config: ConfigOpt = None,
) -> None:
    """Emit the edge graph as Mermaid or DOT."""
    try:
        lattice = _load(config)
        stale = {
            (s.source_id, s.target_id)
            for s in check_lattice(lattice)
            if s.state == "STALE" and s.target_id is not None
        }
    except ProjectError as exc:
        _err.print(f"[red]error[/red]: {exc} ({exc.code})")
        raise typer.Exit(2) from exc
    rendered = to_dot(lattice, stale) if fmt == "dot" else to_mermaid(lattice, stale)
    _out.print(rendered, end="")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    """Console-script entry point."""
    app()
```

Note on output and tests: the `check --json` test parses `result.stdout`; `Console.print_json` prints to stdout. If `print_json` reformatting interferes with parsing, switch the JSON branch to `typer.echo(json.dumps(payload))` for byte-exact output. Apply that swap in `check` and `impact` if the JSON tests fail to parse.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and the boundary check**

Run: `uv run --group dev pytest`
Expected: all tests PASS, coverage at or above 80 percent.
Run: `uv run --group dev python scripts/check_typing_boundaries.py src`
Expected: PASS.
Run: `uv run --group dev ruff check src tests` and `uv run --group dev ruff format --check src tests`
Expected: clean (fix any reported issues).

- [ ] **Step 6: Commit**

```bash
git add src/doc_lattice/cli.py tests/test_cli.py
git commit -m "feat: wire impact, check, reconcile, and graph CLI commands"
```

---

## Self-Review

**Spec coverage** (each spec section maps to a task):

- Data model (spec 2) -> Task 2; id namespace and ref resolution (2.1) -> Tasks 5, 6; edge identity (2.2) -> Tasks 6, 13.
- Architecture and module decomposition (spec 3) -> Tasks 2-15; pure/impure split -> Tasks 6/10 (loader/orchestrate) and 13 (reconcile apply vs write).
- Loading and indexing (spec 4), incl. root containment and frontmatter rules -> Tasks 7, 8, 9, 10; broken-ref-is-not-a-load-error -> Task 6.
- Section extraction and hashing (spec 5), incl. nested spans and canonicalization -> Tasks 3, 4.
- Commands (spec 6): check -> Task 11; impact with ancestor expansion -> Task 12; reconcile in-place fresh-read -> Task 13; graph -> Task 14; CLI exit codes and `--json` -> Task 15.
- Configuration (spec 7) -> Task 7. Error handling (spec 8) -> Task 1, threaded through. Security (spec 9): root containment -> Task 7; safe YAML -> Tasks 7, 8; safe_resolve -> Tasks 7, 9.
- Testing (spec 10): synthetic fixture -> Task 10 conftest; per-module tests -> each task; hypothesis -> Task 3; CLI exit codes -> Task 15; reconcile round-trip and concurrency -> Task 13; uniqueness -> Task 6; impact ancestor expansion -> Task 12; broken-ref `check --json` -> Task 15; docs-root containment -> Tasks 7, 15.
- Dependencies (spec 11): ruamel.yaml -> Task 7.

**Placeholder scan:** no TBD/TODO; every code step shows complete code; tests are concrete.

**Type consistency:** names verified against the locked vocabulary block. `EdgeStatus` fields, `Lattice` attributes (`nodes_by_id`, `index`, `dependents`, `ancestors`), `Edge.target_id: str | None`, and the `reconcile`/`apply_reconcile` signatures match across Tasks 2, 6, 11, 12, 13, 14, 15.

Two implementation notes are flagged inline for the executor to resolve if a tool complains: the `_node_for_path` return annotation in Task 5 (ty), and the `check --json` output function in Task 15 (`print_json` vs `echo`). Both have the fix written next to them.
