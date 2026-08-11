# Reconcile List Indentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make reconcile update only targeted `seen` scalars without changing the input
indentation of any frontmatter list.

**Architecture:** Keep the existing round-trip YAML load for fresh-input validation, compose a
second YAML node tree to obtain exact source spans, and patch only existing or newly inserted
`seen` values. Reverse-ordered source edits preserve every unrelated byte and leave the pure
writer/durable transaction boundary unchanged.

**Tech Stack:** Python 3.13+, ruamel.yaml 0.19.1, pytest, Ruff, ty, uv

## Global Constraints

- Keep `src/doc_lattice/reconcile.py` pure and do not change transaction, journal, recovery,
  hashing, `seen` scalar format, or scaffold behavior.
- Preserve comments, key order, body text, column-zero lists, two-space lists, and mixed list
  indentation.
- Continue returning the original text and an empty applied set for exact no-ops.
- Continue wrapping malformed fresh frontmatter in `UnreadableDocError`.
- Production code remains compatible with Python 3.13 and follows the repository's 100-character
  Ruff line length.

---

### Task 1: Localized `seen` source edits

**Files:**

- Modify: `tests/test_reconcile.py:33-51`
- Modify: `tests/test_reconcile.py:120-173`
- Modify: `tests/test_reconcile.py:313-332`
- Modify: `src/doc_lattice/reconcile.py:8-12`
- Modify: `src/doc_lattice/reconcile.py:112-177`

**Interfaces:**

- Consumes: `apply_reconcile(current_file_text: str, updates: dict[str, str], source: Path)` and
  ruamel `MappingNode`, `Node`, `ScalarNode`, and `SequenceNode` source marks.
- Produces: unchanged public return type `tuple[str, set[str]]`; private immutable `_SourceEdit`
  records containing `start`, `end`, and `replacement`.

- [ ] **Step 1: Correct the existing plan rewrite expectation and add exact indentation cases**

Replace the normalization expectation in `test_plan_rewrites_applies_updates_from_reader` so its
expected bytes retain the two-space list indentation:

```python
expected_after = (
    "---\n"
    "id: d\n"
    "derives_from:\n"
    "  - ref: a#x\n"
    "    seen: newhash\n"
    "---\n"
    "café ☕\n"
).encode()
```

Add a parameterized exact-output test for both accepted block styles:

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: old\n---\nbody\n",
            "---\nid: d\nderives_from:\n  - ref: a#x\n    seen: new\n---\nbody\n",
        ),
        (
            "---\nid: d\nderives_from:\n- ref: a#x\n  seen: old\n---\nbody\n",
            "---\nid: d\nderives_from:\n- ref: a#x\n  seen: new\n---\nbody\n",
        ),
    ],
)
def test_apply_reconcile_preserves_derives_from_indentation(text: str, expected: str):

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
```

- [ ] **Step 2: Add exact missing-`seen` cases before production changes**

Add parameterized cases proving insertion follows the entry's existing indentation:

```python
@pytest.mark.parametrize(
    ("source_entry", "expected_entry"),
    [
        ("  - ref: a#x\n", "  - ref: a#x\n    seen: new\n"),
        ("- ref: a#x\n", "- ref: a#x\n  seen: new\n"),
        ("  - {ref: a#x}\n", "  - {ref: a#x, seen: new}\n"),
    ],
)
def test_apply_reconcile_adds_seen_without_reformatting_entry(
    source_entry: str, expected_entry: str
):
    text = f"---\nid: d\nderives_from:\n{source_entry}---\nbody\n"
    expected = f"---\nid: d\nderives_from:\n{expected_entry}---\nbody\n"

    out, applied = apply_reconcile(text, {"a#x": "new"}, Path("downstream.md"))

    assert out == expected
    assert applied == {"a#x"}
