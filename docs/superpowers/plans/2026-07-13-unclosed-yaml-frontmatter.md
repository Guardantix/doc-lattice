# Unclosed YAML Frontmatter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every lattice-loading path fail with tool error 2 when a Markdown file opens YAML frontmatter but never closes it.

**Architecture:** Keep the three-way distinction at `frontmatter_parser.split_frontmatter`: no opening fence is untracked Markdown, matched fences yield metadata and body, and an unmatched opening fence raises a source-naming `UnreadableDocError`. Pass source paths through loaders and reconcile validation, and invalidate version-1 cached non-node derivations by incrementing the cache version.

**Tech Stack:** Python 3.13+, Pydantic project errors, Typer CLI, pytest, uv, Ruff, ty

---

### Task 1: Fail Closed at the Parsing Boundary

**Files:**
- Modify: `tests/test_frontmatter_parser.py:14-74`
- Modify: `src/doc_lattice/frontmatter_parser.py:18-41`
- Modify: `tests/test_discovery.py:176-183`
- Modify: `src/doc_lattice/orchestrate.py:41,74`
- Modify: `src/doc_lattice/reconcile.py:83-115,175`
- Modify: `tests/test_reconcile.py`

- [ ] **Step 1: Replace the fail-open parser test with a source-naming failure test**

Update every `split_frontmatter` call in `tests/test_frontmatter_parser.py` to pass
`Path("a.md")`, then replace the old unclosed-fence assertion with:

```python
def test_split_frontmatter_unclosed_fence_raises_source_naming_error():
    text = "---\nid: x\nno closing fence\n"

    with pytest.raises(UnreadableDocError) as exc:
        split_frontmatter(text, Path("broken.md"))

    assert exc.value.code == "UNREADABLE_DOC"
    assert str(exc.value) == (
        "unclosed YAML frontmatter in broken.md: add a closing '---' fence"
    )
```

Pass `doc` as the second argument to the `split_frontmatter` call in
`test_decode_doc_lone_cr_frontmatter_is_still_parsed`.

- [ ] **Step 2: Run the parser regression and verify it fails for the old return value**

Run:

```bash
uv run --group dev pytest --no-cov \
  tests/test_frontmatter_parser.py::test_split_frontmatter_unclosed_fence_raises_source_naming_error -v
```

Expected: FAIL because `split_frontmatter` accepts only one argument and does not raise.

- [ ] **Step 3: Implement the source-aware three-way split**

Change the splitter to require the source path and fail after exhausting closing-fence candidates:

```python
def split_frontmatter(text: str, source: Path) -> tuple[str | None, str]:
    """Split a document into its YAML frontmatter block and body.

    Args:
        text: The full file text.
        source: The file the text came from, for error messages.

    Returns:
        ``(raw_meta, body)`` where ``raw_meta`` is the YAML between the opening and
        closing ``---`` fences (or None if the file does not open with a fence), and
        ``body`` is everything after the closing fence (the whole text if no fence).

    Raises:
        UnreadableDocError: If an opening frontmatter fence has no closing fence.
    """
    stripped = text.lstrip(_BOM)
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for closing_fence_index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FENCE:
            raw_meta = "\n".join(lines[1:closing_fence_index])
            body = "\n".join(lines[closing_fence_index + 1 :])
            return raw_meta + "\n" if raw_meta else "", body
    raise UnreadableDocError(
        f"unclosed YAML frontmatter in {source}: add a closing '---' fence"
    )
```

Pass `path` or `doc_path` from both loops in `orchestrate.py`:

```python
raw_meta, body = split_frontmatter(text, path)
raw_meta, body = split_frontmatter(text, doc_path)
```

Add `source: Path` to `apply_reconcile`, pass it to the splitter, and pass each planned path from
`plan_rewrites`:

```python
def apply_reconcile(
    current_file_text: str, updates: dict[str, str], source: Path
) -> tuple[str, set[str]]:
    raw_meta, body = split_frontmatter(current_file_text, source)

# inside plan_rewrites
new_text, applied = apply_reconcile(fresh, updates, path)
```

Update direct test calls to pass the file path under test, using `Path("downstream.md")` where no
fixture path exists. Update `_apply_plan` to pass its loop variable `path`.

- [ ] **Step 4: Add reconcile fresh-read coverage for an unclosed fence**

Add:

