# Markdown Compatibility Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the handwritten Markdown heading/fence parser and manually ported slug regex with
a versioned compatibility adapter while preserving section identity, spans, cache structure, and
load performance.

**Architecture:** The unmodified fence and ATX-heading rules from `markdown-it-py==4.2.0` operate on
a minimal adapter-owned source map, while `markdown_compat.py` exposes the narrow
heading/anchor/slug interface consumed by `sections.py`. A maintenance script evaluates
`github-slugger@2.0.0` over all Unicode scalar values and deterministically generates the Python
slug compatibility data.

**Tech Stack:** Python 3.13+, markdown-it-py 4.2.0, pytest, Node/npm for maintenance verification,
uv, Ruff, ty.

---

## File map

- Create `src/doc_lattice/markdown_compat.py`: the documented parser, marker, and slug adapter.
- Create `src/doc_lattice/_github_slugger_data.py`: deterministic generated slug compatibility data.
- Modify `src/doc_lattice/sections.py`: keep span/text utilities and delegate compatibility work.
- Create `scripts/generate_github_slugger_data.py`: generate or check the pinned slug artifact.
- Create `scripts/bench_sections.py`: reproducible large-document section benchmark.
- Create `tests/fixtures/markdown_compatibility.json`: golden supported-subset behavior.
- Create `tests/test_markdown_compat.py`: adapter and golden-fixture tests.
- Create `tests/test_slugger_generator.py`: deterministic generator tests.
- Create `tests/test_bench_sections.py`: benchmark document and threshold tests.
- Modify `tests/test_orchestrate.py`: cache cold/warm structural-parity integration test.
- Modify `pyproject.toml` and `uv.lock`: exact direct parser dependency.
- Modify `README.md`, `CLAUDE.md`, and `CHANGELOG.md`: compatibility contract and maintenance docs.
- Modify the design spec to expose the shared first-line anchor-stripping helper.

### Task 1: Golden heading extraction through pinned upstream rules

**Files:**
- Create: `tests/fixtures/markdown_compatibility.json`
- Create: `tests/test_markdown_compat.py`
- Create: `src/doc_lattice/markdown_compat.py`

- [x] **Step 1: Add the golden fixture**

Create a JSON array with these named cases and exact expected `Heading` records, anchor ids, and
inclusive spans:

