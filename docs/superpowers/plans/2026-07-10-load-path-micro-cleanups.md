# Load-Path Micro-Cleanups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove repeated line counting, safe YAML construction, and newline normalization from the
document load path without changing any observable behavior.

**Architecture:** Keep each optimization at its existing ownership boundary. Reuse a local safe
YAML instance in each parsing module, reuse one per-document line count inside the loader, delegate
section newline handling to the hashing helper, and preserve per-call round-trip YAML construction.

**Tech Stack:** Python 3.13+, pytest, ruamel.yaml, Ruff, ty, uv

---

### Task 1: Count Each Document's Lines Once

**Files:**
- Modify: `tests/test_loader.py:3-25`
- Modify: `src/game_lattice/loader.py:28-43`

- [ ] **Step 1: Write the failing call-count test**

Add the module import after the third-party imports in `tests/test_loader.py`:

```python
import game_lattice.loader as loader_module
```

Add this test after `test_registers_file_and_anchor_ids`:

```python
def test_build_lattice_counts_lines_once_per_document(monkeypatch):
    docs = [
        _doc("a.md", "# A {#a}\nbody\n", id="a"),
        _doc("b.md", "# B {#b}\nbody\n", id="b"),
    ]
    calls: list[str] = []
    original_line_count = loader_module._line_count

    def counting_line_count(body: str) -> int:
        calls.append(body)
        return original_line_count(body)

    monkeypatch.setattr(loader_module, "_line_count", counting_line_count)

    loader_module.build_lattice(docs)

    assert calls == [doc.body for doc in docs]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --group dev pytest tests/test_loader.py::test_build_lattice_counts_lines_once_per_document -v --no-cov
```

Expected: FAIL because the observed calls contain each document body twice.

- [ ] **Step 3: Hoist the per-document line count**

Change the opening of the document loop in `src/game_lattice/loader.py` to:

```python
    for doc in docs:
        file_id = doc.meta.id
        total_lines = _line_count(doc.body)
        _register(
            TargetId(file_id),
            Location(path=doc.path, kind="file", span=(1, total_lines)),
            index,
            sources,
            f"file {doc.path}",
        )
        toc = build_toc(doc.body)
        anchored: list[tuple[int, Heading, TargetId]] = []
```

Keep the existing `section_span(toc, i, total_lines)` call unchanged.

- [ ] **Step 4: Run the loader tests and verify GREEN**

Run:

```bash
uv run --group dev pytest tests/test_loader.py -q --no-cov
```

Expected: all loader tests PASS.

- [ ] **Step 5: Commit the loader cycle**

```bash
git add tests/test_loader.py src/game_lattice/loader.py
git commit -m "perf: count document lines once"
```

### Task 2: Reuse the Frontmatter Safe YAML Loader

**Files:**
- Modify: `tests/test_frontmatter_parser.py:3-12`
- Modify: `src/game_lattice/frontmatter_parser.py:13-60`

- [ ] **Step 1: Write the failing singleton-use test**

Add this module import after the third-party imports in `tests/test_frontmatter_parser.py`:

```python
import game_lattice.frontmatter_parser as frontmatter_parser_module
```

Add this test after `test_parse_meta_returns_node`:

```python
def test_parse_meta_reuses_safe_yaml_loader(monkeypatch):
    raw_documents = ["id: first\n", "id: second\n"]
    original_yaml = frontmatter_parser_module._YAML
    calls: list[str] = []

    class TrackingYAML:
        def load(self, raw_meta: str):
            calls.append(raw_meta)
            return original_yaml.load(raw_meta)

    monkeypatch.setattr(frontmatter_parser_module, "_YAML", TrackingYAML())

    metas = [parse_meta(raw, Path(f"{index}.md")) for index, raw in enumerate(raw_documents)]

    assert [meta.id for meta in metas if meta is not None] == ["first", "second"]
    assert calls == raw_documents
```

Replacing only the module singleton keeps the spy local to frontmatter parsing, and pytest restores
the original singleton after the test.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --group dev pytest \
  tests/test_frontmatter_parser.py::test_parse_meta_reuses_safe_yaml_loader -v --no-cov