```python
def test_plan_rewrites_names_unclosed_frontmatter_source():
    path = Path("downstream.md")
    text = "---\nid: d\nderives_from:\n  - ref: a#x\n"

    with pytest.raises(UnreadableDocError) as exc_info:
        plan_rewrites({path: {"a#x": "newhash"}}, lambda _path: text)

    assert str(exc_info.value) == (
        "unclosed YAML frontmatter in downstream.md: add a closing '---' fence"
    )
```

- [ ] **Step 5: Run the focused parser, discovery, and reconcile tests**

Run:

```bash
uv run --group dev pytest --no-cov \
  tests/test_frontmatter_parser.py tests/test_discovery.py tests/test_reconcile.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the parser boundary change**

```bash
git add src/doc_lattice/frontmatter_parser.py src/doc_lattice/orchestrate.py \
  src/doc_lattice/reconcile.py tests/test_frontmatter_parser.py tests/test_discovery.py \
  tests/test_reconcile.py
git commit -m "fix: reject unclosed YAML frontmatter"
```

### Task 2: Prove Cached and Uncached Load Parity

**Files:**
- Modify: `tests/test_orchestrate.py:29-69`
- Modify: `tests/test_cache.py`
- Modify: `src/doc_lattice/constants.py:49-52`
- Modify: `src/doc_lattice/cache/schema.py:40-48`

- [ ] **Step 1: Add uncached and cache-enabled loader parity coverage**

Add to `tests/test_orchestrate.py`:

```python
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

    (tmp_path / ".doc-lattice.yml").write_text("cache_key: unclosed\n", encoding="utf-8")
    with pytest.raises(UnreadableDocError) as cached:
        load_lattice(load_config(None, tmp_path))

    expected = f"unclosed YAML frontmatter in {broken}: add a closing '---' fence"
    assert str(uncached.value) == expected
    assert str(cached.value) == expected