```json
[
  {
    "name": "fences",
    "body": "# Top\n```python\n## Hidden\n``\n### Hidden too\n``` trailing\n#### Still hidden\n```\n~~~\n##### Tilde hidden\n```\n###### Still tilde hidden\n~~~\n## After\n",
    "headings": [
      {"level": 1, "text": "Top", "anchor": null, "line": 1},
      {"level": 2, "text": "After", "anchor": null, "line": 14}
    ],
    "anchor_ids": ["top", "after"],
    "spans": [[1, 14], [14, 14]]
  },
  {
    "name": "atx_closers_and_markers",
    "body": "# C# guide ##\n## Accent {#accent} ###\n### Use `{#id}` in examples\n#### Example {#bad}##\n",
    "headings": [
      {"level": 1, "text": "C# guide", "anchor": null, "line": 1},
      {"level": 2, "text": "Accent {#accent}", "anchor": "accent", "line": 2},
      {"level": 3, "text": "Use `{#id}` in examples", "anchor": null, "line": 3},
      {"level": 4, "text": "Example {#bad}##", "anchor": null, "line": 4}
    ],
    "anchor_ids": ["c-guide", "accent", "use-id-in-examples", "example-bad"],
    "spans": [[1, 4], [2, 4], [3, 4], [4, 4]]
  },
  {
    "name": "duplicate_slugs_and_marker_reservation",
    "body": "## Notes\n## Notes\n## Notes {#n}\n## Notes n\n## Notes-1\n",
    "headings": [
      {"level": 2, "text": "Notes", "anchor": null, "line": 1},
      {"level": 2, "text": "Notes", "anchor": null, "line": 2},
      {"level": 2, "text": "Notes {#n}", "anchor": "n", "line": 3},
      {"level": 2, "text": "Notes n", "anchor": null, "line": 4},
      {"level": 2, "text": "Notes-1", "anchor": null, "line": 5}
    ],
    "anchor_ids": ["notes", "notes-1", "n", "notes-n-1", "notes-1-1"],
    "spans": [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5]]
  },
  {
    "name": "unicode_and_empty_slugs",
    "body": "## Привет 你好\n## é under‿score\n## ⚡\n## Привет 你好\n",
    "headings": [
      {"level": 2, "text": "Привет 你好", "anchor": null, "line": 1},
      {"level": 2, "text": "é under‿score", "anchor": null, "line": 2},
      {"level": 2, "text": "⚡", "anchor": null, "line": 3},
      {"level": 2, "text": "Привет 你好", "anchor": null, "line": 4}
    ],
    "anchor_ids": ["привет-你好", "é-under‿score", "", "привет-你好-1"],
    "spans": [[1, 1], [2, 2], [3, 3], [4, 4]]
  },
  {
    "name": "empty_and_rejected_headings",
    "body": "#\n##   \n#not-heading\n####### Too deep\n###### Six\n",
    "headings": [
      {"level": 1, "text": "", "anchor": null, "line": 1},
      {"level": 2, "text": "", "anchor": null, "line": 2},
      {"level": 6, "text": "Six", "anchor": null, "line": 5}
    ],
    "anchor_ids": ["", "-1", "six"],
    "spans": [[1, 5], [2, 5], [5, 5]]
  },
  {
    "name": "nested_spans",
    "body": "# Top {#top}\nintro\n## Child {#child}\nbody\n### Nested {#nested}\nbody\n## Peer {#peer}\nbody\n",
    "headings": [
      {"level": 1, "text": "Top {#top}", "anchor": "top", "line": 1},
      {"level": 2, "text": "Child {#child}", "anchor": "child", "line": 3},
      {"level": 3, "text": "Nested {#nested}", "anchor": "nested", "line": 5},
      {"level": 2, "text": "Peer {#peer}", "anchor": "peer", "line": 7}
    ],
    "anchor_ids": ["top", "child", "nested", "peer"],
    "spans": [[1, 8], [3, 6], [5, 6], [7, 8]]
  },
  {
    "name": "excluded_heading_forms",
    "body": "Setext\n======\n> # Quoted\n- ## Listed\n   ### Indented\n# Top level\n",
    "headings": [
      {"level": 1, "text": "Top level", "anchor": null, "line": 6}
    ],
    "anchor_ids": ["top-level"],
    "spans": [[6, 6]]
  }
]
```

- [x] **Step 2: Write the extraction-only golden test**

```python
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from doc_lattice.markdown_compat import extract_headings

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "markdown_compatibility.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_extract_headings_matches_golden_fixture(case: dict[str, object]) -> None:
    assert [asdict(heading) for heading in extract_headings(str(case["body"]))] == case["headings"]
```

- [x] **Step 3: Run the test and verify RED**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest tests/test_markdown_compat.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'doc_lattice.markdown_compat'`.

- [x] **Step 4: Implement focused extraction**

Create `markdown_compat.py` with the version constants, `Heading`, a module-level parser configured
with only `normalize`, `block`, `fence`, `heading`, and a one-line fallback replacing `paragraph`,
plus `extract_headings`. The implementation must normalize through `hashing.normalize_newlines`,
accept only `heading_open` tokens with `token.level == 0`, `token.markup` made of `#`, and a source
line beginning with `#`, then read the following `inline` token and convert `token.map[0] + 1`.
Use the existing anchor expression unchanged:

```python
"""Versioned Markdown heading and GitHub-slug compatibility adapter."""

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_block.state_block import StateBlock

from .hashing import normalize_newlines

MARKDOWN_COMPAT_VERSION = "markdown-it-py==4.2.0"
SLUG_COMPAT_VERSION = "github-slugger@2.0.0"

_ANCHOR_RE = re.compile(
    r"(?:^|\s+)\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}(?:\s*$|\s+(?=#+\s*$))"
)


@dataclass(frozen=True, slots=True)
class Heading:
    """One supported ATX heading with a 1-based source line."""

    level: int
    text: str
    anchor: str | None
    line: int


def _skip_line(
    state: StateBlock, start_line: int, _end_line: int, silent: bool
) -> bool:
    if not silent:
        state.line = start_line + 1
    return True


def _make_parser() -> MarkdownIt:
    parser = MarkdownIt("zero")
    parser.core.ruler.enableOnly(["normalize", "block"])
    parser.block.ruler.enableOnly(["fence", "heading", "paragraph"])
    parser.block.ruler.at("paragraph", _skip_line)
    return parser


_PARSER = _make_parser()


def extract_headings(body: str) -> list[Heading]:
    """Extract the supported top-level ATX headings from Markdown."""
    normalized = normalize_newlines(body)
    lines = normalized.split("\n")
    tokens = _PARSER.parse(normalized)
    headings: list[Heading] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.level != 0:
            continue
        if not token.markup or set(token.markup) != {"#"} or token.map is None:
            continue
        source_line = token.map[0]
        if source_line >= len(lines) or not lines[source_line].startswith("#"):
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].type != "inline":
            msg = f"{MARKDOWN_COMPAT_VERSION} returned a malformed heading token pair"
            raise RuntimeError(msg)
        text = tokens[index + 1].content
        anchor_match = _ANCHOR_RE.search(text)
        headings.append(
            Heading(
                level=len(token.markup),
                text=text,
                anchor=anchor_match.group(1) if anchor_match else None,
                line=source_line + 1,
            )
        )
    return headings
```

Malformed token pairs raise `RuntimeError` naming `MARKDOWN_COMPAT_VERSION`.

Implementation note: Task 4's benchmark rejected this generic parser state. The completed adapter
instead constructs the exact line-offset and indentation fields required by `StateBlock`, then
invokes the pinned upstream `fence` and `heading` rules directly. This keeps Markdown recognition
upstream-owned while avoiding unrelated block and inline parsing.

- [x] **Step 5: Run the extraction test and existing section tests**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest --no-cov tests/test_markdown_compat.py tests/test_sections.py -v`

Expected: the new extraction cases pass; existing section tests still use the old implementation
and pass.

- [x] **Step 6: Commit the extraction seam**

```bash
git add src/doc_lattice/markdown_compat.py tests/test_markdown_compat.py \
  tests/fixtures/markdown_compatibility.json
git commit -m "refactor: add focused markdown compatibility adapter"
```

### Task 2: Generated slug data and delegating section API

**Files:**
- Create: `tests/test_slugger_generator.py`
- Create: `scripts/generate_github_slugger_data.py`
- Create: `src/doc_lattice/_github_slugger_data.py`
- Modify: `tests/test_markdown_compat.py`
- Modify: `src/doc_lattice/markdown_compat.py`
- Modify: `src/doc_lattice/sections.py`

- [x] **Step 1: Write failing generator and slug tests**

Load the generator with `runpy.run_path` and assert its `render_pattern` converts
`[(0, 1), (0x41, 0x41), (0x10000, 0x10001)]` to
`r"[\u0000-\u0001\u0041\U00010000-\U00010001]"`. Extend the golden adapter test to assert
`anchor_ids(headings) == case["anchor_ids"]` and `section_spans(headings, total_lines) ==
case["spans"]`. Add a direct `strip_heading_anchor("## Accent {#accent} ##")` assertion.

- [x] **Step 2: Run the tests and verify RED**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest --no-cov tests/test_slugger_generator.py tests/test_markdown_compat.py -v`

Expected: failures report the missing generator, slug functions, and generated data.

- [x] **Step 3: Implement the deterministic generator**