```

Expected: FAIL with `AttributeError` because `frontmatter_parser._YAML` does not exist.

- [ ] **Step 3: Add and use the safe YAML singleton**

Add the singleton beside the existing module constants in `src/game_lattice/frontmatter_parser.py`:

```python
_FENCE = "---"
_BOM = chr(0xFEFF)  # UTF-8 byte-order mark; strip a leading one so the opening fence is detected
_YAML = YAML(typ="safe")
```

Replace the per-call construction and load in `parse_meta` with:

```python
    try:
        data: Any = _YAML.load(raw_meta)
```

- [ ] **Step 4: Run the frontmatter parser tests and verify GREEN**

Run:

```bash
uv run --group dev pytest tests/test_frontmatter_parser.py -q --no-cov
```

Expected: all frontmatter parser tests PASS.

- [ ] **Step 5: Commit the frontmatter cycle**

```bash
git add tests/test_frontmatter_parser.py src/game_lattice/frontmatter_parser.py
git commit -m "perf: reuse frontmatter YAML loader"
```

- [ ] **Step 6: Write the failing malformed-frontmatter version regression**

Add this test after the malformed-YAML tests in `tests/test_frontmatter_parser.py`:

```python
def test_safe_yaml_loader_resets_version_after_malformed_frontmatter():
    with pytest.raises(UnreadableDocError):
        parse_meta("%YAML 1.1\nid: [unclosed\n", Path("broken.md"))

    meta = parse_meta("id: on\n", Path("next.md"))

    assert meta is not None
    assert meta.id == "on"
```

- [ ] **Step 7: Run the malformed-frontmatter test and verify RED**

Run:

```bash
uv run --group dev pytest \
  tests/test_frontmatter_parser.py::test_safe_yaml_loader_resets_version_after_malformed_frontmatter \
  -v --no-cov
```

Expected: FAIL because the malformed document leaves YAML 1.1 active and the next document parses
`on` as `True`, which strict `NodeMeta` validation rejects.

- [ ] **Step 8: Reset the reusable parser's version before each frontmatter load**

Immediately before `_YAML.load(raw_meta)` in `frontmatter_parser.parse_meta`, add:

```python
    # A YAML directive can update the reusable parser's version even when parsing fails. Reset it
    # so each document starts with default YAML semantics, matching a fresh safe loader.
    _YAML.version = None
```

- [ ] **Step 9: Run the malformed-frontmatter test and verify GREEN**

Run:

```bash
uv run --group dev pytest \
  tests/test_frontmatter_parser.py::test_safe_yaml_loader_resets_version_after_malformed_frontmatter \
  -v --no-cov
```

Expected: PASS; the next document uses default YAML semantics and preserves `on` as a string.

- [ ] **Step 10: Commit the frontmatter version-isolation fix**

```bash
git add src/game_lattice/frontmatter_parser.py tests/test_frontmatter_parser.py \
  docs/superpowers/specs/2026-07-10-load-path-micro-cleanups-design.md \
  docs/superpowers/plans/2026-07-10-load-path-micro-cleanups.md
git commit -m "fix: reset frontmatter YAML version per load"
```

### Task 3: Reuse the Config Safe YAML Loader

**Files:**
- Modify: `tests/test_config.py:3-25`
- Modify: `src/game_lattice/config.py:13-90`

- [ ] **Step 1: Write the failing singleton-use test**

Add this module import after the third-party imports in `tests/test_config.py`:

```python
import game_lattice.config as config_module
```

Add this test after `test_loads_and_resolves_roots`:

```python
def test_load_config_reuses_safe_yaml_loader(monkeypatch, tmp_path: Path):
    original_yaml = config_module._YAML
    calls: list[str] = []

    class TrackingYAML:
        def load(self, text: str):
            calls.append(text)
            return original_yaml.load(text)

    monkeypatch.setattr(config_module, "_YAML", TrackingYAML())
    projects = [tmp_path / "first", tmp_path / "second"]
    for project in projects:
        project.mkdir()
        (project / ".game-lattice.yml").write_text("docs_roots: [docs]\n", encoding="utf-8")
        load_config(None, project)

    assert calls == ["docs_roots: [docs]\n", "docs_roots: [docs]\n"]