```

This test proves the `id`-bearing malformed file raises instead of yielding an empty lattice.

- [ ] **Step 2: Add a legacy cache regression that requires invalidation**

Import `hashlib`, `doc_lattice.__version__`, `Entry`, `StatRecord`, and `CACHE_VERSION`. Create a
version-1 cache entry recording the malformed file as `node=None`, then require a fresh parse:

```python
def test_version_1_non_node_cache_cannot_hide_unclosed_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    docs = tmp_path / "docs"
    docs.mkdir()
    doc = docs / "broken.md"
    doc.write_text("---\nid: vanished\n# Missing close\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text("cache_key: legacy\n", encoding="utf-8")
    root = str(tmp_path.resolve())
    st = doc.stat()
    old_cache = CacheFile(
        version=1,
        tool_version=__version__,
        roots=[root],
        entries={
            "docs/broken.md": Entry(
                file_sha256=hashlib.sha256(doc.read_bytes()).hexdigest(),
                stats={root: StatRecord(size=st.st_size, mtime_ns=st.st_mtime_ns)},
                node=None,
            )
        },
    )
    path = cache_path("legacy", os.environ)
    path.parent.mkdir(parents=True)
    path.write_text(old_cache.model_dump_json(), encoding="utf-8")

    assert CACHE_VERSION > old_cache.version
    with pytest.raises(UnreadableDocError, match="unclosed YAML frontmatter"):
        load_lattice(load_config(None, tmp_path))
```

- [ ] **Step 3: Run both tests and verify the legacy-cache test fails before invalidation**

Run:

```bash
uv run --group dev pytest --no-cov \
  tests/test_orchestrate.py::test_cached_and_uncached_loads_reject_unclosed_frontmatter_identically \
  tests/test_cache.py::test_version_1_non_node_cache_cannot_hide_unclosed_frontmatter -v
```

Expected: the loader parity test passes after Task 1; the legacy-cache test fails because
`CACHE_VERSION` is still 1.

- [ ] **Step 4: Invalidate old derivations**

Change the cache constant and its comments:

```python
# CACHE_VERSION bumps on an intentional schema or cached-derivation semantics change;
CACHE_VERSION: int = 2
```

Make the cache model docstring version-independent:

```python
class CacheFile(BaseModel):
    """The whole versioned cache document."""
```

- [ ] **Step 5: Run the orchestration and cache suites**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_orchestrate.py tests/test_cache.py \
  tests/test_cache_schema.py tests/test_cache_state.py tests/test_cache_store.py \
  tests/test_cache_lookup.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit cache parity and invalidation**

```bash
git add src/doc_lattice/constants.py src/doc_lattice/cache/schema.py \
  tests/test_orchestrate.py tests/test_cache.py
git commit -m "test: enforce frontmatter cache parity"
```

### Task 3: Pin the CLI Contract and Document It

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `README.md:76-85,404-430`

- [ ] **Step 1: Add an all-command exit-code regression**

Add a parameterized test for every command that loads the lattice:

```python
@pytest.mark.parametrize(
    "args",
    [
        ["check"],
        ["lint"],
        ["impact", "vanished"],
        ["reconcile", "vanished"],
        ["graph"],
        ["linear"],
    ],
)
@pytest.mark.parametrize("cache_enabled", [False, True], ids=["uncached", "cached"])
def test_lattice_loading_commands_exit_2_on_unclosed_frontmatter(
    tmp_path: Path, args: list[str], cache_enabled: bool
):
    docs = tmp_path / "docs"
    docs.mkdir()
    broken = docs / "broken.md"
    broken.write_text("---\nid: vanished\n# Missing close\n", encoding="utf-8")
    if cache_enabled:
        (tmp_path / ".doc-lattice.yml").write_text(
            "cache_key: cli-unclosed\n", encoding="utf-8"
        )
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg"), "NO_COLOR": "1", "COLUMNS": "240"}

    result = _run(args, tmp_path, env)

    assert result.exit_code == 2
    assert "unclosed YAML frontmatter" in result.stderr
    assert str(broken) in result.stderr
    assert "add a closing '---' fence" in result.stderr
    assert "UNREADABLE_DOC" in result.stderr
```

- [ ] **Step 2: Run the CLI regression**

Run:

```bash
uv run --group dev pytest --no-cov \
  tests/test_cli.py::test_lattice_loading_commands_exit_2_on_unclosed_frontmatter -v
```

Expected: all 12 uncached/cached command cases pass.

- [ ] **Step 3: Document the missing-close contract**

After the tool-error paragraph under “Broken refs and tool errors,” add:

```markdown
A Markdown file without an opening `---` fence is valid untracked prose. Once a file opens YAML
frontmatter with `---`, it must include a closing `---` fence; otherwise every lattice-loading
command names the file, asks for the missing close, and exits 2 instead of omitting the node.
```

In the exit-code table, make code 2 explicit:

```markdown
| `2` | Tool error: invalid or unclosed frontmatter, invalid config, unreadable or non-UTF-8 input, incoherent ids, or a containment failure. |
```

Add a troubleshooting entry:

```markdown
**`unclosed YAML frontmatter ...` exits 2.** A file beginning with `---` must add another `---`
line after its YAML metadata. The message names the malformed file; a file with no opening fence
remains ordinary untracked Markdown.
```

- [ ] **Step 4: Run README/version guards and CLI tests**

Run:

```bash
uv run --group dev pytest --no-cov tests/test_cli.py tests/test_version_check.py -q
uv run --group dev python scripts/check_version_sync.py
```

Expected: all tests pass and the version-sync script exits 0.

- [ ] **Step 5: Commit CLI and documentation coverage**

```bash
git add tests/test_cli.py README.md
git commit -m "docs: specify unclosed frontmatter errors"
```

### Task 4: Verify the Complete Fix

**Files:**
- Verify all changed production, test, and documentation files

- [ ] **Step 1: Run formatting, lint, type, and boundary checks**

```bash
uv run --group dev ruff format --check src tests
uv run --group dev ruff check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
```

Expected: every command exits 0.

- [ ] **Step 2: Run the full suite with coverage**

```bash
uv run --group dev pytest
```

Expected: zero failures and total coverage at least 80%.

- [ ] **Step 3: Audit every issue acceptance criterion against evidence**

Confirm the exact error assertion names the file and missing close; the CLI parameter matrix covers
all six lattice-loading commands; orchestration compares cached and uncached messages; the legacy
cache test proves version-1 non-node entries cannot suppress the error; the existing no-frontmatter
tests remain green; and README describes both branches.

- [ ] **Step 4: Inspect the final diff and commit history**

```bash
git diff --check origin/main...HEAD
git status -sb
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors, a clean worktree, and only #87-scoped commits.
