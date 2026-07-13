# Markdown Compatibility Adapter Design

**Issue:** GitHub issue #88, `Replace bespoke Markdown section parsing with a maintainable
compatibility adapter`

## 1. Problem and outcome

Section identity is product behavior. Today `sections.py` owns three separate concerns: a
Markdown fence and heading state machine, section span utilities, and a manually translated
Unicode regular expression from `github-slugger@2.0.0`. The behavior is well tested, but the
compatibility boundary is implicit and updating either upstream behavior requires hand-editing
specialized parsing or Unicode data.

This change introduces one documented adapter for the two upstream-sensitive operations:

1. extract the supported headings from Markdown with exact source-line locations; and
2. generate unique GitHub-compatible heading slugs in document order.

The adapter uses `markdown-it-py==4.2.0` as an explicit production dependency and retains
`github-slugger@2.0.0` as the explicitly pinned slug compatibility target. Existing callers keep
using the public functions in `sections.py`, which delegate through the adapter. Section spans,
section text, cache records, target ids, duplicate-id behavior, and command output remain
structurally unchanged.

## 2. Supported Markdown contract

Doc-lattice supports this intentionally narrow Markdown subset for addressable sections:

- top-level ATX headings at levels 1 through 6 whose first character is `#`;
- empty ATX headings, such as `#` and `##   `;
- the CommonMark optional ATX closing `#` sequence;
- backtick and tilde fenced code blocks, including CommonMark opener, closer, indentation,
  marker-character, marker-length, and unclosed-fence rules; and
- an optional valid trailing `{#anchor}` marker in the heading content, immediately before the
  end of content or an ATX closing sequence.

Setext headings, headings in block quotes or list items, indented ATX headings, and headings inside
fenced code are not addressable. Inline Markdown remains raw heading content for slugging, matching
the current public semantics. Explicit marker syntax and validation remain doc-lattice extensions,
not parser plugins.

The exact parser compatibility version is `markdown-it-py==4.2.0`. The adapter configuration uses
the upstream normalization, block, fence, and ATX-heading rules. It disables unrelated block and
inline parsing and replaces paragraph token creation with a fallback that advances one source line.
That fallback does not recognize Markdown. It only lets the upstream fence and heading rules scan
the document without building unused paragraph or inline tokens.

## 3. Adapter interface and responsibilities

`src/doc_lattice/markdown_compat.py` owns the compatibility boundary:

```python
MARKDOWN_COMPAT_VERSION = "markdown-it-py==4.2.0"
SLUG_COMPAT_VERSION = "github-slugger@2.0.0"

@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str
    anchor: str | None
    line: int

def extract_headings(body: str) -> list[Heading]: ...
def github_slug(text: str) -> str: ...
def anchor_ids(headings: list[Heading]) -> list[str]: ...
```

`extract_headings` normalizes newlines through the existing shared normalizer, parses the focused
token stream, accepts only level-zero ATX heading tokens whose source line begins with `#`, converts
the zero-based token source map to a 1-based line, and applies the existing trailing-anchor rule to
the raw inline token content. Missing or malformed token structure is an adapter invariant failure,
not user input failure; the exact parser version and direct tests make that state actionable during
development rather than silently changing document identity.

`github_slug` implements the pinned upstream operation: lowercase, remove every code point in the
generated strip class, then replace each ASCII space with `-`. `anchor_ids` owns document-order
deduplication. Every heading reserves its generated slug even when its addressable id is an explicit
marker, preserving the current mixed marker/slug collision behavior.

`src/doc_lattice/sections.py` retains line splitting, `section_span`, batch `section_spans`, and
`section_text`. It imports and re-exports `Heading`, `github_slug`, and `anchor_ids`, and makes
`build_toc` a compatibility-preserving wrapper over `extract_headings`. This keeps the current
internal Python import surface while moving all upstream-sensitive parsing and slug behavior behind
one module boundary.

## 4. Generated slug compatibility data

`src/doc_lattice/_github_slugger_data.py` is generated and must not be hand-edited. It contains the
Python regular-expression pattern derived mechanically from the strip behavior of
`github-slugger@2.0.0` and records the exact upstream version and integrity metadata in its header.
`markdown_compat.py` compiles that generated pattern.

`scripts/generate_github_slugger_data.py` is the maintenance entry point. It installs or reads the
exact pinned npm package in a temporary working directory, asks Node to evaluate the upstream regex
over every Unicode scalar value, coalesces the stripped code points into ranges, and renders the
Python artifact deterministically. `--check` compares a fresh rendering with the committed file.
The script also reports the upstream package version and an exhaustive scalar-value count so an
update cannot be mistaken for a hand-edited approximation. Node and npm are maintenance-time tools
only; neither is a runtime nor Python development dependency.

