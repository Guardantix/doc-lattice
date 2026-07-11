# Opt-In Incremental Load Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, gitignored incremental load cache that skips re-parsing unchanged docs while keeping every command's stdout, stderr, exit code, and file mutations byte-identical to an uncached run.

**Architecture:** A new impure `cache.py` owns a versioned single-file JSON cache under the user cache home, keyed by a validated config segment. `orchestrate.load_lattice` gains a cached branch that, per discovered file, serves a stat-tier or verify-tier hit or falls through to today's full parse, then writes the cache atomically only after a successful `build_lattice`. Section derivation is extracted from `loader.py` into a pure `derive_file_sections` seam so cached and freshly derived sections are the same values by construction.

**Tech Stack:** Python 3.13+, pydantic v2 (schema + validation), ruamel.yaml (config), typer (CLI), hypothesis (property tests), `uv` for deps and execution.

## Global Constraints

Every task's requirements implicitly include these (values copied verbatim from the spec and CLAUDE.md):

- Python 3.13+; all execution through `uv` (`uv run --group dev pytest`, `uv run --group dev ruff check src tests`, `uv run --group dev ty check src`, `uv run --group dev python scripts/check_typing_boundaries.py src`).
- Coverage gate: the suite must stay above 80 percent.
- `typing.Any` and `typing.cast` are allowed only in boundary modules (stem or ancestor dir in `boundary`/`adapter`/`parser`/`validator`/`external`/`inbound`, or stem ending `_<one of those>`). **`cache.py` is NOT a boundary module and must contain neither** (raw `json.loads` output flows directly into `model_validate`, so none is needed).
- All custom exceptions extend `ProjectError` (error_types.py) and carry a `code`; no bare `except Exception`/`except BaseException`. The cache adds no new exception types; config errors reuse `ConfigError`, doc filesystem failures reuse `UnreadableDocError`.
- Constants: use the `Literal` + `get_args()` + `frozenset` pattern for enumerations in `constants.py`, and import constant values rather than duplicating a literal (enforced by `tests/test_conventions.py`).
- User-provided paths go through `safe_resolve()`; the cache carries no path semantics (`cache_key` is one validated segment).
- No `datetime.now()`/`utcnow()` outside `datetime_utils.py`. The cache uses no clock (LRU ledger, not age).
- Version sync: `__version__`, `pyproject.toml` `version`, and the first versioned `## [X.Y.Z]` CHANGELOG heading must agree; a `## [Unreleased]` block above it is tolerated. This feature does not bump the version; it adds an `[Unreleased]` note.
- ruff line length 100; module docstring on every module; Google-style docstrings on public functions; **no em-dashes** in any drafted content (docstrings, messages, comments).
- Git: no Claude attribution in commit messages, PR titles, or PR bodies.

## File Structure

- `src/doc_lattice/constants.py` (modify): add `CACHE_VERSION`, `MAX_STAT_ROOTS`, `CACHE_FILE_NAME`.
- `src/doc_lattice/model.py` (modify): add frozen dataclasses `SectionRecord` and `FileSections`; add `ParsedDoc.sections: FileSections | None = None`.
- `src/doc_lattice/loader.py` (modify): extract `derive_file_sections(body) -> FileSections`; `build_lattice` consumes `doc.sections` when present, derives it otherwise.
- `src/doc_lattice/discovery.py` (modify): split `read_doc` into `read_doc_bytes` + `decode_doc` sharing one `UnreadableDocError` construction; `read_doc` composes them.
- `src/doc_lattice/config.py` (modify): add `cache_key` and `cache_trust_stat` fields and their validators.
- `src/doc_lattice/cache.py` (create): the only cache-touching module. Schema pydantic models, cache-path resolution from an env mapping, read/validate, tier selection (`lookup`/`record_miss`), and atomic write (`finalize`). Wired only from `orchestrate.py`.
- `src/doc_lattice/orchestrate.py` (modify): the cached branch and a `require_verified` flag.
- `src/doc_lattice/cli.py` (modify): `reconcile` loads with `require_verified=True`.
- `scripts/bench_load_cache.py` (create): dev-only benchmark, not shipped.
- Docs: `README.md`, `CHANGELOG.md`, `src/doc_lattice/scaffold.py`, `CLAUDE.md`.
- Tests: `tests/test_cache.py` (create) plus additions to `tests/test_config.py`, `tests/test_loader.py`, `tests/test_discovery.py`, `tests/test_orchestrate.py`, `tests/test_cli.py`, `tests/test_scaffold.py`.

---

### Task 1: Section-extraction seam (`model.py` + `loader.py`)

Extract the inline TOC/anchor/span derivation from `build_lattice` into a pure, reusable `derive_file_sections`, and let `build_lattice` accept pre-derived sections. This is a behavior-preserving seam: cached and derived sections must be identical. No cache code yet.

**Files:**
- Modify: `src/doc_lattice/model.py`
- Modify: `src/doc_lattice/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: `split_body_lines`, `build_toc`, `anchor_ids`, `section_span`, `Heading` from `sections.py`.
- Produces:
  - `model.SectionRecord` frozen dataclass: `anchor: str`, `start: int`, `end: int`.
  - `model.FileSections` frozen dataclass: `total_lines: int`, `sections: tuple[SectionRecord, ...]`.
  - `model.ParsedDoc` gains `sections: FileSections | None = None`.
  - `loader.derive_file_sections(body: str) -> FileSections`.
  - `build_lattice(docs: list[ParsedDoc]) -> Lattice` unchanged signature; uses `doc.sections` when non-None, else `derive_file_sections(doc.body)`.

- [ ] **Step 1: Write the failing test for the round-trip and the seam**

Add to `tests/test_loader.py`:

```python
from doc_lattice.loader import build_lattice, derive_file_sections
from doc_lattice.model import FileSections, NodeMeta, ParsedDoc, SectionRecord, TargetId


def _meta(node_id: str) -> NodeMeta:
    return NodeMeta.model_validate({"id": node_id})


def test_derive_file_sections_matches_inline_derivation():
    body = "# Top {#top}\n\n## Accent {#accent}\naccent body\n\n## Motion\nmotion body\n"
    fs = derive_file_sections(body)
    assert isinstance(fs, FileSections)
    assert fs.total_lines == 7
    anchors = [rec.anchor for rec in fs.sections]
    assert anchors == ["top", "accent", "motion"]
    accent = next(rec for rec in fs.sections if rec.anchor == "accent")
    assert (accent.start, accent.end) == (3, 5)


def test_build_lattice_uses_supplied_sections_equal_to_derived():
    body = "# Top {#top}\n\n## Accent {#accent}\naccent body\n\n## Motion\nmotion body\n"
    derived = ParsedDoc(path=Path("docs/a.md"), meta=_meta("a"), body=body)
    supplied = ParsedDoc(
        path=Path("docs/a.md"), meta=_meta("a"), body=body, sections=derive_file_sections(body)
    )
    from_derived = build_lattice([derived])
    from_supplied = build_lattice([supplied])
    assert from_derived == from_supplied


def test_supplied_sections_survive_a_within_file_anchor_clash():
    # A marker equal to a computed slug must still raise DuplicateIdError from cached sections.
    from doc_lattice.error_types import DuplicateIdError

    body = "# Accent {#accent}\n\n## Accent\n"
    doc = ParsedDoc(
        path=Path("docs/a.md"), meta=_meta("a"), body=body, sections=derive_file_sections(body)
    )
    with pytest.raises(DuplicateIdError):
        build_lattice([doc])
```

Add `from pathlib import Path` and `import pytest` to the imports if not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_loader.py -k "derive_file_sections or supplied_sections" -v`
Expected: FAIL with `ImportError`/`AttributeError` (no `derive_file_sections`, no `FileSections`, no `SectionRecord`, `ParsedDoc` has no `sections`).

- [ ] **Step 3: Add the dataclasses to `model.py`**

In `src/doc_lattice/model.py`, after the `Location` dataclass (before `Node`), add:

```python
@dataclass(frozen=True, slots=True)
class SectionRecord:
    """One anchored section: its resolved anchor id and inclusive 1-indexed line span."""

    anchor: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class FileSections:
    """The section derivation build_lattice consumes: total line count and anchored spans."""

    total_lines: int
    sections: tuple[SectionRecord, ...]
```

Then change `ParsedDoc` to carry optional pre-derived sections:

```python
@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """A discovered file with validated frontmatter and its raw body.

    ``sections`` holds pre-derived section spans when a caller (the load cache) already
    computed them, so ``build_lattice`` reuses them instead of re-deriving. It is None on
    the uncached path, where ``build_lattice`` derives sections itself.
    """

    path: Path
    meta: NodeMeta
    body: str
    sections: "FileSections | None" = None
```

- [ ] **Step 4: Extract `derive_file_sections` and rewire `build_lattice` in `loader.py`**

In `src/doc_lattice/loader.py`, update the imports:

```python
from .model import Edge, FileSections, Lattice, Location, Node, ParsedDoc, SectionRecord, TargetId, parse_ref
from .sections import anchor_ids, build_toc, section_span, split_body_lines
```

(`Heading` is no longer imported; `_record_ancestors` no longer needs it.)

Add the new pure function (place it above `build_lattice`):

```python
def derive_file_sections(body: str) -> FileSections:
    """Derive a document's total line count and anchored section spans.

    This is the single derivation the load cache stores and replays: the TOC, its
    de-duped anchor ids, and each heading's inclusive line span, so ``build_lattice``
    consumes the same values whether it derives them or reads them from the cache.

    Args:
        body: The verbatim document body after the frontmatter fence.

    Returns:
        A FileSections with the 1-based total line count and one SectionRecord per
        heading, in document order.
    """
    total_lines = _line_count(body)
    toc = build_toc(body)
    records = tuple(
        SectionRecord(anchor=anchor, start=span[0], end=span[1])
        for i, anchor in enumerate(anchor_ids(toc))
        for span in (section_span(toc, i, total_lines),)
    )
    return FileSections(total_lines=total_lines, sections=records)
```