```

Replacing only the module singleton keeps the spy local to config parsing, and pytest restores the
original singleton after the test.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --group dev pytest tests/test_config.py::test_load_config_reuses_safe_yaml_loader -v --no-cov
```

Expected: FAIL with `AttributeError` because `config._YAML` does not exist.

- [ ] **Step 3: Add and use the safe YAML singleton**

Add the singleton below `DEFAULT_CONFIG_NAME` in `src/game_lattice/config.py`:

```python
DEFAULT_CONFIG_NAME = ".game-lattice.yml"
_YAML = YAML(typ="safe")
```

Replace the per-call construction and load in `_read_yaml` with:

```python
def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read config {path}: {exc}"
        raise ConfigError(msg) from exc
    try:
        data = _YAML.load(text)
```

- [ ] **Step 4: Run the config tests and verify GREEN**

Run:

```bash
uv run --group dev pytest tests/test_config.py -q --no-cov
```

Expected: all config tests PASS.

- [ ] **Step 5: Commit the config cycle**

```bash
git add tests/test_config.py src/game_lattice/config.py
git commit -m "perf: reuse config YAML loader"
```

- [ ] **Step 6: Add the malformed-config recovery regression**

Add this test after the malformed-YAML test in `tests/test_config.py`:

```python
def test_safe_yaml_loader_recovers_after_malformed_config(tmp_path: Path):
    config_path = tmp_path / ".game-lattice.yml"
    config_path.write_text("docs_roots: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(None, tmp_path)

    config_path.write_text("docs_roots: [docs]\n", encoding="utf-8")

    project = load_config(None, tmp_path)

    assert project.config.docs_roots == ["docs"]
```

- [ ] **Step 7: Run the recovery regression**

Run:

```bash
uv run --group dev pytest tests/test_config.py::test_safe_yaml_loader_recovers_after_malformed_config -q --no-cov
```

Expected: PASS, proving the same module singleton remains reusable after a parse error.

- [ ] **Step 8: Commit the review-hardening test**

```bash
git add tests/test_config.py
git commit -m "test: cover config YAML recovery"
```

- [ ] **Step 9: Write the failing YAML-version isolation regression**

Add this test after `test_safe_yaml_loader_recovers_after_malformed_config`:

```python
def test_safe_yaml_loader_resets_version_between_config_files(tmp_path: Path):
    first_config = tmp_path / "first.yml"
    first_config.write_text("%YAML 1.1\n---\ndocs_roots: [docs]\n", encoding="utf-8")
    second_config = tmp_path / "second.yml"
    second_config.write_text("docs_roots: [on]\n", encoding="utf-8")

    first_project = load_config(first_config, tmp_path)
    second_project = load_config(second_config, tmp_path)

    assert first_project.config.docs_roots == ["docs"]
    assert second_project.config.docs_roots == ["on"]
```

- [ ] **Step 10: Run the version-isolation test and verify RED**

Run:

```bash
uv run --group dev pytest \
  tests/test_config.py::test_safe_yaml_loader_resets_version_between_config_files -v --no-cov
```

Expected: FAIL because the first file leaves YAML 1.1 active and the second file parses `on` as
`True`, which strict config validation rejects.

- [ ] **Step 11: Reset the reusable parser's version before each load**

Immediately before `_YAML.load(text)` in `config._read_yaml`, add:

```python
    # A YAML directive updates the reusable parser's version. Reset it so each config starts
    # with default YAML semantics, matching a fresh safe loader.
    _YAML.version = None
```

- [ ] **Step 12: Run the version-isolation test and verify GREEN**

Run:

```bash
uv run --group dev pytest \
  tests/test_config.py::test_safe_yaml_loader_resets_version_between_config_files -v --no-cov
```

Expected: PASS; the second config uses default YAML semantics and preserves `on` as a string.