```

- [ ] **Step 3: Strengthen the fidelity test with mixed list indentation**

Change `test_apply_reconcile_preserves_comments_key_order_and_untargeted_edges` so it includes a
column-zero block-form `tickets` list beside the two-space `derives_from` list and asserts the
entire output:

```python
text = (
    "---\n"
    "id: d  # the node id\n"
    "derives_from:\n"
    "  - ref: a#x\n"
    "    seen: oldx\n"
    "  - ref: b#y\n"
    "    seen: oldy\n"
    "tickets:\n"
    "- T-1\n"
    "---\n"
    "# Body\n"
    "keep\n"
)
expected = (
    "---\n"
    "id: d  # the node id\n"
    "derives_from:\n"
    "  - ref: a#x\n"
    "    seen: newx\n"
    "  - ref: b#y\n"
    "    seen: oldy\n"
    "tickets:\n"
    "- T-1\n"
    "---\n"
    "# Body\n"
    "keep\n"
)

out, applied = apply_reconcile(text, {"a#x": "newx"}, Path("downstream.md"))

assert out == expected
assert applied == {"a#x"}
```

- [ ] **Step 4: Run the new tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev pytest \
  tests/test_reconcile.py::test_plan_rewrites_applies_updates_from_reader \
  tests/test_reconcile.py::test_apply_reconcile_preserves_derives_from_indentation \
  tests/test_reconcile.py::test_apply_reconcile_adds_seen_without_reformatting_entry \
  tests/test_reconcile.py::test_apply_reconcile_preserves_comments_key_order_and_untargeted_edges \
  -q
```

Expected: the two-space, missing-`seen`, and mixed-indentation assertions fail because the current
document-wide dump normalizes block sequences.

- [ ] **Step 5: Add source-edit helpers**

Remove `io`, import the ruamel node classes, and add these private structures immediately after
`Rewrite`:

```python
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


@dataclass(frozen=True, slots=True)
class _SourceEdit:
    start: int
    end: int
    replacement: str


def _mapping_value_node(mapping: MappingNode, name: str) -> Node | None:
    for key_node, value_node in mapping.value:
        if isinstance(key_node, ScalarNode) and key_node.value == name:
            return value_node
    return None


def _seen_source_edit(raw_meta: str, entry: MappingNode, new_seen: str) -> _SourceEdit:
    seen_node = _mapping_value_node(entry, "seen")
    if seen_node is not None:
        return _SourceEdit(seen_node.start_mark.index, seen_node.end_mark.index, new_seen)
    if entry.flow_style:
        insert_at = entry.end_mark.index - 1
        separator = " " if raw_meta[:insert_at].rstrip().endswith(",") else ", "
        return _SourceEdit(insert_at, insert_at, f"{separator}seen: {new_seen}")
    first_key, _ = entry.value[0]
    insert_at = entry.end_mark.index
    replacement = f"{' ' * first_key.start_mark.column}seen: {new_seen}\n"
    return _SourceEdit(insert_at, insert_at, replacement)


def _apply_source_edits(raw_meta: str, edits: list[_SourceEdit]) -> str:
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        raw_meta = raw_meta[: edit.start] + edit.replacement + raw_meta[edit.end :]
    return raw_meta
```

- [ ] **Step 6: Replace whole-document dumping in `apply_reconcile()`**

Compose a source-mark tree inside the existing `YAMLError` boundary, validate that its root and
`derives_from` nodes match the already validated loaded structure, and collect edits while walking
loaded entries and node entries in parallel:

```python
node_yaml = YAML(typ="base")
try:
    data = yaml.load(raw_meta)
    root_node = node_yaml.compose(raw_meta)
except YAMLError as exc:
    msg = f"cannot parse frontmatter to reconcile: {exc}"
    raise UnreadableDocError(msg) from exc

# Keep the existing None/mapping/derives_from/list validation above this point.
if not isinstance(root_node, MappingNode):
    raise UnreadableDocError("frontmatter is not a mapping; cannot reconcile")
entry_nodes = _mapping_value_node(root_node, "derives_from")
if not isinstance(entry_nodes, SequenceNode) or len(entry_nodes.value) != len(entries):
    raise UnreadableDocError("frontmatter derives_from is not a list; cannot reconcile")

edits: list[_SourceEdit] = []
applied: set[str] = set()
for entry, entry_node in zip(entries, entry_nodes.value, strict=True):
    if not isinstance(entry, MutableMapping) or not isinstance(entry_node, MappingNode):
        raise UnreadableDocError("frontmatter derives_from entry is not a mapping")
    ref = entry.get("ref")
    if not isinstance(ref, str):
        raise UnreadableDocError("frontmatter derives_from entry ref is not a string")
    if ref in updates:
        new_seen = updates[ref]
        if entry.get("seen") != new_seen:
            edits.append(_seen_source_edit(raw_meta, entry_node, new_seen))
            applied.add(ref)

if not applied:
    return current_file_text, applied
new_meta = _apply_source_edits(raw_meta, edits)
return f"---\n{new_meta}---\n{body}", applied
```