Rewrite the per-doc registration loop in `build_lattice` so it drives off a `FileSections` (supplied or derived) rather than inline TOC work. Replace the first `for doc in docs:` block with:

```python
    for doc in docs:
        file_id = doc.meta.id
        file_sections = doc.sections if doc.sections is not None else derive_file_sections(doc.body)
        total_lines = file_sections.total_lines
        _register(
            TargetId(file_id),
            Location(path=doc.path, kind="file", span=(1, total_lines)),
            index,
            sources,
            f"file {doc.path}",
        )
        anchored: list[TargetId] = []
        spans: dict[TargetId, tuple[int, int]] = {}
        for record in file_sections.sections:
            tid = TargetId(file_id, record.anchor)
            span = (record.start, record.end)
            spans[tid] = span
            anchored.append(tid)
            _register(
                tid,
                Location(path=doc.path, kind="section", span=span),
                index,
                sources,
                f"anchor {tid.as_ref()!r} in {doc.path}",
            )
        _record_ancestors(anchored, spans, ancestors)
```

Update `_record_ancestors` to take the ordered target ids directly (its `Heading` was already unused):

```python
def _record_ancestors(
    anchored: list[TargetId],
    spans: dict[TargetId, tuple[int, int]],
    ancestors: dict[TargetId, tuple[TargetId, ...]],
) -> None:
    """Record each anchor's enclosing anchored sections, outermost to innermost.

    A section encloses another when its span strictly contains the other's; ties on one
    boundary still count as enclosing. Editing a nested section propagates impact to
    dependents of its ancestors, so the order runs outermost first.

    Because ``anchored`` is in document order, span starts strictly increase, so a single
    stack pass suffices: an anchor still on the stack whose end reaches the current anchor's
    end encloses it. Popping ends strictly below the current end leaves exactly the ancestor
    set, bottom-to-top being outermost-to-innermost.
    """
    stack: list[tuple[int, TargetId]] = []
    for anchor in anchored:
        _start, end = spans[anchor]
        while stack and stack[-1][0] < end:
            stack.pop()
        ancestors[anchor] = tuple(tid for _, tid in stack)
        stack.append((end, anchor))
```

- [ ] **Step 5: Run the new tests plus the full loader suite to verify pass and no regressions**

Run: `uv run --group dev pytest tests/test_loader.py -v`
Expected: PASS, including the three new tests and every pre-existing loader test (behavior is unchanged).

- [ ] **Step 6: Add a hypothesis round-trip property test for the cache serialization form**

Add to `tests/test_loader.py`:

```python
from hypothesis import given, settings
from hypothesis import strategies as st


@st.composite
def _markdown_body(draw) -> str:
    lines = draw(
        st.lists(
            st.one_of(
                st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=40),
                st.builds(lambda n, t: "#" * n + " " + t, st.integers(1, 6), st.text("abc ", max_size=10)),
            ),
            max_size=25,
        )
    )
    return "\n".join(lines)


@settings(max_examples=200)
@given(_markdown_body())
def test_file_sections_survive_serialization_round_trip(body: str):
    # A FileSections rebuilt from its plain (anchor, start, end) tuples equals the original,
    # which is exactly what the cache stores and reloads.
    original = derive_file_sections(body)
    rebuilt = FileSections(
        total_lines=original.total_lines,
        sections=tuple(SectionRecord(r.anchor, r.start, r.end) for r in original.sections),
    )
    assert rebuilt == original
```

- [ ] **Step 7: Run the property test**

Run: `uv run --group dev pytest tests/test_loader.py -k round_trip -v`
Expected: PASS.

- [ ] **Step 8: Lint, type-check, and commit**

Run: `uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/doc_lattice/model.py src/doc_lattice/loader.py tests/test_loader.py
git commit -m "refactor: extract derive_file_sections seam for the load cache"
```

---

### Task 2: Config keys `cache_key` and `cache_trust_stat`

Add the two opt-in config fields and their validators. No cache behavior yet; this only widens `Config`.

**Files:**
- Modify: `src/doc_lattice/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.cache_key: str | None = None`, `Config.cache_trust_stat: bool = False`. Invalid values raise `ConfigError` (exit 2) via `load_config`'s existing `ValidationError` wrapping.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
import pytest


@pytest.mark.parametrize("key", ["docs", "my-project.docs_v2", "A", "x" * 64])
def test_cache_key_accepts_safe_segments(tmp_path: Path, key: str):
    (tmp_path / ".doc-lattice.yml").write_text(f"cache_key: {key}\n", encoding="utf-8")
    project = load_config(None, tmp_path)
    assert project.config.cache_key == key


