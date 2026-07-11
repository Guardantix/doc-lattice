# Report Render Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move check, lint, and impact JSON construction beside their result types and human rendering into a dedicated report module without changing CLI output.

**Architecture:** `check.py`, `lint.py`, and `impact.py` own pure dict builders for their domain results. A new `report_render.py` owns Rich console output, while `cli.py` retains loading, output-format dispatch, JSON serialization, GitHub annotations, and exit behavior.

**Tech Stack:** Python 3.13, Typer, Rich, pytest, Ruff, ty

---

### Task 1: Pure JSON builders

**Files:**
- Modify: `tests/test_check.py`
- Modify: `tests/test_lint.py`
- Modify: `tests/test_impact.py`
- Modify: `src/doc_lattice/check.py`
- Modify: `src/doc_lattice/lint.py`
- Modify: `src/doc_lattice/impact.py`

- [x] **Step 1: Write failing exact-payload tests**

Construct representative `EdgeStatus`, `LintResult`, and `(Node, depth)` values and assert the complete dictionaries returned by `statuses_json`, `lint_json`, and `impact_json`. Include a broken check target, lint violations and skips, and impact title, path, tickets, and depth fields so every serialized field is locked down.

- [x] **Step 2: Verify RED**

Run: `uv run --group dev pytest tests/test_check.py tests/test_lint.py tests/test_impact.py -q`

Expected: collection failures because the three builder functions do not exist.

- [x] **Step 3: Implement the builders**

Add public, Google-documented pure functions that return the exact dictionaries currently assembled by `cli.py`, preserving field and list order. `impact_json` accepts `list[tuple[Node, int]]` because current impact results include minimum depth.

- [x] **Step 4: Verify GREEN**

Run: `uv run --group dev pytest tests/test_check.py tests/test_lint.py tests/test_impact.py -q`

Expected: all selected tests pass.

### Task 2: Human report renderers

**Files:**
- Create: `tests/test_report_render.py`
- Create: `src/doc_lattice/report_render.py`
- Modify: `tests/test_cli.py`

- [x] **Step 1: Write failing renderer tests**

Use `Console(record=True, width=200)` and representative domain values to assert exact plain text from `render_statuses`, `render_lint`, and `render_impact`. Assert Rich markup characters in ids, refs, paths, and tickets render literally. Move the state-color exhaustiveness import assertion from `cli.py` to `report_render.py`.

- [x] **Step 2: Verify RED**

Run: `uv run --group dev pytest tests/test_report_render.py tests/test_cli.py -q`

Expected: collection failure because `report_render.py` does not exist.

- [x] **Step 3: Implement the renderer module**

Create `report_render.py` with the required module docstring, `_STATE_COL_WIDTH`, `_STATE_COLORS`, private `_skip_summary`, and three public Google-documented renderers. Move the existing human-output statements byte-for-byte and continue escaping user-controlled Rich markup.

- [x] **Step 4: Verify GREEN**

Run: `uv run --group dev pytest tests/test_report_render.py tests/test_cli.py -q`

Expected: all selected tests pass.

### Task 3: CLI wiring and documentation

**Files:**
- Modify: `src/doc_lattice/cli.py`
- Modify: `CHANGELOG.md`

- [x] **Step 1: Rewire output dispatch**

Import the three JSON builders and three human renderers. Replace inline check, lint, and impact dict comprehensions with builder calls and replace their human Rich loops with renderer calls. Remove `EdgeState`, `LintResult`, `_STATE_COL_WIDTH`, `_STATE_COLORS`, and `_skip_summary` from `cli.py`; retain GitHub annotation markup and generic CLI error markup.

- [x] **Step 2: Document the internal refactor**

Add an Unreleased Changed entry stating that check, lint, and impact JSON and human report rendering now live in their owning pure modules and dedicated renderer module.

- [x] **Step 3: Verify acceptance criteria and full quality gates**

Run:

```bash
uv run --group dev pytest
uv run --group dev ruff check src tests
uv run --group dev ruff format --check src tests
uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
```

Expected: every command exits 0, coverage remains at least 80%, existing CLI assertions are unchanged, and `cli.py` contains none of the moved report dicts or Rich markup.

### Task 4: Review and publish

**Files:**
- Review all changed files

- [x] **Step 1: Run `/review` and resolve findings at root cause**

Inspect correctness, acceptance coverage, architecture, typing, output compatibility, and docs. Apply each valid finding, then rerun the full quality gates.

- [ ] **Step 2: Commit and publish**

Stage only issue #29 files, commit with an issue-focused message, push `refactor/cli`, and open a pull request targeting `main` with `Fixes #29` plus verification output.