Delete the `entry["seen"] = new_seen`, `io.StringIO`, and `yaml.dump()` path.

- [ ] **Step 7: Run the focused reconcile suite and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev pytest tests/test_reconcile.py -q
```

Expected: all reconcile tests pass and coverage remains above 80 percent.

- [ ] **Step 8: Commit the localized writer and regressions**

```bash
git add src/doc_lattice/reconcile.py tests/test_reconcile.py
git commit -m "fix: preserve reconcile list indentation"
```

---

### Task 2: Reconcile contract and changelog

**Files:**

- Modify: `RECONCILE.md:44-46`
- Modify: `CHANGELOG.md:7-8`

**Interfaces:**

- Consumes: the verified exact-output behavior from Task 1.
- Produces: the authoritative round-trip fidelity guarantee and an `Unreleased` fix entry.

- [ ] **Step 1: Extend the reconcile fidelity sentence**

Change the write-mechanics sentence to:

```markdown
`reconcile` re-reads each downstream file fresh at write time, rewrites only the targeted `seen`
scalar through round-trip YAML (preserving your body, key order, comments, and list indentation),
and retains the exact source and replacement bytes.
```

- [ ] **Step 2: Add the changelog entry**

Add this section directly beneath `## [Unreleased]`:

```markdown
### Fixed

- `reconcile` now preserves the input indentation of frontmatter lists when it updates `seen`, so
  two-space, column-zero, and mixed list styles no longer produce unrelated cosmetic diffs.
```

- [ ] **Step 3: Run documentation checks**

Run:

```bash
test -f RECONCILE.md && test -f CHANGELOG.md
git diff --check
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev python scripts/check_version_sync.py
```

Expected: both relative documentation targets exist, the diff has no whitespace errors, and
version synchronization passes.

- [ ] **Step 4: Commit the contract update**

```bash
git add RECONCILE.md CHANGELOG.md
git commit -m "docs: guarantee reconcile list indentation"
```

---

### Task 3: Durability regression and complete verification

**Files:**

- Verify: `tests/test_reconcile_commit.py`
- Verify: `tests/test_reconcile_transaction.py`
- Verify: entire repository

**Interfaces:**

- Consumes: the pure writer output from Task 1 and documentation from Task 2.
- Produces: fresh evidence for every GTX-65 acceptance and handoff gate.

- [ ] **Step 1: Run focused durability suites**

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev pytest \
  tests/test_reconcile_commit.py tests/test_reconcile_transaction.py
```

- [ ] **Step 2: Run the full test suite with coverage enforcement**

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev pytest
```

- [ ] **Step 3: Run Ruff checks**

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev ruff check src tests
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev ruff format --check src tests
```

- [ ] **Step 4: Run type and boundary checks**

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev ty check src
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev python \
  scripts/check_typing_boundaries.py src
```

- [ ] **Step 5: Run version and repository hygiene checks**

```bash
UV_CACHE_DIR=/tmp/doc-lattice-uv-cache uv run --group dev python scripts/check_version_sync.py
git diff --check origin/main...HEAD
git status --short
```

- [ ] **Step 6: Audit the diff against GTX-65**

Inspect `git diff --stat origin/main...HEAD` and `git diff origin/main...HEAD`. Confirm that the
diff contains only the dated design/plan artifacts, localized reconcile implementation, exact
regression tests, `RECONCILE.md`, and `CHANGELOG.md`; no transaction, recovery, hash, scalar-format,
or scaffold file changes are permitted.