The script must expose pure `coalesce(code_points)`, `render_pattern(ranges)`, and
`render_module(pattern, version, regex_sha256, stripped_count)` functions. Its CLI accepts
`--check`, `--package-root`, and `--output`. Without `--package-root`, use a temporary directory and
run this exact install command as an argument list:

```text
npm install --ignore-scripts --no-package-lock --no-save github-slugger@2.0.0
```

Run one Node ESM program that imports `regex.js`, tests every code point from `0` through
`0x10FFFF` except `0xD800` through `0xDFFF`, and prints the stripped code points as JSON. Python
coalesces them, validates `package.json` version, hashes `regex.js`, renders
`_github_slugger_data.py`, and either writes it or compares it byte-for-byte under `--check`.

Render the artifact from measured values with these exact names:

```python
def render_module(
    pattern: str, version: str, regex_sha256: str, stripped_count: int
) -> str:
    chunks: list[str] = []
    offset = 0
    while offset < len(pattern):
        end = min(offset + 80, len(pattern))
        if pattern[end - 1] == "\\":
            end -= 1
        chunks.append(pattern[offset:end])
        offset = end
    pattern_lines = "".join(f'    r"{chunk}"\n' for chunk in chunks)
    return (
        '"""Generated strip data for github-slugger. Do not edit by hand."""\n\n'
        f'UPSTREAM_PACKAGE = "github-slugger@{version}"\n'
        "UPSTREAM_REGEX_SHA256 = (\n"
        f'    "{regex_sha256}"  # pragma: allowlist secret\n'
        ")\n"
        "CHECKED_UNICODE_SCALARS = 1_112_064\n"
        f"STRIPPED_UNICODE_SCALARS = {stripped_count:_}\n"
        f"SLUG_STRIP_PATTERN = (\n{pattern_lines})\n"
    )
```

- [x] **Step 4: Run the generator against the pinned npm package**

Run:
`UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python scripts/generate_github_slugger_data.py`

Expected: it reports `github-slugger@2.0.0`, 1,112,064 checked Unicode scalar values, the stripped
count, and writes `src/doc_lattice/_github_slugger_data.py`.

Implementation note: review found that regex parity alone did not cover JavaScript/Python Unicode
lowercase differences. The completed generator checks 1,112,070 calls to the actual upstream
`slug()` operation under JavaScript Unicode 17.0, compares lowercase mappings against the minimum
Python 3.13 Unicode 15.1 table, and generates the 55 required lowercase patches plus pinned
`Cased` and `Case_Ignorable` tables for contextual casing. The CLI's `--python-baseline` defaults
to `python3.13`.

- [x] **Step 5: Implement slug and marker operations in the adapter**

Compile `SLUG_STRIP_PATTERN` from the generated module. Implement `github_slug`, a private
document-order slugger with upstream collision reservation, `anchor_ids`, and
`strip_heading_anchor`. Keep every operation pure and document the pinned behavior.

```python
_SLUG_STRIP_RE = re.compile(SLUG_STRIP_PATTERN)


def github_slug(text: str) -> str:
    """Return a github-slugger 2.0.0 base slug without deduplication."""
    return _SLUG_STRIP_RE.sub("", text.lower()).replace(" ", "-")


class _Slugger:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        base = github_slug(text)
        result = base
        while result in self._seen:
            self._seen[base] += 1
            result = f"{base}-{self._seen[base]}"
        self._seen[result] = 0
        return result


def anchor_ids(headings: list[Heading]) -> list[str]:
    """Return one explicit or generated addressable id per heading."""
    slugger = _Slugger()
    ids: list[str] = []
    for heading in headings:
        unique = slugger.slug(heading.text)
        ids.append(heading.anchor if heading.anchor is not None else unique)
    return ids


def strip_heading_anchor(text: str) -> str:
    """Remove a valid trailing explicit anchor from one raw heading line."""
    return _ANCHOR_RE.sub(" ", text).rstrip()
```