- [ ] **Step 13: Commit the YAML-version isolation fix**

```bash
git add src/game_lattice/config.py tests/test_config.py \
  docs/superpowers/specs/2026-07-10-load-path-micro-cleanups-design.md \
  docs/superpowers/plans/2026-07-10-load-path-micro-cleanups.md
git commit -m "fix: reset config YAML version per load"
```

### Task 4: Centralize Section Newline Normalization

**Files:**
- Modify: `tests/test_sections.py:3-12`
- Modify: `src/game_lattice/sections.py:8-11,45-63`

- [ ] **Step 1: Write the failing delegation test**

Add this module import after `import pytest` in `tests/test_sections.py`:

```python
import game_lattice.sections as sections_module
```

Add this test after `test_split_body_lines_normalizes_crlf_and_lone_cr`:

```python
def test_split_body_lines_uses_shared_newline_normalizer(monkeypatch):
    calls: list[str] = []

    def normalize_newlines(body: str) -> str:
        calls.append(body)
        return "normalized\nlines"

    monkeypatch.setattr(sections_module, "normalize_newlines", normalize_newlines)

    assert split_body_lines("raw body") == ["normalized", "lines"]
    assert calls == ["raw body"]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --group dev pytest \
  tests/test_sections.py::test_split_body_lines_uses_shared_newline_normalizer -v --no-cov
```

Expected: FAIL with `AttributeError` because `sections.normalize_newlines` does not exist.

- [ ] **Step 3: Delegate to the hashing helper**

Add the package import after the standard-library imports in `src/game_lattice/sections.py`:

```python
from .hashing import normalize_newlines
```

Replace the inline replacements inside `split_body_lines` with:

```python
    lines = normalize_newlines(body).split("\n")
```

- [ ] **Step 4: Run the sections tests and verify GREEN**

Run:

```bash
uv run --group dev pytest tests/test_sections.py -q --no-cov
```

Expected: all section tests PASS, including the existing mixed CRLF and lone-CR test.

- [ ] **Step 5: Commit the newline cycle**

```bash
git add tests/test_sections.py src/game_lattice/sections.py
git commit -m "refactor: centralize section newline normalization"
```

### Task 5: Document Round-Trip YAML Lifetime and the Cleanup

**Files:**
- Modify: `src/game_lattice/reconcile.py:102-107`
- Modify: `CHANGELOG.md:7-25`

- [ ] **Step 1: Document the per-call round-trip loader invariant**

Insert this comment immediately above the existing `YAML(typ="rt")` construction in
`reconcile.apply_reconcile`:

```python
    # Round-trip loaders retain document-specific state, so construct one for each call.
    yaml = YAML(typ="rt")
```

Do not move this loader to module scope and do not change its type.

- [ ] **Step 2: Add the Unreleased changelog entry**

Add this bullet under `## [Unreleased]` -> `### Changed` in `CHANGELOG.md`:

```markdown
- Reduced repeated load-path work by counting document lines once, reusing safe YAML loaders, and
  sharing newline normalization between section parsing and hashing (#27).
```

- [ ] **Step 3: Verify the source-level acceptance criteria**

Run:

```bash
rg -n '_YAML = YAML\(typ="safe"\)|YAML\(typ="rt"\)|normalize_newlines|total_lines' \
  src/game_lattice/{config,frontmatter_parser,reconcile,sections,loader}.py
```

Expected:

- exactly two safe `_YAML` definitions, one in `config.py` and one in `frontmatter_parser.py`;
- `YAML(typ="rt")` remains inside `apply_reconcile` with the lifetime comment;
- `sections.py` imports and calls `normalize_newlines`;
- `loader.py` assigns `total_lines` once inside each document iteration and reuses it.

- [ ] **Step 4: Run formatting checks for the edited documentation and source**

Run:

```bash
uv run --group dev ruff format --check src tests
git diff --check
```

Expected: both commands exit 0 with no formatting errors.

- [ ] **Step 5: Commit the invariant comment and changelog**