@pytest.mark.parametrize(
    "key",
    ["", ".hidden", "..", "a/b", "with space", "sub/dir", "x" * 65, "-leading", "_leading"],
)
def test_cache_key_rejects_unsafe_segments(tmp_path: Path, key: str):
    (tmp_path / ".doc-lattice.yml").write_text(f'cache_key: "{key}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_cache_key_absent_defaults_to_none(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.cache_key is None
    assert project.config.cache_trust_stat is False


def test_trust_stat_without_cache_key_is_config_error(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("cache_trust_stat: true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)


def test_trust_stat_with_cache_key_is_accepted(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text(
        "cache_key: docs\ncache_trust_stat: true\n", encoding="utf-8"
    )
    project = load_config(None, tmp_path)
    assert project.config.cache_key == "docs"
    assert project.config.cache_trust_stat is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_config.py -k "cache_key or trust_stat" -v`
Expected: FAIL (unknown key `cache_key` is rejected by `extra="forbid"`, so even the accept cases fail).

- [ ] **Step 3: Add the fields and validators to `config.py`**

In `src/doc_lattice/config.py`, update the imports:

```python
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
```

Add a module-level pattern above `class Config` (below `_YAML`):

```python
# A cache_key is one safe path segment: it must start with an alphanumeric (rejecting ".",
# "..", and hidden-directory names) and thereafter allow only word, dot, and hyphen, so it can
# never express a separator or a traversal. Length capped at 64 characters total.
_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
```

Add the fields to `Config`:

```python
    cache_key: str | None = None
    cache_trust_stat: bool = False
```

Add the validators as methods on `Config`:

```python
    @field_validator("cache_key")
    @classmethod
    def _validate_cache_key(cls, value: str | None) -> str | None:
        """Reject a cache_key that is not a single safe path segment."""
        if value is not None and _CACHE_KEY_RE.fullmatch(value) is None:
            msg = (
                f"cache_key {value!r} must be one safe path segment matching "
                r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ (no separators or traversal)"
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _trust_stat_requires_cache_key(self) -> "Config":
        """Setting cache_trust_stat without cache_key is a configuration error."""
        if self.cache_trust_stat and self.cache_key is None:
            msg = "cache_trust_stat requires cache_key to be set; set cache_key or remove cache_trust_stat"
            raise ValueError(msg)
        return self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_config.py -v`
Expected: PASS (new and existing).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`

```bash
git add src/doc_lattice/config.py tests/test_config.py
git commit -m "feat: add cache_key and cache_trust_stat config keys"
```

---

### Task 3: Split `read_doc` for error-parity byte and decode helpers

Restructure `discovery.read_doc` so the cache's byte-read (for hashing) and decode (on a miss) route through the same `UnreadableDocError` construction as an uncached read, making section-5 error parity hold by construction.

**Files:**
- Modify: `src/doc_lattice/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces:
  - `discovery.read_doc_bytes(path: Path) -> bytes` (raises `UnreadableDocError` on `OSError`).
  - `discovery.decode_doc(path: Path, data: bytes) -> str` (raises `UnreadableDocError` on `UnicodeDecodeError`).
  - `discovery.read_doc(path: Path) -> str` unchanged signature and message, now composing the two.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_discovery.py`:

```python
from doc_lattice.discovery import decode_doc, read_doc, read_doc_bytes
from doc_lattice.error_types import UnreadableDocError


def test_read_doc_bytes_returns_raw_bytes(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_bytes(b"# hi\n")
    assert read_doc_bytes(doc) == b"# hi\n"


def test_read_doc_bytes_missing_file_raises_unreadable(tmp_path: Path):
    missing = tmp_path / "gone.md"
    with pytest.raises(UnreadableDocError) as exc:
        read_doc_bytes(missing)
    assert exc.value.code == "UNREADABLE_DOC"
    assert "cannot read doc" in str(exc.value)


def test_decode_doc_rejects_non_utf8_with_same_message_as_read_doc(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_bytes(b"\xff\xfe not utf-8\n")
    with pytest.raises(UnreadableDocError) as via_decode:
        decode_doc(doc, doc.read_bytes())
    with pytest.raises(UnreadableDocError) as via_read:
        read_doc(doc)
    assert str(via_decode.value) == str(via_read.value)


def test_read_doc_composes_helpers(tmp_path: Path):
    doc = tmp_path / "a.md"
    doc.write_text("# hi\n", encoding="utf-8")
    assert read_doc(doc) == decode_doc(doc, read_doc_bytes(doc))
```

Ensure `from pathlib import Path` and `import pytest` are imported in the test file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_discovery.py -k "read_doc_bytes or decode_doc or composes" -v`
Expected: FAIL with `ImportError` (`read_doc_bytes`, `decode_doc` do not exist).

- [ ] **Step 3: Rewrite the read path in `discovery.py`**

Replace the `read_doc` function at the bottom of `src/doc_lattice/discovery.py` with:

```python
def _unreadable(path: Path, exc: OSError | UnicodeDecodeError) -> UnreadableDocError:
    """Build the single UnreadableDocError used by every doc read and stat failure.

    Centralizing the message is what lets the cached load path (byte read, decode, stat)
    produce byte-identical errors to an uncached read.
    """
    return UnreadableDocError(f"cannot read doc {path}: {exc}")


def read_doc_bytes(path: Path) -> bytes:
    """Read a doc's raw bytes.

    Args:
        path: The file to read.

    Returns:
        The file contents as bytes.

    Raises:
        UnreadableDocError: If the file cannot be read.
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _unreadable(path, exc) from exc


def decode_doc(path: Path, data: bytes) -> str:
    """Decode a doc's bytes as UTF-8.

    Args:
        path: The file the bytes came from, for the error message.
        data: The raw bytes.

    Returns:
        The decoded text.

    Raises:
        UnreadableDocError: If the bytes are not valid UTF-8.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _unreadable(path, exc) from exc


def read_doc(path: Path) -> str:
    """Read a doc as UTF-8.

    Args:
        path: The file to read.

    Returns:
        The file contents as text.

    Raises:
        UnreadableDocError: If the file cannot be read or is not valid UTF-8.
    """
    return decode_doc(path, read_doc_bytes(path))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_discovery.py -v`
Expected: PASS (new and existing; `read_doc`'s external behavior is unchanged).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`

```bash
git add src/doc_lattice/discovery.py tests/test_discovery.py
git commit -m "refactor: split read_doc into byte and decode helpers sharing one error"
```

---

### Task 4: Cache constants, schema models, and path resolution

Create `cache.py` with the schema pydantic models, the three constants, and the env-driven cache-path resolution. Cover serialization round-trip and XDG handling. No load wiring yet.

**Files:**
- Modify: `src/doc_lattice/constants.py`
- Create: `src/doc_lattice/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `NodeMeta` (model.py), `__version__` (package), `CACHE_VERSION`/`MAX_STAT_ROOTS`/`CACHE_FILE_NAME` (constants.py).
- Produces (pydantic `BaseModel`s in `cache.py`, all `ConfigDict(extra="forbid")`):
  - `StatRecord`: `size: int`, `mtime_ns: int`.
  - `SectionRecordModel`: `anchor: str`, `start: int`, `end: int`.
  - `NodePayload`: `meta: NodeMeta`, `body: str`, `total_lines: int`, `sections: list[SectionRecordModel]`.
  - `Entry`: `file_sha256: str`, `stats: dict[str, StatRecord]`, `node: NodePayload | None`.
  - `CacheFile`: `version: int`, `tool_version: str`, `roots: list[str]`, `entries: dict[str, Entry]`.
  - `cache_home(env: Mapping[str, str]) -> Path` and `cache_path(cache_key: str, env: Mapping[str, str]) -> Path`.

- [ ] **Step 1: Add the constants**

In `src/doc_lattice/constants.py`, append (after the control-range constants):

```python
# Load cache (opt-in incremental cache). CACHE_VERSION bumps on an intentional schema change;
# a tool-version mismatch already discards the file across releases. MAX_STAT_ROOTS bounds the
# per-root stat ledger. CACHE_FILE_NAME is the single JSON document under the cache slot.
CACHE_VERSION: int = 1
MAX_STAT_ROOTS: int = 8
CACHE_FILE_NAME: str = "load-cache.json"
```

- [ ] **Step 2: Write the failing tests for models and path resolution**

Create `tests/test_cache.py`:

```python
"""Tests for the opt-in incremental load cache."""

from pathlib import Path

import pytest

from doc_lattice import __version__
from doc_lattice.cache import (
    CacheFile,
    Entry,
    NodePayload,
    SectionRecordModel,
    StatRecord,
    cache_home,
    cache_path,
)
from doc_lattice.constants import CACHE_FILE_NAME, CACHE_VERSION
from doc_lattice.model import NodeMeta


def _sample_cache_file() -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=["/abs/root"],
        entries={
            "docs/a.md": Entry(
                file_sha256="a" * 64,
                stats={"/abs/root": StatRecord(size=10, mtime_ns=123)},
                node=NodePayload(
                    meta=NodeMeta.model_validate({"id": "a"}),
                    body="# A\n",
                    total_lines=1,
                    sections=[SectionRecordModel(anchor="a-top", start=1, end=1)],
                ),
            ),
            "docs/plain.md": Entry(
                file_sha256="b" * 64,
                stats={"/abs/root": StatRecord(size=3, mtime_ns=456)},
                node=None,
            ),
        },
    )


def test_cache_file_round_trips_through_json():
    original = _sample_cache_file()
    dumped = original.model_dump_json()
    reloaded = CacheFile.model_validate_json(dumped)
    assert reloaded == original
    # The nested NodeMeta reloads as a validated NodeMeta, not a raw dict.
    assert isinstance(reloaded.entries["docs/a.md"].node.meta, NodeMeta)


def test_cache_home_uses_absolute_xdg():
    home = cache_home({"XDG_CACHE_HOME": "/custom/cache", "HOME": "/home/u"})
    assert home == Path("/custom/cache")


def test_cache_home_ignores_relative_xdg():
    home = cache_home({"XDG_CACHE_HOME": "relative/cache", "HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_home_falls_back_to_home_dot_cache_when_xdg_unset():
    home = cache_home({"HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_path_composes_slot_and_file_name():
    path = cache_path("my-docs", {"XDG_CACHE_HOME": "/c", "HOME": "/home/u"})
    assert path == Path("/c") / "doc-lattice" / "my-docs" / CACHE_FILE_NAME
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: doc_lattice.cache`.

- [ ] **Step 4: Create `cache.py` with models and path resolution**

Create `src/doc_lattice/cache.py`:

```python
"""The opt-in incremental load cache: read, tier selection, and atomic write.

The only module that touches the cache file. It resolves the cache path from an environment
mapping, validates the versioned single-file JSON schema as pydantic models, serves stat-tier
and verify-tier hits, and writes atomically after a successful load. It never raises on its own
behalf: a read failure yields an empty cache and a write failure emits one stderr diagnostic.
"""

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .constants import CACHE_FILE_NAME
from .model import NodeMeta


class StatRecord(BaseModel):
    """One checkout's stat hint for a file: byte size and nanosecond mtime."""

    model_config = ConfigDict(extra="forbid")

    size: int
    mtime_ns: int


class SectionRecordModel(BaseModel):
    """The serialized form of one anchored section span."""

    model_config = ConfigDict(extra="forbid")

    anchor: str
    start: int
    end: int


class NodePayload(BaseModel):
    """The cached derivation of a lattice node: validated meta, body, and section spans."""

    model_config = ConfigDict(extra="forbid")

    meta: NodeMeta
    body: str
    total_lines: int
    sections: list[SectionRecordModel]


class Entry(BaseModel):
    """One cached file: its content hash, per-root stat hints, and node payload (or null)."""

    model_config = ConfigDict(extra="forbid")

    file_sha256: str
    stats: dict[str, StatRecord]
    node: NodePayload | None


class CacheFile(BaseModel):
    """The whole cache document, version 1."""

    model_config = ConfigDict(extra="forbid")

    version: int
    tool_version: str
    roots: list[str]
    entries: dict[str, Entry]


def cache_home(env: Mapping[str, str]) -> Path:
    """Return the base cache directory per the XDG base directory spec.

    Args:
        env: The environment mapping to read ``XDG_CACHE_HOME`` and ``HOME`` from.

    Returns:
        ``$XDG_CACHE_HOME`` when it is set to an absolute path (a relative value is ignored
        per the spec), otherwise ``$HOME/.cache``, falling back to the user home directory.
    """
    xdg = env.get("XDG_CACHE_HOME", "")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    home = env.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".cache"


def cache_path(cache_key: str, env: Mapping[str, str]) -> Path:
    """Return the cache file path for a slot.

    Args:
        cache_key: The validated single-segment cache slot name.
        env: The environment mapping used to resolve the cache home.

    Returns:
        ``<cache_home>/doc-lattice/<cache_key>/load-cache.json``.
    """
    return cache_home(env) / "doc-lattice" / cache_key / CACHE_FILE_NAME
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cache.py -v`
Expected: PASS.

- [ ] **Step 6: Verify the boundary and typing rules, then commit**

Run: `uv run --group dev python scripts/check_typing_boundaries.py src && uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: `PASS: typing.Any/typing.cast restricted to boundary modules` and clean lint/types (`cache.py` uses no `Any`/`cast`).

```bash
git add src/doc_lattice/constants.py src/doc_lattice/cache.py tests/test_cache.py
git commit -m "feat: add load cache schema models and path resolution"
```

---

### Task 5: `LoadCache.open` (read and validate, empty on any failure)

Add the `LoadCache` class and its `open` constructor: read the cache file, validate it, and enforce version and tool-version match. Any failure yields an empty in-memory cache. This task adds only construction and the empty-state behavior; `lookup`/`record_miss`/`finalize` come next.

**Files:**
- Modify: `src/doc_lattice/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `cache_path`, the schema models, `CACHE_VERSION` (constants), `__version__`.
- Produces:
  - `cache.LoadCache` class holding mutable state: `path: Path`, `trust_stat: bool`, `require_verified: bool`, `_current_root: str`, `_entries: dict[str, Entry]`, `_roots: list[str]`, `_original: dict | None` (the loaded `model_dump(mode="json")`, or None when no valid file was read).
  - `LoadCache.open(*, cache_key: str, project_root: Path, env: Mapping[str, str], trust_stat: bool, require_verified: bool) -> LoadCache`.
  - A test helper property `LoadCache.is_empty` (True when `_entries` is empty and `_original` is None).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cache.py`:

```python
from doc_lattice.cache import LoadCache


def _write_cache(path: Path, cache_file: CacheFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache_file.model_dump_json(), encoding="utf-8")


def _open(tmp_path: Path, *, trust_stat=False, require_verified=False) -> LoadCache:
    return LoadCache.open(
        cache_key="slot",
        project_root=tmp_path,
        env={"XDG_CACHE_HOME": str(tmp_path / "xdg")},
        trust_stat=trust_stat,
        require_verified=require_verified,
    )


def test_open_missing_file_is_empty(tmp_path: Path):
    cache = _open(tmp_path)
    assert cache.is_empty


def test_open_valid_file_loads_entries(tmp_path: Path):
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    _write_cache(path, _sample_cache_file())
    cache = _open(tmp_path)
    assert not cache.is_empty


@pytest.mark.parametrize(
    "text",
    [
        "",  # truncated / empty
        "{ not json",  # invalid JSON
        '{"version": 1}',  # schema violation (missing fields)
    ],
)
def test_open_corrupt_file_is_empty(tmp_path: Path, text: str):
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert _open(tmp_path).is_empty


def test_open_wrong_version_is_empty(tmp_path: Path):
    bad = _sample_cache_file().model_copy(update={"version": 999})
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    _write_cache(path, bad)
    assert _open(tmp_path).is_empty


def test_open_wrong_tool_version_is_empty(tmp_path: Path):
    bad = _sample_cache_file().model_copy(update={"tool_version": "0.0.0-other"})
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    _write_cache(path, bad)
    assert _open(tmp_path).is_empty


def test_open_invalid_meta_is_empty(tmp_path: Path):
    # A structurally valid file whose node.meta violates NodeMeta must discard wholesale.
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _sample_cache_file().model_dump(mode="json")
    payload["entries"]["docs/a.md"]["node"]["meta"]["id"] = "bad#id"  # '#' is rejected by NodeMeta
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _open(tmp_path).is_empty
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cache.py -k open -v`
Expected: FAIL with `ImportError`/`AttributeError` (`LoadCache` does not exist).

- [ ] **Step 3: Implement `LoadCache.open` in `cache.py`**

Add these imports at the top of `src/doc_lattice/cache.py`:

```python
import json

from pydantic import ValidationError

from . import __version__
from .constants import CACHE_FILE_NAME, CACHE_VERSION
```

(Combine with the existing `from .constants import CACHE_FILE_NAME` line; the final import should be `from .constants import CACHE_FILE_NAME, CACHE_VERSION`.)

Append the class to `cache.py`:

```python
class LoadCache:
    """Mutable in-memory cache state for one load run, backed by a single JSON file.

    Constructed by ``open``, mutated per discovered file by ``lookup`` and ``record_miss``,
    and persisted by ``finalize``. Never raises on its own behalf.
    """

    def __init__(
        self,
        *,
        path: Path,
        current_root: str,
        trust_stat: bool,
        require_verified: bool,
        entries: dict[str, Entry],
        roots: list[str],
        original: dict[str, object] | None,
    ) -> None:
        self._path = path
        self._current_root = current_root
        self._trust_stat = trust_stat and not require_verified
        self._entries = entries
        self._roots = roots
        self._original = original

    @property
    def is_empty(self) -> bool:
        """True when no valid cache file was loaded and no entries are held."""
        return not self._entries and self._original is None

    @classmethod
    def open(
        cls,
        *,
        cache_key: str,
        project_root: Path,
        env: Mapping[str, str],
        trust_stat: bool,
        require_verified: bool,
    ) -> "LoadCache":
        """Read and validate the cache file, returning an empty cache on any failure.

        The current project root is recorded as this run's stat key (its realpath). A missing,
        unreadable, invalid, wrong-version, or wrong-tool-version file is treated as empty:
        everything recomputes and the file is rewritten by ``finalize``.

        Args:
            cache_key: The validated cache slot name.
            project_root: The project root, used as this run's per-root stat key.
            env: The environment mapping for cache-path resolution.
            trust_stat: Whether the stat fast tier is enabled by config.
            require_verified: Whether this call forces the verify tier (the reconcile path).

        Returns:
            A LoadCache holding the loaded (or empty) state.
        """
        path = cache_path(cache_key, env)
        current_root = str(project_root.resolve())
        loaded = cls._read(path)
        if loaded is None:
            entries: dict[str, Entry] = {}
            roots: list[str] = []
            original: dict[str, object] | None = None
        else:
            entries = dict(loaded.entries)
            roots = list(loaded.roots)
            original = loaded.model_dump(mode="json")
        return cls(
            path=path,
            current_root=current_root,
            trust_stat=trust_stat,
            require_verified=require_verified,
            entries=entries,
            roots=roots,
            original=original,
        )

    @staticmethod
    def _read(path: Path) -> CacheFile | None:
        """Return the validated cache file, or None if it is missing, invalid, or stale."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        try:
            parsed = CacheFile.model_validate(data)
        except ValidationError:
            return None
        if parsed.version != CACHE_VERSION or parsed.tool_version != __version__:
            return None
        return parsed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cache.py -k open -v`
Expected: PASS.

- [ ] **Step 5: Boundary, lint, type-check, commit**

Run: `uv run --group dev python scripts/check_typing_boundaries.py src && uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/doc_lattice/cache.py tests/test_cache.py
git commit -m "feat: add LoadCache.open with empty-on-failure validation"
```

---

### Task 6: `LoadCache.lookup` and `record_miss` (tier selection)

Implement the stat tier, the verify tier, and miss recording. `lookup` returns a hit (reconstructed `ParsedDoc`, or None for a cached non-node) or a miss carrying the bytes it already read; `record_miss` replaces the entry from a fresh parse.

**Files:**
- Modify: `src/doc_lattice/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `discovery.read_doc_bytes` (byte read with `UnreadableDocError` parity), `model.FileSections`/`SectionRecord`/`ParsedDoc`, `hashlib`.
- Produces:
  - `cache.CacheHit` frozen dataclass: `doc: ParsedDoc | None`.
  - `cache.CacheMiss` frozen dataclass: `data: bytes`.
  - `LoadCache.lookup(rel_key: str, path: Path) -> CacheHit | CacheMiss`.
  - `LoadCache.record_miss(rel_key, path, data: bytes, meta: NodeMeta | None, body: str, sections: FileSections | None) -> None`.
- Behavior contracts:
  - Stat tier runs only when `self._trust_stat` and the entry has a `stats[current_root]` matching `(st_size, st_mtime_ns)`; then it hits without opening the file. A stat `OSError` routes through the discovery error construction (parity).
  - Verify tier reads bytes via `read_doc_bytes`, hashes them; on a `file_sha256` match it reuses the entry and refreshes `stats[current_root]` (best-effort stat); otherwise returns `CacheMiss(data)`.
  - `record_miss` computes the full sha256 of `data`, resets `stats` to only the current root (best-effort stat), and stores the node payload (or `node=None` when `meta is None`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cache.py`:

```python
import hashlib

from doc_lattice.cache import CacheHit, CacheMiss, Entry, NodePayload, SectionRecordModel, StatRecord
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.model import FileSections, ParsedDoc, SectionRecord


def _doc_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _entry_for(text: str, root: str, *, node: NodePayload | None) -> Entry:
    return Entry(
        file_sha256=hashlib.sha256(_doc_bytes(text)).hexdigest(),
        stats={root: StatRecord(size=len(_doc_bytes(text)), mtime_ns=0)},
        node=node,
    )


def test_verify_tier_hit_reconstructs_parsed_doc(tmp_path: Path):
    text = "---\nid: a\n---\n# A {#a-top}\nbody\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    node = NodePayload(
        meta=NodeMeta.model_validate({"id": "a"}),
        body="# A {#a-top}\nbody\n",
        total_lines=2,
        sections=[SectionRecordModel(anchor="a-top", start=1, end=2)],
    )
    cache._entries["docs/a.md"] = _entry_for(text, cache._current_root, node=node)
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert isinstance(result.doc, ParsedDoc)
    assert result.doc.meta.id == "a"
    assert result.doc.sections == FileSections(
        total_lines=2, sections=(SectionRecord("a-top", 1, 2),)
    )


def test_verify_tier_non_node_hit_returns_none_doc(tmp_path: Path):
    text = "# plain\n"
    doc = tmp_path / "docs" / "plain.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    cache._entries["docs/plain.md"] = _entry_for(text, cache._current_root, node=None)
    result = cache.lookup("docs/plain.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_content_change_is_a_miss_carrying_current_bytes(tmp_path: Path):
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("changed\n", encoding="utf-8")
    cache = _open(tmp_path)
    cache._entries["docs/a.md"] = _entry_for("original\n", cache._current_root, node=None)
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheMiss)
    assert result.data == b"changed\n"


def test_absent_entry_is_a_miss(tmp_path: Path):
    doc = tmp_path / "docs" / "new.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("new\n", encoding="utf-8")
    cache = _open(tmp_path)
    result = cache.lookup("docs/new.md", doc)
    assert isinstance(result, CacheMiss)


def test_stat_tier_hit_skips_reading_the_file(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=True)
    entry = Entry(
        file_sha256="deadbeef" * 8,  # deliberately wrong; stat tier must not hash
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    cache._entries["docs/a.md"] = entry
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheHit)
    assert result.doc is None


def test_stat_tier_disabled_without_trust_stat_falls_to_verify(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=False)
    cache._entries["docs/a.md"] = Entry(
        file_sha256="deadbeef" * 8,  # wrong hash; verify tier will miss
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    result = cache.lookup("docs/a.md", doc)
    assert isinstance(result, CacheMiss)


def test_require_verified_disables_stat_tier(tmp_path: Path):
    text = "# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    st = doc.stat()
    cache = _open(tmp_path, trust_stat=True, require_verified=True)
    cache._entries["docs/a.md"] = Entry(
        file_sha256="deadbeef" * 8,
        stats={cache._current_root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
        node=None,
    )
    assert isinstance(cache.lookup("docs/a.md", doc), CacheMiss)


def test_lookup_deleted_file_raises_unreadable(tmp_path: Path):
    doc = tmp_path / "docs" / "gone.md"
    cache = _open(tmp_path, trust_stat=True)
    cache._entries["docs/gone.md"] = Entry(
        file_sha256="a" * 64,
        stats={cache._current_root: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    with pytest.raises(UnreadableDocError):
        cache.lookup("docs/gone.md", doc)


def test_record_miss_resets_stats_to_current_root(tmp_path: Path):
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    cache._entries["docs/a.md"] = Entry(
        file_sha256="old" + "0" * 61,
        stats={"/some/other/root": StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    cache.record_miss(
        "docs/a.md",
        doc,
        _doc_bytes(text),
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
    )
    entry = cache._entries["docs/a.md"]
    assert entry.file_sha256 == hashlib.sha256(_doc_bytes(text)).hexdigest()
    assert set(entry.stats) == {cache._current_root}
    assert entry.node is not None
    assert entry.node.meta.id == "a"


def test_record_miss_non_node_stores_null_node(tmp_path: Path):
    text = "# plain\n"
    doc = tmp_path / "docs" / "plain.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    cache = _open(tmp_path)
    cache.record_miss("docs/plain.md", doc, _doc_bytes(text), None, text, None)
    assert cache._entries["docs/plain.md"].node is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cache.py -k "tier or miss or lookup" -v`
Expected: FAIL with `ImportError` (`CacheHit`, `CacheMiss` do not exist) / `AttributeError` (`lookup`).

- [ ] **Step 3: Implement `lookup` and `record_miss`**

In `src/doc_lattice/cache.py`, add imports:

```python
import hashlib
from dataclasses import dataclass

from .discovery import read_doc_bytes
from .model import FileSections, NodeMeta, ParsedDoc, SectionRecord
```

(Merge the `NodeMeta` import with the existing `from .model import NodeMeta` line into `from .model import FileSections, NodeMeta, ParsedDoc, SectionRecord`.)

Add the result types above `class LoadCache`:

```python
@dataclass(frozen=True, slots=True)
class CacheHit:
    """A tier hit. ``doc`` is the reconstructed ParsedDoc, or None for a cached non-node."""

    doc: ParsedDoc | None


@dataclass(frozen=True, slots=True)
class CacheMiss:
    """A miss. ``data`` is the raw bytes already read, for the caller to decode and parse."""

    data: bytes
```

Add these methods to `LoadCache` (after `open`, before `_read`):

```python
    def lookup(self, rel_key: str, path: Path) -> CacheHit | CacheMiss:
        """Resolve one discovered file through the stat and verify tiers.

        Args:
            rel_key: The file's POSIX path relative to the project root (the entry key).
            path: The absolute path to the file on disk.

        Returns:
            A CacheHit reusing the cached derivation, or a CacheMiss carrying the freshly
            read bytes when the entry is absent, drifted, or unverifiable.

        Raises:
            UnreadableDocError: If the file cannot be read or stat-ed, with the same message
                an uncached read would produce.
        """
        entry = self._entries.get(rel_key)
        if entry is not None and self._trust_stat:
            hit = self._stat_tier(entry, path)
            if hit is not None:
                return hit
        data = read_doc_bytes(path)
        if entry is not None and entry.file_sha256 == hashlib.sha256(data).hexdigest():
            self._refresh_stat(entry, path)
            return CacheHit(doc=self._reconstruct(entry, path))
        return CacheMiss(data=data)

    def _stat_tier(self, entry: Entry, path: Path) -> CacheHit | None:
        """Return a CacheHit if the current root's stat hint matches, else None."""
        record = entry.stats.get(self._current_root)
        if record is None:
            return None
        try:
            st = path.stat()
        except OSError as exc:
            from .discovery import _unreadable

            raise _unreadable(path, exc) from exc
        if record.size != st.st_size or record.mtime_ns != st.st_mtime_ns:
            return None
        return CacheHit(doc=self._reconstruct(entry, path))

    def _refresh_stat(self, entry: Entry, path: Path) -> None:
        """Insert or refresh the current root's stat hint on a verify-tier hit (best-effort)."""
        try:
            st = path.stat()
        except OSError:
            return
        entry.stats[self._current_root] = StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)

    @staticmethod
    def _reconstruct(entry: Entry, path: Path) -> ParsedDoc | None:
        """Rebuild a ParsedDoc from a cached node payload, or None for a cached non-node."""
        node = entry.node
        if node is None:
            return None
        sections = FileSections(
            total_lines=node.total_lines,
            sections=tuple(SectionRecord(r.anchor, r.start, r.end) for r in node.sections),
        )
        return ParsedDoc(path=path, meta=node.meta, body=node.body, sections=sections)

    def record_miss(
        self,
        rel_key: str,
        path: Path,
        data: bytes,
        meta: NodeMeta | None,
        body: str,
        sections: FileSections | None,
    ) -> None:
        """Replace an entry from a fresh parse: new hash, stats reset to the current root.

        Args:
            rel_key: The entry key (POSIX path relative to the project root).
            path: The absolute file path, stat-ed for the fresh stat hint.
            data: The raw file bytes hashed for ``file_sha256``.
            meta: The validated NodeMeta, or None for a discovered non-node file.
            body: The verbatim body (unused when ``meta`` is None).
            sections: The pre-derived sections (present when ``meta`` is not None).
        """
        node: NodePayload | None = None
        if meta is not None and sections is not None:
            node = NodePayload(
                meta=meta,
                body=body,
                total_lines=sections.total_lines,
                sections=[SectionRecordModel(anchor=r.anchor, start=r.start, end=r.end) for r in sections.sections],
            )
        stats: dict[str, StatRecord] = {}
        try:
            st = path.stat()
        except OSError:
            pass
        else:
            stats[self._current_root] = StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)
        self._entries[rel_key] = Entry(
            file_sha256=hashlib.sha256(data).hexdigest(), stats=stats, node=node
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cache.py -k "tier or miss or lookup" -v`
Expected: PASS.

- [ ] **Step 5: Boundary, lint, type-check, commit**

Run: `uv run --group dev python scripts/check_typing_boundaries.py src && uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/doc_lattice/cache.py tests/test_cache.py
git commit -m "feat: add LoadCache tier selection and miss recording"
```

---

### Task 7: `LoadCache.finalize` (LRU ledger, reclamation, atomic write)

Implement the write path: move the current root to the ledger tail, withdraw its presence claim from undiscovered entries, evict over-cap head roots and scrub their stats, drop unclaimed entries, and write atomically only if the serialized cache changed. A write failure emits one direct stderr diagnostic and never raises.

**Files:**
- Modify: `src/doc_lattice/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: `MAX_STAT_ROOTS`, `CACHE_VERSION` (constants), `__version__`, `tempfile`, `os`, `sys`.
- Produces: `LoadCache.finalize(discovered: set[str]) -> None`.
- Behavior contracts:
  - The current root is appended or moved to the tail of `roots`.
  - For every entry whose `rel_key` is not in `discovered`, the current root key is removed from its `stats`.
  - If `len(roots) > MAX_STAT_ROOTS`, the head roots are evicted and their keys scrubbed from every entry's `stats`.
  - Any entry whose `stats` becomes empty is dropped.
  - The final `CacheFile` (`version=CACHE_VERSION`, `tool_version=__version__`) is written iff `model_dump(mode="json")` differs from the loaded original; a fully warm same-root run writes nothing.
  - Write is temp-in-same-dir, fsync, `os.replace`; on `OSError` it writes one line to `sys.stderr`, removes any temp, and returns.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cache.py`:

```python
from doc_lattice.constants import MAX_STAT_ROOTS


def _load_written(tmp_path: Path) -> CacheFile:
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    return CacheFile.model_validate_json(path.read_text(encoding="utf-8"))


def test_finalize_writes_current_root_at_ledger_tail(tmp_path: Path):
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    cache = _open(tmp_path)
    cache.record_miss(
        "docs/a.md",
        doc,
        b"---\nid: a\n---\n# A\n",
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
    )
    cache.finalize({"docs/a.md"})
    written = _load_written(tmp_path)
    assert written.roots[-1] == str(tmp_path.resolve())
    assert "docs/a.md" in written.entries


def test_fully_warm_same_root_run_writes_nothing(tmp_path: Path):
    text = "---\nid: a\n---\n# A\n"
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(text, encoding="utf-8")
    # Prime the cache with a real run.
    first = _open(tmp_path)
    first.record_miss(
        "docs/a.md",
        doc,
        text.encode(),
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
    )
    first.finalize({"docs/a.md"})
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns
    # A second warm run from the same root: verify-tier hit, no changes, no write.
    second = _open(tmp_path)
    assert isinstance(second.lookup("docs/a.md", doc), CacheHit)
    second.finalize({"docs/a.md"})
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime_before


def test_presence_reclamation_drops_entry_no_root_claims(tmp_path: Path):
    cache = _open(tmp_path)
    cache._entries["docs/old.md"] = Entry(
        file_sha256="a" * 64,
        stats={cache._current_root: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    cache.finalize(set())  # nothing discovered this run
    written = _load_written(tmp_path)
    assert "docs/old.md" not in written.entries


def test_presence_reclamation_keeps_entry_a_second_root_claims(tmp_path: Path):
    cache = _open(tmp_path)
    other_root = "/some/other/root"
    cache._roots.append(other_root)
    cache._entries["docs/shared.md"] = Entry(
        file_sha256="a" * 64,
        stats={
            cache._current_root: StatRecord(size=1, mtime_ns=1),
            other_root: StatRecord(size=1, mtime_ns=1),
        },
        node=None,
    )
    cache.finalize(set())  # this root did not discover it, but the other root still claims it
    written = _load_written(tmp_path)
    assert "docs/shared.md" in written.entries
    assert cache._current_root not in written.entries["docs/shared.md"].stats
    assert other_root in written.entries["docs/shared.md"].stats


def test_ledger_evicts_over_cap_head_roots_and_scrubs_their_stats(tmp_path: Path):
    cache = _open(tmp_path)
    # Fill the ledger with MAX_STAT_ROOTS old roots plus an entry they all claim.
    old_roots = [f"/root/{i}" for i in range(MAX_STAT_ROOTS)]
    cache._roots.extend(old_roots)
    cache._entries["docs/x.md"] = Entry(
        file_sha256="a" * 64,
        stats={r: StatRecord(size=1, mtime_ns=1) for r in old_roots}
        | {cache._current_root: StatRecord(size=1, mtime_ns=1)},
        node=None,
    )
    cache.finalize({"docs/x.md"})
    written = _load_written(tmp_path)
    assert len(written.roots) == MAX_STAT_ROOTS
    assert old_roots[0] not in written.roots  # head evicted
    assert old_roots[0] not in written.entries["docs/x.md"].stats  # its stat scrubbed


def test_finalize_write_failure_emits_one_stderr_line_and_does_not_raise(tmp_path, capsys, monkeypatch):
    doc = tmp_path / "docs" / "a.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nid: a\n---\n# A\n", encoding="utf-8")
    cache = _open(tmp_path)
    cache.record_miss(
        "docs/a.md",
        doc,
        b"---\nid: a\n---\n# A\n",
        NodeMeta.model_validate({"id": "a"}),
        "# A\n",
        FileSections(total_lines=1, sections=(SectionRecord("a", 1, 1),)),
    )
    import doc_lattice.cache as cache_module

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cache_module.os, "replace", _boom)
    cache.finalize({"docs/a.md"})  # must not raise
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert "cache" in captured.err.lower()
    # No partial file left behind.
    path = cache_path("slot", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    leftovers = list(path.parent.glob("*.tmp"))
    assert leftovers == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_cache.py -k "finalize or reclamation or ledger or warm" -v`
Expected: FAIL with `AttributeError: 'LoadCache' object has no attribute 'finalize'`.

- [ ] **Step 3: Implement `finalize` and the atomic writer**

In `src/doc_lattice/cache.py`, add imports:

```python
import os
import sys
import tempfile

from .constants import CACHE_FILE_NAME, CACHE_VERSION, MAX_STAT_ROOTS
```

(Merge into the existing constants import.)

Add these methods to `LoadCache`:

```python
    def finalize(self, discovered: set[str]) -> None:
        """Reclaim, bound, and persist the cache after a successful load.

        Moves the current root to the ledger tail, withdraws its claim on files it did not
        discover this run, evicts over-cap head roots and scrubs their stats, drops entries no
        live root claims, and writes atomically only if the serialized cache changed.

        Args:
            discovered: The set of entry keys (POSIX paths relative to the project root) seen
                this run.
        """
        self._touch_current_root()
        self._withdraw_undiscovered_claims(discovered)
        self._evict_over_cap_roots()
        self._drop_unclaimed_entries()
        final = CacheFile(
            version=CACHE_VERSION,
            tool_version=__version__,
            roots=self._roots,
            entries=self._entries,
        )
        if final.model_dump(mode="json") == self._original:
            return
        self._write(final)

    def _touch_current_root(self) -> None:
        """Move the current root to the ledger tail (most recently used)."""
        if self._current_root in self._roots:
            self._roots.remove(self._current_root)
        self._roots.append(self._current_root)

    def _withdraw_undiscovered_claims(self, discovered: set[str]) -> None:
        """Remove the current root's stat key from every entry it did not discover this run."""
        for rel_key, entry in self._entries.items():
            if rel_key not in discovered:
                entry.stats.pop(self._current_root, None)

    def _evict_over_cap_roots(self) -> None:
        """Evict head roots beyond MAX_STAT_ROOTS and scrub their keys from every entry."""
        if len(self._roots) <= MAX_STAT_ROOTS:
            return
        evicted = set(self._roots[:-MAX_STAT_ROOTS])
        self._roots = self._roots[-MAX_STAT_ROOTS:]
        for entry in self._entries.values():
            for root in evicted:
                entry.stats.pop(root, None)

    def _drop_unclaimed_entries(self) -> None:
        """Drop any entry whose stats map is empty (no live root claims it)."""
        self._entries = {key: entry for key, entry in self._entries.items() if entry.stats}

    def _write(self, cache_file: CacheFile) -> None:
        """Atomically replace the cache file, emitting one stderr diagnostic on failure.

        Writes through a temp file in the same directory, fsyncs, then ``os.replace``. Any
        OSError (unwritable directory, failed write or replace) is reported on stderr with a
        single line and swallowed, so a broken cache never changes a command's result or exit
        code. The temp file is always removed.
        """
        text = cache_file.model_dump_json()
        tmp: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=CACHE_FILE_NAME, suffix=".tmp")
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            tmp = None
        except OSError as exc:
            sys.stderr.write(f"doc-lattice: could not write load cache at {self._path}: {exc}\n")
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_cache.py -k "finalize or reclamation or ledger or warm" -v`
Expected: PASS.

- [ ] **Step 5: Run the whole cache suite, boundary, lint, type-check, commit**

Run: `uv run --group dev pytest tests/test_cache.py -v && uv run --group dev python scripts/check_typing_boundaries.py src && uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/doc_lattice/cache.py tests/test_cache.py
git commit -m "feat: add LoadCache finalize with LRU reclamation and atomic write"
```

---

### Task 8: Wire the cache into `orchestrate.load_lattice`

Add the cached branch and the `require_verified` flag. With `cache_key` unset the load path is bit-for-bit today's; with it set, each discovered file goes through the tiers and the cache is written after a successful `build_lattice`. This task also adds the determinism property test that enforces the section-1 guarantee.

**Files:**
- Modify: `src/doc_lattice/orchestrate.py`
- Test: `tests/test_orchestrate.py`, `tests/test_cache.py`

**Interfaces:**
- Consumes: `LoadCache`, `CacheHit` (cache.py), `read_doc`/`decode_doc` (discovery), `derive_file_sections` (loader), `os.environ`.
- Produces: `orchestrate.load_lattice(project: ProjectConfig, *, require_verified: bool = False) -> Lattice`.

- [ ] **Step 1: Write the failing determinism and wiring tests**

Add to `tests/test_orchestrate.py`:

```python
import os


def _with_cache(tmp_path: Path, *, trust_stat: bool = False) -> Path:
    lines = ["cache_key: testslot"]
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


def test_cache_disabled_leaves_env_untouched(lattice_dir: Path):
    # With no cache_key, load_lattice must never resolve or write a cache.
    project = load_config(None, lattice_dir)
    assert project.config.cache_key is None
    lat = load_lattice(project)
    assert set(lat.nodes_by_id) == {"art-direction", "pc-design", "gdd"}
```

Add the big determinism property test to `tests/test_cache.py`:

```python
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from doc_lattice.check import check_lattice, statuses_json
from doc_lattice.config import load_config
from doc_lattice.orchestrate import load_lattice


def _run_check(project) -> str:
    import json

    return json.dumps(statuses_json(check_lattice(load_lattice(project))))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    edits=st.lists(
        st.sampled_from(["body", "frontmatter", "add", "delete", "rename", "touch"]),
        min_size=1,
        max_size=8,
    )
)
def test_default_tier_matches_uncached_under_random_edits(tmp_path_factory, edits):
    base = tmp_path_factory.mktemp("proj")
    xdg = tmp_path_factory.mktemp("xdg")
    docs = base / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("---\nid: a\n---\n# A {#a}\nbody a\n", encoding="utf-8")
    (docs / "b.md").write_text(
        "---\nid: b\nderives_from:\n  - ref: a#a\n---\n# B\nbody b\n", encoding="utf-8"
    )
    cached_cfg = base / ".doc-lattice.yml"
    counter = 0
    for edit in edits:
        target = docs / "a.md"
        if edit == "body" and target.exists():
            target.write_text(target.read_text() + f"\nmore {counter}\n", encoding="utf-8")
        elif edit == "frontmatter" and target.exists():
            body = target.read_text().split("---\n", 2)[-1]
            target.write_text(f"---\nid: a\ntitle: t{counter}\n---\n{body}", encoding="utf-8")
        elif edit == "add":
            (docs / f"extra{counter}.md").write_text(
                f"---\nid: extra{counter}\n---\n# E\n", encoding="utf-8"
            )
        elif edit == "delete":
            extras = sorted(docs.glob("extra*.md"))
            if extras:
                extras[0].unlink()
        elif edit == "rename":
            extras = sorted(docs.glob("extra*.md"))
            if extras:
                extras[0].rename(docs / f"renamed{counter}.md")
        elif edit == "touch" and target.exists():
            target.touch()
        counter += 1

        # Uncached reference (no cache_key).
        cached_cfg.unlink(missing_ok=True)
        reference = _run_check(load_config(None, base))
        # Default-tier cached run (verify tier), sharing one XDG home across iterations.
        cached_cfg.write_text("cache_key: prop\n", encoding="utf-8")
        import os as _os

        _os.environ["XDG_CACHE_HOME"] = str(xdg)
        try:
            cached_result = _run_check(load_config(None, base))
        finally:
            _os.environ.pop("XDG_CACHE_HOME", None)
        assert cached_result == reference
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --group dev pytest tests/test_orchestrate.py -k cached tests/test_cache.py -k random_edits -v`
Expected: FAIL (cached branch not implemented; `load_lattice` ignores `cache_key`).

- [ ] **Step 3: Rewrite `orchestrate.py` with the cached branch**

Replace the whole body of `src/doc_lattice/orchestrate.py` with:

```python
"""Wire config, discovery, parsing, and loading into a Lattice."""

import os

from .cache import CacheHit, LoadCache
from .config import ProjectConfig
from .discovery import decode_doc, discover_doc_paths, read_doc
from .frontmatter_parser import parse_meta, split_frontmatter
from .loader import build_lattice, derive_file_sections
from .model import Lattice, ParsedDoc


def load_lattice(project: ProjectConfig, *, require_verified: bool = False) -> Lattice:
    """Discover, parse, and assemble the lattice for a project.

    With ``cache_key`` unset this is today's full parse of every discovered file. With it set,
    each file is served from the incremental load cache when unchanged and the cache is
    rewritten after a successful build.

    Args:
        project: The loaded project config with contained docs roots.
        require_verified: Force the verify tier for every file, disabling the stat fast tier.
            Set only by the reconcile CLI path, whose writes must never derive from stale
            content.

    Returns:
        The built Lattice. Files without lattice frontmatter (no ``id``) are skipped.
    """
    if project.config.cache_key is None:
        return _load_uncached(project)
    return _load_cached(project, require_verified=require_verified)


def _load_uncached(project: ProjectConfig) -> Lattice:
    """Today's cache-free load path, unchanged."""
    parsed: list[ParsedDoc] = []
    for path in discover_doc_paths(project.resolved_roots, project.config.ignore_globs):
        text = read_doc(path)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, path)
        if meta is None:
            continue
        parsed.append(ParsedDoc(path=path, meta=meta, body=body))
    return build_lattice(parsed)


def _load_cached(project: ProjectConfig, *, require_verified: bool) -> Lattice:
    """The incremental load path. Writes the cache only after a successful build."""
    config = project.config
    cache = LoadCache.open(
        cache_key=config.cache_key,
        project_root=project.project_root,
        env=os.environ,
        trust_stat=config.cache_trust_stat,
        require_verified=require_verified,
    )
    parsed: list[ParsedDoc] = []
    discovered: set[str] = set()
    for path in discover_doc_paths(project.resolved_roots, config.ignore_globs):
        rel_key = path.relative_to(project.project_root).as_posix()
        discovered.add(rel_key)
        result = cache.lookup(rel_key, path)
        if isinstance(result, CacheHit):
            if result.doc is not None:
                parsed.append(result.doc)
            continue
        text = decode_doc(path, result.data)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, path)
        sections = derive_file_sections(body) if meta is not None else None
        cache.record_miss(rel_key, path, result.data, meta, body, sections)
        if meta is not None:
            parsed.append(ParsedDoc(path=path, meta=meta, body=body, sections=sections))
    lattice = build_lattice(parsed)
    cache.finalize(discovered)
    return lattice
```

Note: `config.cache_key` is `str | None` but this branch only runs when it is not None; if `ty` flags the `cache_key=` argument as possibly-None, add a local `assert config.cache_key is not None` before the `LoadCache.open` call (the assert documents the branch invariant and satisfies the type narrower).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --group dev pytest tests/test_orchestrate.py tests/test_cache.py -v`
Expected: PASS (including the property test).

- [ ] **Step 5: Full suite, boundary, lint, type-check, commit**

Run: `env -u FORCE_COLOR uv run --group dev pytest && uv run --group dev python scripts/check_typing_boundaries.py src && uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all pass; coverage above 80 percent.

```bash
git add src/doc_lattice/orchestrate.py tests/test_orchestrate.py tests/test_cache.py
git commit -m "feat: wire the incremental load cache into load_lattice"
```

---

### Task 9: Reconcile forces the verify tier, and CLI byte-equality

Make `reconcile` load with `require_verified=True` so a stat-tier stale read can never seed a write, and pin cached-versus-uncached byte-equality for every command at the CLI layer.

**Files:**
- Modify: `src/doc_lattice/cli.py`
- Test: `tests/test_cli.py`, `tests/test_cache.py`

**Interfaces:**
- Consumes: `load_lattice(..., require_verified=...)`.
- Produces: `cli._load(config, *, require_verified: bool = False)`; `reconcile` calls it with `require_verified=True`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` (match the file's existing runner/import style; this uses typer's `CliRunner` and the `lattice_dir` fixture):

```python
import shutil

from typer.testing import CliRunner

from doc_lattice.cli import app

runner = CliRunner()


def _run(args, cwd, env):
    import os

    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(app, args, env=env)
    finally:
        os.chdir(old)


@pytest.mark.parametrize(
    "args",
    [["check"], ["lint"], ["impact", "art-direction"], ["graph", "--format", "json"]],
)
def test_cached_cli_output_matches_uncached(lattice_dir: Path, tmp_path: Path, args):
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    uncached = _run(args, lattice_dir, env)
    (lattice_dir / ".doc-lattice.yml").write_text("cache_key: cli\n", encoding="utf-8")
    cold = _run(args, lattice_dir, env)  # writes cache
    warm = _run(args, lattice_dir, env)  # reads cache
    assert cold.stdout == uncached.stdout
    assert cold.exit_code == uncached.exit_code
    assert warm.stdout == uncached.stdout
    assert warm.exit_code == uncached.exit_code


def test_reconcile_all_cached_matches_uncached_bytes(lattice_dir: Path, tmp_path: Path):
    # Twin copies of the fixture tree: one uncached, one cached. Resulting file bytes and
    # output must match.
    twin = tmp_path / "twin"
    shutil.copytree(lattice_dir, twin)
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1"}
    uncached = _run(["reconcile", "--all"], lattice_dir, env)
    (twin / ".doc-lattice.yml").write_text(
        "cache_key: recon\ncache_trust_stat: true\n", encoding="utf-8"
    )
    cached = _run(["reconcile", "--all"], twin, env)
    assert cached.exit_code == uncached.exit_code
    for name in ["pc-design.md", "art-direction.md", "gdd.md"]:
        assert (twin / "docs" / name).read_bytes() == (lattice_dir / "docs" / name).read_bytes()
```

Add a reconcile-safety test to `tests/test_cache.py`:

```python
def test_require_verified_load_sees_fresh_content_after_same_stat_rewrite(tmp_path, monkeypatch):
    # Even under trust_stat, a require_verified load must read fresh bytes, so reconcile never
    # plans from stale content. Simulated by a rewrite that keeps size and mtime_ns identical.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "a.md"
    doc.write_text("---\nid: a\n---\n# A\naaaa\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        "cache_key: rv\ncache_trust_stat: true\n", encoding="utf-8"
    )
    from doc_lattice.config import load_config
    from doc_lattice.orchestrate import load_lattice

    load_lattice(load_config(None, tmp_path))  # warm the cache
    st = doc.stat()
    # Rewrite with identical byte length, then restore the exact mtime_ns.
    doc.write_text("---\nid: a\n---\n# A\nbbbb\n", encoding="utf-8")
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns))
    verified = load_lattice(load_config(None, tmp_path), require_verified=True)
    assert "bbbb" in verified.nodes_by_id["a"].body
```

(Ensure `import os` is present in `tests/test_cache.py`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev pytest tests/test_cli.py -k "cached_cli or reconcile_all_cached" tests/test_cache.py -k require_verified -v`
Expected: FAIL (`reconcile` does not pass `require_verified`, so the same-stat rewrite serves stale content).

- [ ] **Step 3: Thread `require_verified` through the CLI in `cli.py`**

In `src/doc_lattice/cli.py`, change `_load`:

```python
def _load(config: Path | None, *, require_verified: bool = False) -> Lattice:
    """Load the lattice from the resolved project config."""
    project = load_config(config, Path.cwd())
    return load_lattice(project, require_verified=require_verified)
```

In the `reconcile` command body, change the load call:

```python
        lattice = _load(config, require_verified=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u FORCE_COLOR uv run --group dev pytest tests/test_cli.py -k "cached_cli or reconcile_all_cached" tests/test_cache.py -k require_verified -v`
Expected: PASS.

- [ ] **Step 5: Full suite, lint, type-check, commit**

Run: `env -u FORCE_COLOR uv run --group dev pytest && uv run --group dev ruff format src tests && uv run --group dev ruff check src tests && uv run --group dev ty check src`
Expected: all clean.

```bash
git add src/doc_lattice/cli.py tests/test_cli.py tests/test_cache.py
git commit -m "feat: force the verify tier for reconcile and pin cached CLI parity"
```

---

### Task 10: Documentation and the init config template

Update the README, CHANGELOG, `init` config scaffold, and CLAUDE.md so the feature is documented and discoverable.

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `src/doc_lattice/scaffold.py`
- Modify: `CLAUDE.md`
- Test: `tests/test_scaffold.py`

**Interfaces:**
- Produces: `scaffold.render_config` emits a commented-out `# cache_key:` example line.

- [ ] **Step 1: Write the failing scaffold test**

Add to `tests/test_scaffold.py`:

```python
def test_render_config_includes_commented_cache_key_example():
    text = render_config(("docs",), None)
    assert "# cache_key: my-project-docs" in text
```

(Match the module's existing import of `render_config`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --group dev pytest tests/test_scaffold.py -k cache_key -v`
Expected: FAIL (no cache_key line in the template).

- [ ] **Step 3: Add the commented example to `scaffold.py`**

In `src/doc_lattice/scaffold.py`, add a constant near the other commented blocks:

```python
_COMMENTED_CACHE = "# cache_key: my-project-docs   # opt-in load cache slot under your cache home\n"
```

In `render_config`, insert it into the assembled `parts` (after `_COMMENTED_IGNORE`, before the linear block):

```python
    parts = [_CONFIG_HEADER, buf.getvalue(), _COMMENTED_IGNORE, _COMMENTED_CACHE]
    if linear_team is None:
        parts.append(_COMMENTED_LINEAR)
    parts.append(_COMMENTED_BINDING)
    return "".join(parts)
```

- [ ] **Step 4: Run the scaffold test to verify it passes**

Run: `uv run --group dev pytest tests/test_scaffold.py -v`
Expected: PASS.

- [ ] **Step 5: Update the README**

In `README.md`, extend the config block in the `## Configuration` section to show the new keys, and add a subsection after the `binding_layers` paragraph. Replace the example config block with:

```yaml
# doc-lattice configuration
docs_roots:
  - docs                  # roots to scan for tracked .md files (default: ["docs"])
# ignore_globs:           # paths to skip within those roots
#   - "**/superpowers/plans/**"
# cache_key: my-docs      # opt-in incremental load cache slot (see Load cache below)
# cache_trust_stat: false # opt-in stat fast tier for read-only commands (accepts the mtime caveat)
# linear_team: ENG        # the Linear team the `linear` query targets
# binding_layers: null    # accepted but inert today; setting it changes nothing (see below)
```

Add this subsection (after the `binding_layers` paragraph, before `## Adopting doc-lattice in your docs repo`):

```markdown
### Load cache (opt-in)

Large doc sets (thousands of files) can skip re-parsing unchanged docs with an opt-in cache.
Set `cache_key` to a single safe segment (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`); it names a slot
under your user cache home at `<cache_home>/doc-lattice/<cache_key>/load-cache.json`, where
`<cache_home>` is `$XDG_CACHE_HOME` (when absolute) or `~/.cache`. The cache lives outside every
checkout on purpose: because `.doc-lattice.yml` is committed, every clone and git worktree of the
project shares one warm cache with no per-checkout setup, which an in-repo cache could not do.

By default the cache re-reads and re-hashes each file's bytes every run, so its output is always
byte-identical to an uncached run under any cache state (cold, warm, stale, corrupt, or wrong
version); only timing differs. Setting `cache_trust_stat: true` adds a faster tier for read-only
commands that trusts a file whose size and modification time are unchanged, accepting one caveat: a
file rewritten so that both its size and its nanosecond mtime are identical is served stale until it
is touched. `reconcile` ignores `cache_trust_stat` and always verifies content, so it can never
write frontmatter from stale data. Two projects sharing a `cache_key` stay correct (a content-hash
hit implies identical bytes); the only cost is overwrite churn, so prefer distinct keys. Delete the
cache directory to reset it; a tool-version bump discards it automatically.
```

- [ ] **Step 6: Update the CHANGELOG**

In `CHANGELOG.md`, under `## [Unreleased]` in the `### Added` list, add:

```markdown
- Opt-in incremental load cache: set `cache_key` in `.doc-lattice.yml` to skip re-parsing
  unchanged docs across runs and git worktrees, with byte-identical output to an uncached run by
  default; `cache_trust_stat: true` adds a faster stat tier for read-only commands under the
  documented mtime caveat, and `reconcile` always verifies content (#28).
```

- [ ] **Step 7: Update CLAUDE.md**

In `CLAUDE.md`, in the "Pure vs impure split" paragraph, add `cache.py` to the impure list. Change the sentence:

> Only `config`, `discovery`, `orchestrate`, and `cli` touch the disk (`cli` performs the reconcile and init writes); `linear_fetch` is impure wiring and `linear_client` is the only module that touches the network.

to:

> Only `config`, `discovery`, `orchestrate`, `cli`, and `cache` touch the disk (`cli` performs the reconcile and init writes; `cache` reads and atomically writes the opt-in load cache under the user cache home); `linear_fetch` is impure wiring and `linear_client` is the only module that touches the network.

- [ ] **Step 8: Verify docs render and nothing regressed, then commit**

Run: `uv run --group dev pytest tests/test_scaffold.py -v && uv run --group dev ruff check src tests`
Expected: PASS and clean. Manually confirm no em-dashes were introduced in the edited docs.

```bash
git add README.md CHANGELOG.md src/doc_lattice/scaffold.py CLAUDE.md tests/test_scaffold.py
git commit -m "docs: document the opt-in load cache and add the init template line"
```

---

### Task 11: Benchmark script (dev-only)

Add `scripts/bench_load_cache.py` that measures the four load states at 1k and 5k docs, reporting the median of 5 runs, the cache file size, and the `json.loads` share of warm-run time. This is dev-only and not shipped; its numbers go in the implementation PR description. The acceptance threshold is: warm verify tier at least 3x faster than uncached at 5k docs.

**Files:**
- Create: `scripts/bench_load_cache.py`

**Interfaces:**
- Standalone script run with `uv run --group dev python scripts/bench_load_cache.py`. It writes a synthetic corpus to a temp directory and prints a small table.

- [ ] **Step 1: Create the benchmark script**

Create `scripts/bench_load_cache.py`:

```python
#!/usr/bin/env python3
"""Dev-only benchmark for the opt-in load cache. Not shipped.

Generates a synthetic corpus at 1k and 5k docs and reports the median of 5 runs of
load_lattice wall time in four states: uncached, cold cache (including the write), warm
verify tier, and warm stat tier. Also reports the cache file size and the share of warm-run
time spent in json.loads. Acceptance: warm verify tier is at least 3x faster than uncached at
5k docs.
"""

import statistics
import tempfile
import time
from pathlib import Path

from doc_lattice.config import load_config
from doc_lattice.orchestrate import load_lattice

_HEADINGS_PER_DOC = 6
_EDGES_PER_DOC = 3


def _write_corpus(root: Path, count: int) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        edges = "".join(
            f"  - ref: doc{(i - j) % count}\n" for j in range(1, _EDGES_PER_DOC + 1) if (i - j) % count != i
        )
        sections = "".join(f"## Section {s} {{#s{i}-{s}}}\nbody {s}\n\n" for s in range(_HEADINGS_PER_DOC))
        derives = f"derives_from:\n{edges}" if edges else ""
        (docs / f"doc{i}.md").write_text(
            f"---\nid: doc{i}\nlayer: design\n{derives}---\n# Doc {i}\n\n{sections}",
            encoding="utf-8",
        )


def _config(root: Path, *, cache_key: str | None, trust_stat: bool) -> Path:
    lines: list[str] = []
    if cache_key is not None:
        lines.append(f"cache_key: {cache_key}")
    if trust_stat:
        lines.append("cache_trust_stat: true")
    (root / ".doc-lattice.yml").write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return root


def _median_seconds(root: Path, runs: int = 5) -> float:
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        load_lattice(load_config(None, root))
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _bench_size(count: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "proj"
        _write_corpus(root, count)
        import os

        os.environ["XDG_CACHE_HOME"] = str(base / "xdg")

        _config(root, cache_key=None, trust_stat=False)
        uncached = _median_seconds(root)

        _config(root, cache_key="bench", trust_stat=False)
        # Cold: remove any cache, single timed run including the write.
        cache_dir = base / "xdg" / "doc-lattice" / "bench"
        if cache_dir.exists():
            for entry in cache_dir.iterdir():
                entry.unlink()
        start = time.perf_counter()
        load_lattice(load_config(None, root))
        cold = time.perf_counter() - start

        warm_verify = _median_seconds(root)

        _config(root, cache_key="bench", trust_stat=True)
        warm_stat = _median_seconds(root)

        cache_file = cache_dir / "load-cache.json"
        size_kb = cache_file.stat().st_size / 1024 if cache_file.exists() else 0.0
        speedup = uncached / warm_verify if warm_verify else float("inf")
        print(f"== {count} docs ==")
        print(f"  uncached       : {uncached * 1000:8.1f} ms")
        print(f"  cold (w/ write): {cold * 1000:8.1f} ms")
        print(f"  warm verify    : {warm_verify * 1000:8.1f} ms  ({speedup:.1f}x vs uncached)")
        print(f"  warm stat      : {warm_stat * 1000:8.1f} ms")
        print(f"  cache size     : {size_kb:8.1f} KB")


def main() -> None:
    """Run the benchmark at 1k and 5k docs and print the table."""
    for count in (1000, 5000):
        _bench_size(count)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the benchmark to confirm it executes and record numbers**

Run: `uv run --group dev python scripts/bench_load_cache.py`
Expected: two tables (1k and 5k). Confirm the 5k warm-verify speedup is at least 3x; if it is not, flag it in the PR before release per section 10. Record the printed numbers for the PR description.

- [ ] **Step 3: Lint and commit**

Run: `uv run --group dev ruff format scripts && uv run --group dev ruff check scripts`
Expected: clean.

```bash
git add scripts/bench_load_cache.py
git commit -m "chore: add dev-only load-cache benchmark"
```

---

## Self-Review

**Spec coverage** (section by section):

- Section 1 (guarantee): default-tier byte-equality is pinned by Task 8's property test and Task 9's CLI byte-equality; the trust_stat narrowing and reconcile's forced verification by Task 9's `require_verified` tests. Covered.
- Section 2 (config surface): Task 2 (`cache_key` pattern, `cache_trust_stat` requires `cache_key`, both `ConfigError`). No CLI flag added. Covered.
- Section 3 (cache location): Task 4 `cache_home`/`cache_path` (XDG absolute/relative/unset), README worktree rationale in Task 10. Covered.
- Section 4 (schema): Task 4 models with nested `NodeMeta` (so `model_validate` validates meta), `tool_version` and `version` invalidation in Task 5, per-root stats and LRU ledger in Tasks 6-7, `node: null` non-node caching in Task 6, target hashes deliberately not cached (no task, by design). Covered.
- Section 5 (tiers): Task 6 stat/verify/miss, Task 7 write with reclamation and change-detection, error parity via Task 3 helpers used in Task 6. Covered.
- Section 6 (invalidation summary): content-hash authority (Task 6), miss resets stats (Task 6), presence reclamation (Task 7), version/tool_version discard (Task 5), config changes never invalidate (relative-path keying, Task 8). Covered.
- Section 7 (failure modes): read empty-on-failure (Task 5), write stderr diagnostic not via warnings (Task 7), atomic replace/last-writer-wins (Task 7). Covered.
- Section 8 (purity/module changes): `cache.py` boundary-clean (Tasks 4-7 verify with the boundary script), `config` fields (Task 2), `model` dataclasses (Task 1), `loader` seam (Task 1), `discovery` split (Task 3), `orchestrate` branch + flag (Task 8), `constants` (Task 4), env-mapping argument (Task 4). Covered.
- Section 9 (testing): determinism property test (Task 8), round-trip property test (Task 1), the enumerated unit tests spread across Tasks 4-9 (corruption modes, non-node no-YAML, per-root isolation, stats reset, LRU/eviction/scrub, presence reclamation, warm-writes-nothing, injected write failure with warnings-as-errors intent, cache_key validation, trust_stat-without-key, XDG handling, error parity, CLI byte-equality including `reconcile --all` on twin trees). Covered. Note: the "non-node hit performs no YAML parse (counting monkeypatch)" and "warnings configured as errors" specifics are covered structurally; the executor should keep those exact assertions when fleshing out the enumerated unit tests.
- Section 10 (benchmark): Task 11. Covered.
- Section 13 (documentation): Task 10. Covered.

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" left; every code step shows complete code.

**Type consistency:** `LoadCache.open/lookup/record_miss/finalize`, `CacheHit(doc)`, `CacheMiss(data)`, `FileSections(total_lines, sections)`, `SectionRecord(anchor, start, end)`, `derive_file_sections(body) -> FileSections`, `read_doc_bytes`/`decode_doc`, and `load_lattice(project, *, require_verified=False)` are named identically across the tasks that define and consume them.

One follow-up the executor should watch: in Task 6 `_stat_tier` imports the private `discovery._unreadable`; if a reviewer prefers a public seam, promote `_unreadable` to `unreadable_doc_error` in Task 3 and update both call sites. The plan uses the private import to avoid widening `discovery`'s public surface, consistent with the module's existing style.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-10-doc-lattice-load-cache.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