- [x] **Step 6: Replace compatibility code in sections.py with wrappers**

Remove `_HEADING_RE`, `_ATX_CLOSING_RE`, `_FENCE_RE`, `_SLUG_STRIP_RE`, `_Slugger`, and the local
`Heading`. Import and re-export `Heading`, `anchor_ids`, and `github_slug`; implement `build_toc` as
`return extract_headings(body)`; and make `section_text` call `strip_heading_anchor` on its first
line. Keep `split_body_lines`, `section_span`, `section_spans`, and their behavior unchanged.

```python
from .markdown_compat import (
    Heading,
    anchor_ids,
    extract_headings,
    github_slug,
    strip_heading_anchor,
)


def build_toc(body: str) -> list[Heading]:
    """Return supported ATX headings through the pinned compatibility adapter."""
    return extract_headings(body)


# Inside section_text, after selecting chunk:
if chunk:
    chunk[0] = strip_heading_anchor(chunk[0])
```

- [x] **Step 7: Run focused tests and verify GREEN**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest --no-cov tests/test_slugger_generator.py tests/test_markdown_compat.py tests/test_sections.py tests/test_loader.py -v`

Expected: all pass with the new adapter serving existing imports.

- [x] **Step 8: Verify the committed data exhaustively**

Run:
`UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python scripts/generate_github_slugger_data.py --check`

Expected: exit 0 with an exact match over 1,112,064 Unicode scalar values.

- [x] **Step 9: Commit the slug compatibility boundary**

```bash
git add scripts/generate_github_slugger_data.py src/doc_lattice/_github_slugger_data.py \
  src/doc_lattice/markdown_compat.py src/doc_lattice/sections.py \
  tests/test_markdown_compat.py tests/test_slugger_generator.py
git commit -m "refactor: generate pinned GitHub slug compatibility data"
```

### Task 3: Dependency and cache structural parity

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_orchestrate.py`

- [x] **Step 1: Add a cold/warm parity test**

Create a temporary project whose one node body contains fences, repeated Unicode headings, an
explicit marker, and an empty heading. Load without a cache, then with a fresh cache key, then warm.
Assert the three returned `Lattice` objects are equal and the section locations have the same
`TargetId`, start, and end values. Spy on `orchestrate.derive_file_sections` and assert the warm run
does not call it.

- [x] **Step 2: Run the parity test**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest --no-cov tests/test_orchestrate.py -k section_compatibility -v`

Expected: PASS as a characterization invariant.

- [x] **Step 3: Make markdown-it-py an exact direct dependency**

Add `"markdown-it-py==4.2.0"` to `[project].dependencies`, then run:

`UV_CACHE_DIR=/tmp/uv-cache uv lock`

Expected: `uv.lock` lists `markdown-it-py` directly under the doc-lattice package while retaining
version 4.2.0.

- [x] **Step 4: Run loader, cache, and orchestration suites**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest --no-cov tests/test_loader.py tests/test_cache_schema.py tests/test_cache.py tests/test_orchestrate.py -v`

Expected: all pass with unchanged cache version and payload structure.

- [x] **Step 5: Commit dependency and parity evidence**

```bash
git add pyproject.toml uv.lock tests/test_orchestrate.py
git commit -m "test: preserve cache structure across markdown adapter"
```

### Task 4: Reproducible section benchmark

**Files:**
- Create: `tests/test_bench_sections.py`
- Create: `scripts/bench_sections.py`

- [x] **Step 1: Write failing benchmark-helper tests**

Test that `build_document(100)` contains exactly 100 addressable headings plus fenced hidden
headings, and that `regression_percent(100.0, 120.0) == 20.0`. Test the threshold accepts 120 ms
against a 100 ms baseline and rejects 120.01 ms.