Tests cover the official operation with representative upstream fixtures and divergent Unicode
categories. The exhaustive generator check is run and recorded during this change. A future slugger
upgrade changes the version constant and regenerates the artifact through the same command.

## 5. Data flow and compatibility

The uncached path remains:

```text
body
  -> sections.build_toc
  -> markdown_compat.extract_headings
  -> markdown_compat.anchor_ids
  -> sections.section_spans
  -> FileSections(total_lines, SectionRecord...)
  -> lattice index and ancestors
```

The warm cache continues reconstructing the same `FileSections` value from the version-1 cache
payload. No cache schema, cache version, serialization ordering, or invalidation rule changes. A
golden document is loaded uncached, through a cold cache write, and through a warm cache read; all
three `Lattice` values and serialized section records must be structurally equal.

Duplicate explicit markers, duplicate generated slugs, and marker/slug collisions continue to
reach `loader.build_lattice` as equal `SectionRecord.anchor` values and raise the existing
`DuplicateIdError`. The adapter introduces no new user-facing exceptions or recovery paths.

## 6. Golden tests

`tests/fixtures/markdown_compatibility.json` is the behavior authority for the supported subset.
Each case contains Markdown input plus expected heading records, addressable ids, and section spans.
Collectively the cases cover:

- both fence characters, mismatched and short closers, trailing closer content, indentation, and
  unclosed fences;
- ATX closing sequences and literal `#` content;
- valid, invalid, nontrailing, and closing-sequence-adjacent explicit anchors;
- repeated slugs, collisions with already suffixed slugs, and marker headings that reserve slugs;
- non-ASCII letters, combining marks, symbols, emoji, and empty slugs;
- empty headings and rejected spaceless or level-7 headings; and
- nested heading levels with inclusive 1-based section spans.

Focused unit tests also prove the parser configuration, version constants, delegating wrappers,
generated-data provenance, single-section span API, shared newline normalization, and section-text
marker removal. Existing loader, cache, hashing, resolution, and command tests remain regression
coverage for downstream semantics.

## 7. Performance gate

`scripts/bench_sections.py` generates a deterministic representative document containing 10,000
headings, ordinary prose, explicit markers, Unicode headings, and fenced code samples. It measures
the median of seven warmed `derive_file_sections` calls and reports document bytes, lines, heading
count, sample timings, and median milliseconds. It accepts an optional baseline so maintainers can
fail the benchmark when the candidate median exceeds the baseline by more than 20 percent.

The pre-change implementation measured between 115 ms and 163 ms locally for a 434 KB document
with 10,000 headings and 271 fenced blocks. A full CommonMark tokenization measured about 308 ms
and is rejected. A prototype of the focused parser configuration measured about 72 ms before slug
and span assembly. The completed adapter must be benchmarked with the committed script against the
same main-branch implementation and stay within the 20 percent threshold. Timings are evidence for
the pull request, not a hardware-dependent CI test.

## 8. Documentation and dependency changes

`pyproject.toml` adds the exact direct dependency `markdown-it-py==4.2.0`, and `uv.lock` is updated.
The README documents the supported section-heading subset and both compatibility versions.
`CLAUDE.md` maps the new compatibility and generated-data modules and points slug updates to the
generator. `CHANGELOG.md` records the internal refactor and unchanged public semantics.

## 9. Alternatives considered

1. **Focused `markdown-it-py` adapter, selected.** It supplies maintained CommonMark fence and ATX
   rules plus exact source maps, is pure Python, is already present transitively, and becomes an
   explicit exact dependency. Disabling irrelevant parsing meets the performance requirement.
2. **Full `markdown-it-py` CommonMark parse.** This is simpler configuration, but measured roughly
   2.7 times the current extractor on the representative document and materially regresses a core
   load path.
3. **`cmark-gfm`.** This is close to GitHub rendering and fast, but introduces compiled wheels,
   complicates exact source-line recovery, and broadens packaging risk for a narrow subset.
4. **Keep the local parser behind an interface.** This isolates callers but retains the bespoke
   state machine that motivated the issue.
5. **Call Node or `github-slugger` at runtime.** This provides direct slug execution but makes an
   offline Python CLI depend on a second runtime and process boundary. Node belongs only in the
   maintenance verifier.

## 10. Acceptance evidence

The change is complete when:

- production parsing and slugging route only through the documented adapter;
- JSON golden fixtures cover every issue-listed case and the supported-subset exclusions;
- the generated slug artifact reproduces `github-slugger@2.0.0` over every Unicode scalar value;
- README and code state the Markdown subset and exact upstream versions;
- cache cold/warm structural parity is demonstrated by a dedicated integration test;
- the committed benchmark shows no more than a 20 percent median regression against main; and
- the full tests, coverage gate, lint, format, type check, typing-boundary check, and version-sync
  check pass.