```bash
git add src/game_lattice/reconcile.py CHANGELOG.md
git commit -m "docs: record load-path cleanup"
```

### Task 6: Run the Complete Required Verification

**Files:**
- Verify: `src/game_lattice/`
- Verify: `tests/`
- Verify: `scripts/check_typing_boundaries.py`

- [ ] **Step 1: Run the full test suite with coverage**

Run:

```bash
uv run --group dev pytest
```

Expected: all tests PASS and coverage is at least 80 percent.

- [ ] **Step 2: Run Ruff lint**

Run:

```bash
uv run --group dev ruff check src tests
```

Expected: `All checks passed!`

- [ ] **Step 3: Run Ruff format verification**

Run:

```bash
uv run --group dev ruff format --check src tests
```

Expected: all files already formatted.

- [ ] **Step 4: Run the type checker**

Run:

```bash
uv run --group dev ty check src
```

Expected: all checks pass with no diagnostics.

- [ ] **Step 5: Run the typing-boundary guard**

Run:

```bash
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: exit 0 and no boundary violations.

- [ ] **Step 6: Confirm a clean diff and focused state**

Run:

```bash
git diff --check
git status --short --branch
git diff --stat origin/main...HEAD
```

Expected: no unstaged changes, no whitespace errors, and only the issue #27 implementation,
tests, spec, plan, comment, and changelog are present.

### Task 7: Review and Resolve Every Valid Finding

**Files:**
- Review: all files in `git diff origin/main...HEAD`
- Modify: only files implicated by valid findings

- [ ] **Step 1: Invoke the requested review workflow**

Run `/review` against the complete `origin/main...HEAD` diff. Check correctness, acceptance-criteria
coverage, behavior preservation, mutable state, test quality, stale documentation, and repository
conventions.

Expected: a prioritized list of findings with exact file and line references, or an explicit
no-findings result.

- [ ] **Step 2: Validate each finding before editing**

For every finding, inspect the cited code and reproduce the concern with the narrowest relevant
test or static check. Reject findings that do not apply and record why. For each valid behavioral
finding, add or adjust a failing regression test before changing production code.

- [ ] **Step 3: Fix valid findings at their root cause**

Make the smallest root-cause correction consistent with the approved design. Do not suppress a
diagnostic, weaken an assertion, or broaden scope to conceal a symptom.

- [ ] **Step 4: Rerun all five required verification commands**

Run:

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: every command exits 0.

- [ ] **Step 5: Commit any review fixes**

If review produced valid findings, stage only their root-cause fixes and commit:

```bash
git add CHANGELOG.md src/game_lattice/config.py src/game_lattice/frontmatter_parser.py \
  src/game_lattice/loader.py src/game_lattice/reconcile.py src/game_lattice/sections.py \
  tests/test_config.py tests/test_frontmatter_parser.py tests/test_loader.py tests/test_sections.py
git commit -m "fix: address load-path cleanup review"
```

If review produced no valid findings, do not create an empty commit.

### Task 8: Publish the Pull Request and Capture Evidence

**Files:**
- Verify: Git history and GitHub pull request metadata

- [ ] **Step 1: Inspect the final branch history and diff**

Run:

```bash
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

Expected: a clean branch containing only issue #27 work and its approved design and plan.

- [ ] **Step 2: Push the branch**

Run:

```bash
git push -u origin loadpath-microcleanups
```

Expected: the remote branch is created or updated successfully.

- [ ] **Step 3: Open the pull request**

Create a pull request targeting `main` with a concise summary of the three cleanups, test strategy,
documentation updates, all five verification results, and `Closes #27`.

Expected: GitHub returns the new pull request URL.

- [ ] **Step 4: Verify the published PR state**

Fetch the pull request metadata and confirm its base is `main`, its head is
`loadpath-microcleanups`, its body closes issue #27, and the published commit matches local HEAD.

- [ ] **Step 5: Report requirement-by-requirement evidence**

Paste the branch and worktree evidence, RED and GREEN test evidence, final five-command verification
output, review result and fixes, documentation evidence, commit history, and pull request URL.