- [x] **Step 2: Run and verify RED**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest tests/test_bench_sections.py -v`

Expected: collection fails because `scripts/bench_sections.py` is absent.

- [x] **Step 3: Implement benchmark CLI**

Implement `build_document`, `regression_percent`, and `benchmark`. Default to 10,000 headings,
seven measured runs, and two warmups. CLI options are `--headings`, `--runs`, `--baseline-ms`, and
`--max-regression-percent` (default 20). Print bytes, lines, derived heading count, every sample,
median milliseconds, and regression. Exit 1 only when the optional baseline threshold is exceeded.

- [x] **Step 4: Run benchmark on main and candidate implementations**

Use the committed script against the main checkout's `src` and this worktree's `src`, with the same
Python executable and default corpus. Record both medians, then run the candidate again with
`--baseline-ms` set to the main median.

Expected: candidate median is no more than 20 percent above main and the threshold run exits 0.

Recorded result on the 736,570-byte, 41,354-line default corpus: main median 93.993 ms, candidate
median 100.277 ms, regression 6.685 percent, threshold passed.

- [x] **Step 5: Commit benchmark**

```bash
git add scripts/bench_sections.py tests/test_bench_sections.py
git commit -m "perf: add section compatibility benchmark"
```

### Task 5: Documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-07-13-markdown-compatibility-adapter-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-markdown-compatibility-adapter.md`

- [x] **Step 1: Document the compatibility contract**

README must state the supported top-level ATX/fence subset, exclusions, explicit marker rule,
`markdown-it-py==4.2.0`, and `github-slugger@2.0.0`. CLAUDE.md must add `markdown_compat.py`, the
generated data module, and the generation/check command to its architecture and command sections.
Add one `[Unreleased]` Changed entry to CHANGELOG.md.

- [x] **Step 2: Mark completed plan checkboxes and run diff checks**

Run: `git diff --check`, `rg -n "T[B]D|T[O]DO|F[I]XME"`, and
`rg -n -P "\x{2014}|\x{2026}"` over all changed documentation and source comments. Expected: no
findings and exit 0 from `git diff --check`.

- [x] **Step 3: Run the full verification matrix**

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev pytest
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev ruff check src tests scripts
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev ruff format --check src tests scripts
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev ty check src
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python scripts/check_typing_boundaries.py src
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python scripts/check_version_sync.py
UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python scripts/generate_github_slugger_data.py --check
```

Expected: every command exits 0, full pytest coverage remains at least 80 percent, and generator
output confirms exhaustive parity.

- [x] **Step 4: Review acceptance criteria against authoritative evidence**

Inspect the adapter imports, JSON fixture case names, generated header and check output, README
version text, cold/warm test output, and benchmark threshold output. Each of issue #88's six
acceptance criteria must have direct evidence before publishing.

- [x] **Step 5: Commit documentation and final cleanup**

```bash
git add README.md CLAUDE.md CHANGELOG.md docs/superpowers/specs \
  docs/superpowers/plans/2026-07-13-markdown-compatibility-adapter.md
git commit -m "docs: define markdown compatibility contract"
```

### Task 6: Review and publish the pull request

**Files:**
- Read: `.github/pull_request_template.md`
- Read: all branch changes against `origin/main`

- [x] **Step 1: Use the requesting-code-review workflow**

Review the complete diff against the issue and inspect any findings before publication. Apply
needed fixes with a failing regression test first, then repeat the full relevant verification.

- [ ] **Step 2: Use the finishing-development-branch workflow**

Confirm the branch contains only intentional commits, the worktree is clean, and all required
verification is fresh.

- [ ] **Step 3: Use the yeet workflow to publish**

Push `refactor/compatibility-adapter` and open a draft PR whose body follows the repository template,
references `Closes #88`, lists the parser/slug compatibility versions, reports golden/cache tests,
includes main and candidate benchmark medians, and gives the full verification commands.

- [ ] **Step 4: Verify remote PR state**

Read the created PR and checks. Confirm the head branch and commit SHA match local HEAD, the issue is
linked, and no required check is immediately failing because of the patch.
