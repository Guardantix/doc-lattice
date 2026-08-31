# Comment Envelope v7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the v7 release that lets a tracked Markdown file carry its lattice metadata in a
GitHub-invisible HTML comment envelope instead of `---` fences, and that closes the silent
rebinding hazards auto-slug section identity brings with it: probe-complete ambiguous-target
detection with a first-class `AMBIGUOUS` edge state, a context-inclusive section hash, a safe
heading-display representation, and a required `lattice_format: 2` version-skew guard.

**Architecture:** The comment envelope is a second spelling of the same YAML. `NodeMeta`,
`RawEdge`, `Edge.resolve`, and every module downstream of parsing stay untouched.
`frontmatter_parser.py` gains the second envelope grammar and one document-level entry point
(`parse_document`) that both `orchestrate.py` load paths adopt; `reconcile.py` keeps its single
`split_frontmatter_parts` call site and re-emits `parts.open_fence` / `parts.close_fence`
byte-for-byte, so the delimiters a file was written with survive whichever spelling it uses.
Collision components are derived in `markdown_compat.py` from the slugger's full allocation trace
over the full GitHub heading inventory, persisted per section by `loader.py` into
`SectionRecord.collision`, carried through the cache in `cache/schema.py`, and exposed on the
lattice as `Lattice.collisions`. Every sink that names a colliding heading reads the already
sanitized label the derivation stored.

**Tech Stack:** Python >= 3.13, `uv`, pydantic v2 (`strict=True, extra="forbid"`), ruamel.yaml
(pure parser at the frontmatter boundary), `markdown-it-py==4.2.0`, `github-slugger@2.0.0`
generated data, Typer + Rich for the CLI, pytest with hypothesis.

**Spec:** docs/superpowers/specs/2026-08-31-comment-envelope-design.md

## Global Constraints

- Python 3.13 or later. Dependency and project commands go through `uv`, never bare `pip` or an
  ad hoc virtualenv.
- Run every test command as
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest ...`. The dev shell exports
  `VIRTUAL_ENV=.devenv` (Python 3.12), which shadows `uv run`, and `FORCE_COLOR=3`, which breaks
  the human-output substring assertions.
- A full-suite run adds `-n auto --dist loadfile`. Bare `-n auto` splits
  `tests/test_reconcile_fuzz.py`'s module-level claim accumulator and fails it spuriously.
- Ruff line length is 100. Every module needs a module docstring, and every public function needs
  a Google-style docstring. No em dashes anywhere in drafted content, including this plan and
  every message string it introduces; use commas, colons, or parentheses.
- `typing.Any` and `typing.cast` stay inside the three modules
  `scripts/check_typing_boundaries.py` allowlists (`doc_lattice/frontmatter_parser.py`,
  `doc_lattice/linear_parser.py`, `doc_lattice/yaml_boundary.py`). This plan opens no new
  boundary and adds no new `Any`.
- Every custom exception extends `ProjectError` and carries a code from the `ErrorCode` Literal.
  This plan adds **no** new `ErrorCode` member: the fail-closed envelope errors reuse
  `FrontmatterError` (`FRONTMATTER_ERROR`), the unclosed-envelope error reuses
  `UnreadableDocError` (`UNREADABLE_DOC`), and the reconcile ambiguity refusal reuses
  `ValidationError` (`VALIDATION_ERROR`). `tests/test_error_types.py`'s
  `test_the_declared_domain_has_no_members_without_an_error_type` therefore stays green with no
  README error-table row edit.
- A string domain needing both a `Literal` and a runtime value set keeps one source of truth in
  `constants.py`: derive the `frozenset` from the type with `get_args()`.
- `CACHE_VERSION` moves 5 to 6 **exactly once**, in Task 9. No other task touches it.
- Version target is **7.0.0**: `src/doc_lattice/__init__.py`, `pyproject.toml`, and the first
  versioned `CHANGELOG.md` heading move together in Task 17.
- `tests/conftest.py`'s `lattice_dir` fixture **must not be modified** (spec scope rule, GTX-168).
  Every comment-spelling fixture is a new file written inside the test that needs it. The fixture
  writes no `.doc-lattice.yml`, so it keeps running zero-config after Task 15.
- Gates that must be green before handoff:
  `uv run --group dev ruff check src tests scripts`,
  `uv run --group dev ruff format --check src tests scripts`,
  `uv run --group dev ty check src scripts`,
  `uv run --group dev python scripts/check_typing_boundaries.py src`,
  `uv run --group dev python scripts/check_version_sync.py`,
  `uv run --group dev python scripts/check_doc_links.py`,
  `uv run --group dev python scripts/check_migration_rule.py`.
- `scripts/check_version_sync.py`'s `PIN_MANIFEST` declares `{"README.md": 3, "MANAGED_CI.md": 5}`
  as the exact count of recognized `doc-lattice==X.Y.Z` / `doc-lattice@vX.Y.Z` install pins each
  document carries. **This plan does not change how many pinned install refs any document
  carries.** The new README "Publishing on GitHub" section carries no install pin, so
  `PIN_MANIFEST` is left untouched and only the version inside the existing pins moves in
  Task 17.

---

### Task 1: Envelope-kind domain and the extended split result

**Files:**
- Modify: `src/doc_lattice/constants.py`
- Modify: `src/doc_lattice/frontmatter_parser.py`
- Test: `tests/test_constants.py`, `tests/test_frontmatter_parser.py`

**Interfaces:**
- Produces: `EnvelopeKind = Literal["fence", "comment"]` and
  `VALID_ENVELOPE_KINDS: frozenset[str]` in `constants.py`.
- Produces: `COMMENT_ENVELOPE_OPEN: str = "<!-- doc-lattice"` and
  `COMMENT_ENVELOPE_CLOSE: str = "-->"` in `constants.py`.
- Produces: `FrontmatterParts` gains `kind: EnvelopeKind`, `meta_start: int`, `meta_end: int`.
- Consumes: `split_frontmatter_parts(text: str, source: Path) -> FrontmatterParts | None`
  (signature unchanged).

Note on field naming: `open_fence` and `close_fence` keep their names and hold the envelope's
opening and closing delimiter line for **either** spelling. `tests/test_conventions.py`'s
reconcile reassembly guard pins `ENVELOPE_ORDER` by those exact field names, and keeping them
means the byte-exact reassembly expression in `apply_reconcile` never changes, so a security
guard is not disturbed to rename two fields.

Steps:

- [ ] Write the failing constants test. Append to `tests/test_constants.py`:

```python
def test_envelope_kind_domain_is_derived_from_the_literal():
    from typing import get_args

    from doc_lattice.constants import (
        COMMENT_ENVELOPE_CLOSE,
        COMMENT_ENVELOPE_OPEN,
        VALID_ENVELOPE_KINDS,
        EnvelopeKind,
    )

    assert VALID_ENVELOPE_KINDS == frozenset(get_args(EnvelopeKind))
    assert VALID_ENVELOPE_KINDS == {"fence", "comment"}
    assert COMMENT_ENVELOPE_OPEN == "<!-- doc-lattice"
    assert COMMENT_ENVELOPE_CLOSE == "-->"
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_constants.py -q -k envelope_kind`
  Expected: `ImportError: cannot import name 'COMMENT_ENVELOPE_CLOSE'`.

- [ ] Add the domain to `src/doc_lattice/constants.py`, directly under
  `VALID_FRONTMATTER_DISPOSITIONS`:

```python
# Which envelope a metadata-bearing file declares its frontmatter with. Both spellings are
# accepted unconditionally and forever (AD-44); there is no selector, because nothing needs to
# forbid either one. "fence" is the historical `---` block. "comment" is the HTML comment GitHub
# renders as nothing, which is what lets a tracked README keep a clean landing page. The kind is
# derived from the file at read time and deliberately not cached: reconcile rereads the file it
# rewrites anyway.
EnvelopeKind = Literal["fence", "comment"]
VALID_ENVELOPE_KINDS: frozenset[str] = frozenset(get_args(EnvelopeKind))

# The comment envelope's two delimiter lines, byte-exact. The opener admits no whitespace
# variance at all, unlike the fence rule, because CommonMark reads a four-space-indented opener
# as an indented code block and would print the "invisible" envelope as literal text while
# doc-lattice tracked the file.
COMMENT_ENVELOPE_OPEN: str = "<!-- doc-lattice"
COMMENT_ENVELOPE_CLOSE: str = "-->"
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_constants.py -q -k envelope_kind`
  Expected: 1 passed.

- [ ] Write the failing split-result test. Append to `tests/test_frontmatter_parser.py`:

```python
def test_fence_split_reports_its_kind_and_inner_yaml_offsets():
    text = "﻿---   \nid: pc\n---  \n# Body\n"

    parts = split_frontmatter_parts(text, Path("a.md"))

    assert parts is not None
    assert parts.kind == "fence"
    assert text[parts.meta_start : parts.meta_end] == parts.raw_meta
    assert parts.raw_meta == "id: pc\n"


def test_fence_split_offsets_are_empty_for_an_empty_block():
    parts = split_frontmatter_parts("---\n---\n# Body\n", Path("a.md"))

    assert parts is not None
    assert parts.raw_meta == ""
    assert parts.meta_start == parts.meta_end
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k offsets`
  Expected: `AttributeError: 'FrontmatterParts' object has no attribute 'kind'`.

- [ ] Extend the dataclass in `src/doc_lattice/frontmatter_parser.py`. Replace the
  `FrontmatterParts` docstring attribute list and field block with:

```python
@dataclass(frozen=True, slots=True)
class FrontmatterParts:
    """Every source piece a document's frontmatter block is spelled with.

    ``open_fence`` and ``close_fence`` hold the envelope's opening and closing delimiter line
    whichever spelling the file uses, so the byte-exact rewriter reattaches what the author
    wrote without branching on ``kind``.

    Attributes:
        prefix: A leading byte-order mark, which precedes the opening delimiter.
        open_fence: The opening delimiter line as written, any surrounding space included.
        raw_meta: The YAML between the delimiters, empty when the block holds none.
        close_fence: The closing delimiter line as written.
        close_fence_newline: The newline ending the closing delimiter, empty at end of file.
        body: Everything after that newline.
        kind: Which envelope the file declared.
        meta_start: Offset into the original text where ``raw_meta`` begins.
        meta_end: Offset into the original text where ``raw_meta`` ends, so
            ``text[meta_start:meta_end] == raw_meta`` for either spelling.
    """

    prefix: str
    open_fence: str
    raw_meta: str
    close_fence: str
    close_fence_newline: str
    body: str
    kind: EnvelopeKind
    meta_start: int
    meta_end: int
```

  Add `from .constants import COMMENT_ENVELOPE_CLOSE, COMMENT_ENVELOPE_OPEN, EnvelopeKind,
  LATTICE_INTENT_KEYS` to the existing constants import, and replace the body of
  `split_frontmatter_parts` with a delimiter-agnostic helper:

```python
def split_frontmatter_parts(text: str, source: Path) -> FrontmatterParts | None:
    """Split a document into every piece its frontmatter block is written with.

    ``split_frontmatter`` returns the two pieces a reader needs. This returns the rest of
    them as well, since a byte-exact rewrite has to put back the delimiters the author wrote,
    and any byte-order mark before them, rather than the spelling this engine would choose.

    Args:
        text: The full file text.
        source: The file the frontmatter came from, for error messages.

    Returns:
        The document's frontmatter pieces, or None if it opens neither envelope.

    Raises:
        UnreadableDocError: If an opening delimiter has no closing delimiter.
    """
    # Strip a leading UTF-8 BOM (U+FEFF) so a file saved with one still has its opening
    # "---" fence recognized on line 0 instead of being read as having no frontmatter.
    stripped = text.lstrip(_BOM)
    prefix = text[: len(text) - len(stripped)]
    lines = stripped.split("\n")
    if not lines:
        return None
    if lines[0].strip() == _FENCE:
        return _split_envelope(prefix, lines, "fence", source)
    return None


def _split_envelope(
    prefix: str, lines: list[str], kind: EnvelopeKind, source: Path
) -> FrontmatterParts:
    """Split an already-recognized envelope into its pieces.

    Args:
        prefix: The leading byte-order mark, empty when the file carries none.
        lines: The BOM-stripped document split on newlines.
        kind: The envelope the opening line declared.
        source: The file the frontmatter came from, for error messages.

    Returns:
        The document's frontmatter pieces.

    Raises:
        UnreadableDocError: If the opening delimiter has no closing delimiter.
    """
    closer = _FENCE if kind == "fence" else COMMENT_ENVELOPE_CLOSE
    for closing_index, line in enumerate(lines[1:], start=1):
        # The fence rule has always tolerated surrounding space on its closing line. The comment
        # closer does not, for the reason its opener does not: an indented "-->" is not a comment
        # terminator to CommonMark either.
        matched = line.strip() == closer if kind == "fence" else line == closer
        if not matched:
            continue
        raw_meta = "\n".join(lines[1:closing_index])
        # Splitting on newlines leaves the closing delimiter as the final element only when
        # the file ends on it, so a last line here is what shows the newline was there.
        trailing = "\n" if closing_index < len(lines) - 1 else ""
        meta_start = len(prefix) + len(lines[0]) + 1
        meta_end = meta_start + (len(raw_meta) + 1 if raw_meta else 0)
        return FrontmatterParts(
            prefix,
            lines[0],
            raw_meta + "\n" if raw_meta else "",
            line,
            trailing,
            "\n".join(lines[closing_index + 1 :]),
            kind,
            meta_start,
            meta_end,
        )
    if kind == "fence":
        msg = (
            f"unclosed YAML frontmatter in {format_path_for_display(source)}: "
            "add a closing '---' fence"
        )
    else:
        msg = (
            f"unclosed doc-lattice comment envelope in {format_path_for_display(source)}: "
            "add a closing '-->' line at column zero"
        )
    raise UnreadableDocError(msg, source=source)
```

- [ ] Run the whole parser suite and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py tests/test_constants.py -q`
  Expected: all pass, including the pre-existing
  `test_split_frontmatter_parts_keeps_every_piece_it_read` and the unclosed-fence message
  assertion at line 857, which pins the fence wording unchanged.

- [ ] Commit:
  `git commit -am "feat(frontmatter): add the envelope-kind domain and inner-YAML offsets"`

---

### Task 2: Comment envelope recognition

**Files:**
- Modify: `src/doc_lattice/frontmatter_parser.py`
- Test: `tests/test_frontmatter_parser.py`

**Interfaces:**
- Consumes: `_split_envelope(prefix, lines, kind, source) -> FrontmatterParts` from Task 1.
- Produces: `split_frontmatter_parts` recognizing an exact column-zero `<!-- doc-lattice` opener
  and refusing the BOM form.

The BOM refusal is not a judgement call. `markdown-it-py==4.2.0` parses
`"﻿<!-- doc-lattice\nid: a\n-->\n"` as a `paragraph`, not an `html_block`, so the envelope
would render as visible text. Per the spec, the BOM allowance is therefore dropped for the comment
spelling rather than shipped unproven, and dropping it silently would be the exact vanishing act
the near-miss rule exists to prevent, so it is an actionable error.

Steps:

- [ ] Write the failing tests. Append to `tests/test_frontmatter_parser.py`:

```python
COMMENT_DOC = "<!-- doc-lattice\nid: pc\ntitle: PC\n-->\n# Body\ntext\n"


def test_comment_envelope_is_split_like_a_fence():
    parts = split_frontmatter_parts(COMMENT_DOC, Path("a.md"))

    assert parts is not None
    assert parts.kind == "comment"
    assert parts.prefix == ""
    assert parts.open_fence == "<!-- doc-lattice"
    assert parts.raw_meta == "id: pc\ntitle: PC\n"
    assert parts.close_fence == "-->"
    assert parts.close_fence_newline == "\n"
    assert parts.body == "# Body\ntext\n"
    assert COMMENT_DOC[parts.meta_start : parts.meta_end] == parts.raw_meta


def test_comment_envelope_body_ends_at_the_first_column_zero_terminator():
    text = "<!-- doc-lattice\nid: pc\n  -->\n-->\n# Body\n"

    parts = split_frontmatter_parts(text, Path("a.md"))

    assert parts is not None
    assert parts.raw_meta == "id: pc\n  -->\n"
    assert parts.body == "# Body\n"


def test_unclosed_comment_envelope_is_a_hard_error():
    with pytest.raises(UnreadableDocError) as excinfo:
        split_frontmatter_parts("<!-- doc-lattice\nid: x\n# no terminator\n", Path("broken.md"))

    assert "unclosed doc-lattice comment envelope" in str(excinfo.value)
    assert "'-->'" in str(excinfo.value)


def test_a_byte_order_mark_before_the_comment_opener_is_refused():
    with pytest.raises(FrontmatterError) as excinfo:
        split_frontmatter_parts("﻿" + COMMENT_DOC, Path("bom.md"))

    assert "byte-order mark" in str(excinfo.value)
    assert "'---'" in str(excinfo.value)


def test_comment_syntax_below_line_one_is_ordinary_content():
    text = "---\nid: pc\n---\n# Body\n\n<!-- doc-lattice\nid: other\n-->\n"

    parts = split_frontmatter_parts(text, Path("a.md"))

    assert parts is not None
    assert parts.kind == "fence"
    assert "<!-- doc-lattice" in parts.body


def test_a_crlf_comment_envelope_is_read_after_the_normalization_every_load_does():
    # `discovery.decode_doc` translates CRLF and lone CR to LF before any splitter sees the
    # text, which is what the fence suite's own CRLF case relies on. The fence grammar
    # tolerates a raw `\r` anyway because it compares `line.strip()`; the comment grammar is
    # byte-exact and does not, so a raw CRLF opener lands in the near-miss tier and fails loud
    # rather than vanishing. Both halves are pinned, since the difference is deliberate.
    raw = "<!-- doc-lattice\r\nid: pc\r\n-->\r\n# Body\r\n"

    with pytest.raises(FrontmatterError):
        split_frontmatter_parts(raw, Path("a.md"))

    parts = split_frontmatter_parts(normalize_newlines(raw), Path("a.md"))

    assert parts is not None
    assert parts.kind == "comment"
    assert parts.raw_meta == "id: pc\n"
```

  Add `from doc_lattice.hashing import normalize_newlines` to the test file's imports.

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k comment_envelope or byte_order_mark`
  Expected: `assert parts is not None` fails, because the opener is not recognized yet.

- [ ] Implement recognition. In `split_frontmatter_parts`, replace the trailing `return None`
  with:

```python
    if lines[0] == COMMENT_ENVELOPE_OPEN:
        if prefix:
            msg = (
                f"doc-lattice comment envelope in {format_path_for_display(source)} is preceded "
                "by a byte-order mark, which stops Markdown renderers reading it as a comment, "
                f"so it would print as text; remove the mark or use the '{_FENCE}' fence spelling"
            )
            raise FrontmatterError(msg, source=source)
        return _split_envelope(prefix, lines, "comment", source)
    return None
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q`
  Expected: all pass.

- [ ] Commit:
  `git commit -am "feat(frontmatter): recognize the column-zero comment envelope"`

---

### Task 3: The opener near-miss error

**Files:**
- Modify: `src/doc_lattice/frontmatter_parser.py`
- Test: `tests/test_frontmatter_parser.py`

**Interfaces:**
- Produces: `_OPENER_NEAR_MISS: re.Pattern[str]` in `frontmatter_parser.py`.
- Consumes: `split_frontmatter_parts` from Task 2.

Steps:

- [ ] Write the failing test. Append to `tests/test_frontmatter_parser.py`:

```python
@pytest.mark.parametrize(
    "opener",
    [
        "<!-- doc-lattice ",
        " <!-- doc-lattice",
        "    <!-- doc-lattice",
        "\t<!-- doc-lattice",
        "<!-- DOC-LATTICE",
        "<!--doc-lattice",
        "<!--  doc-lattice",
        "<!-- doc-lattice\t",
    ],
)
def test_a_near_miss_opener_is_an_actionable_error_not_untracked_prose(opener: str):
    text = f"{opener}\nid: pc\n-->\n# Body\n"

    with pytest.raises(FrontmatterError) as excinfo:
        split_frontmatter_parts(text, Path("near.md"))

    message = str(excinfo.value)
    assert "'<!-- doc-lattice'" in message
    assert "first line" in message


def test_an_ordinary_html_comment_on_line_one_stays_untracked():
    assert split_frontmatter_parts("<!-- notes -->\n# Body\n", Path("a.md")) is None
    assert split_frontmatter_parts("<!-- doc-lattice notes -->\n# B\n", Path("a.md")) is None
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k near_miss`
  Expected: `DID NOT RAISE <class 'FrontmatterError'>` for all eight cases.

- [ ] Add the pattern near `_FENCE` in `src/doc_lattice/frontmatter_parser.py`:

```python
# A first line that means the comment envelope but is not spelled exactly. The opener is
# byte-exact on purpose, and a byte-exact rule with no near-miss tier would let a trailing space
# make the intended node vanish from the lattice under a green gate, so the near miss is an
# error rather than ordinary prose. Deliberately wider than the whitespace forms: an author who
# writes `<!--doc-lattice` or `<!-- DOC-LATTICE` meant the envelope just as plainly.
_OPENER_NEAR_MISS = re.compile(r"^\s*<!--\s*doc-lattice\s*$", re.IGNORECASE)
```

  Add `import re` to the module imports, and insert the near-miss branch in
  `split_frontmatter_parts` between the exact-opener branch and the final `return None`:

```python
    if _OPENER_NEAR_MISS.fullmatch(lines[0]):
        msg = (
            f"line 1 of {format_path_for_display(source)} means the doc-lattice comment "
            "envelope but is not spelled exactly; the opener must be the first line and "
            f"exactly '{COMMENT_ENVELOPE_OPEN}' at column zero, with no indentation, no "
            "trailing whitespace, and no case variance"
        )
        raise FrontmatterError(msg, source=source)
    return None
```

- [ ] Narrow the pre-existing hypothesis identity property, which this task widens the raise
  surface of. `tests/test_frontmatter_parser.py::test_split_frontmatter_identity_when_no_opening_fence`
  draws `st.text()` and assumes only `first_line.strip() != "---"`, so it can now draw a first
  line that opens or near-misses the comment envelope and get an exception where it asserts a
  `(None, text)` identity. Add the matching assumption beside the existing one:

```python
@given(st.text())
def test_split_frontmatter_identity_when_no_opening_fence(text):
    first_line = text.lstrip("﻿").split("\n", 1)[0]
    assume(first_line.strip() != "---")
    # The comment envelope claims line 1 too, and a first line that means it without spelling it
    # exactly is an error rather than an identity, so both are drawn out of this property.
    assume(_OPENER_NEAR_MISS.fullmatch(first_line) is None)
    raw, body = split_frontmatter(text, Path("a.md"))
    assert raw is None
    assert body == text
```

  Import `_OPENER_NEAR_MISS` from `doc_lattice.frontmatter_parser` in the test module, which
  already imports that module as `frontmatter_parser_module` for other private access.

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q`
  Expected: all pass. `test_an_ordinary_html_comment_on_line_one_stays_untracked` confirms the
  pattern is anchored, so `<!-- doc-lattice notes -->` is untouched prose.

- [ ] Commit:
  `git commit -am "feat(frontmatter): refuse a near-miss comment envelope opener"`

---

### Task 4: Fail-closed classification and the `--` input refusal

**Files:**
- Modify: `src/doc_lattice/frontmatter_parser.py`
- Test: `tests/test_frontmatter_parser.py`

**Interfaces:**
- Produces: `parse_meta(raw_meta: str | None, source: Path, *, kind: EnvelopeKind = "fence")
  -> ParsedMeta`.
- Produces: `refuse_double_hyphen(raw_meta: str, source: Path, *, first_body_line: int) -> None`.
- Consumes: `FrontmatterError`, `format_path_for_display`.

The `fence` default on `parse_meta` keeps the roughly thirty existing two-argument call sites in
`tests/test_frontmatter_parser.py`, `tests/test_discovery.py`, and `tests/test_reconcile.py`
working, and states the historical behavior explicitly rather than by omission.

Steps:

- [ ] Write the failing fail-closed tests. Append to `tests/test_frontmatter_parser.py`:

```python
@pytest.mark.parametrize(
    "raw_meta",
    ["", "just a scalar\n", "- one\n- two\n", "title: PC\nlayer: design\n"],
)
def test_a_comment_envelope_without_an_id_fails_closed(raw_meta: str):
    with pytest.raises(FrontmatterError) as excinfo:
        parse_meta(raw_meta, Path("a.md"), kind="comment")

    message = str(excinfo.value)
    assert "doc-lattice comment envelope" in message
    assert "'id'" in message


def test_the_same_bodies_stay_soft_under_the_fence_spelling():
    assert parse_meta("", Path("a.md")).disposition == "untracked"
    assert parse_meta("- one\n", Path("a.md")).disposition == "untracked"
    assert parse_meta("title: PC\n", Path("a.md")).disposition == "id-less"


def test_a_comment_envelope_with_an_id_parses_like_a_fence():
    outcome = parse_meta("id: pc\ntitle: PC\n", Path("a.md"), kind="comment")

    assert outcome.disposition == "tracked"
    assert outcome.meta == NodeMeta(id="pc", title="PC")
```

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k fails_closed`
  Expected: `TypeError: parse_meta() got an unexpected keyword argument 'kind'`.

- [ ] Implement the fail-closed branches. In `src/doc_lattice/frontmatter_parser.py`, change the
  `parse_meta` signature and the two soft-degradation branches:

```python
def parse_meta(
    raw_meta: str | None, source: Path, *, kind: EnvelopeKind = "fence"
) -> ParsedMeta:
```

  Extend the docstring with:

```
    The fence spelling degrades softly, because it has innocent readings (Jekyll frontmatter, a
    thematic break). ``<!-- doc-lattice`` has exactly one reading: it declares lattice intent by
    name, so the comment spelling fails closed instead. Any body that is not a mapping carrying
    ``id`` is an error, and there is no untracked or id-less tier for it.
```

  and replace the two branches:

```python
    if not isinstance(data, dict):
        if kind == "comment":
            msg = (
                f"the doc-lattice comment envelope in {format_path_for_display(source)} does not "
                "hold a YAML mapping, so it declares no 'id'; the envelope names this engine, so "
                "an unusable body is an error rather than untracked prose"
            )
            raise FrontmatterError(msg, source=source)
        return _UNTRACKED
    if "id" not in data:
        if kind == "comment":
            msg = (
                f"the doc-lattice comment envelope in {format_path_for_display(source)} has no "
                "'id' key, so the file and every edge it declares would be dropped from the "
                "lattice; add an 'id' (check it for a typo)"
            )
            raise FrontmatterError(msg, source=source)
        return _id_less(data, source)
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k fails_closed or stay_soft or parses_like_a_fence`
  Expected: all pass.

- [ ] Write the failing `--` refusal test. Append to `tests/test_frontmatter_parser.py`:

```python
def test_a_double_hyphen_in_a_comment_envelope_body_is_refused_by_line():
    with pytest.raises(FrontmatterError) as excinfo:
        refuse_double_hyphen("id: pc\nlayer: foo--bar\n", Path("a.md"), first_body_line=2)

    message = str(excinfo.value)
    assert "line 3" in message
    assert "'--'" in message


def test_a_body_without_a_double_hyphen_is_accepted():
    assert refuse_double_hyphen("id: pc\nseen: abc123\n", Path("a.md"), first_body_line=2) is None
```

  and add `refuse_double_hyphen` to the module import list at the top of the test file.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k double_hyphen`
  Expected: `ImportError: cannot import name 'refuse_double_hyphen'`.

- [ ] Add the validator to `src/doc_lattice/frontmatter_parser.py`, after `split_frontmatter`:

```python
def refuse_double_hyphen(raw_meta: str, source: Path, *, first_body_line: int) -> None:
    """Refuse a comment envelope body carrying the substring ``--``.

    ``--`` inside an HTML comment is where the HTML specification and CommonMark versions
    disagree, and the failure mode is silent and user-facing: a legal but unlucky id such as
    ``foo--bar`` could terminate or invalidate the comment in some renderer and turn the
    invisible envelope into rendered text. Refusing the substring outright is stricter than
    GitHub requires today and keeps the invisibility guarantee independent of renderer behavior.
    The refusal is scoped to the comment spelling; the fence spelling accepts ``--`` as it always
    has, so converting a file that uses such an id means renaming the id or keeping the fence.

    Args:
        raw_meta: The envelope's inner YAML text.
        source: The file the envelope came from, for error messages.
        first_body_line: The 1-based file line ``raw_meta``'s first line occupies, so the
            diagnostic names a line the author can jump to.

    Raises:
        FrontmatterError: If any line of ``raw_meta`` contains ``--``.
    """
    for offset, line in enumerate(raw_meta.split("\n")):
        if "--" in line:
            msg = (
                f"the doc-lattice comment envelope in {format_path_for_display(source)} contains "
                f"'--' on line {first_body_line + offset}; HTML comments give '--' no agreed "
                "meaning, so a renderer may end the comment there and print the envelope as "
                f"text. Rename the value, or keep the '{_FENCE}' fence spelling for this file"
            )
            raise FrontmatterError(msg, source=source)
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q`
  Expected: all pass.

- [ ] Commit:
  `git commit -am "feat(frontmatter): fail closed on a branded envelope and refuse '--' in it"`

---

### Task 5: The `parse_document` seam and orchestrate adoption

**Files:**
- Modify: `src/doc_lattice/frontmatter_parser.py`
- Modify: `src/doc_lattice/orchestrate.py`
- Test: `tests/test_frontmatter_parser.py`, `tests/test_orchestrate.py`

**Interfaces:**
- Produces: `parse_document(text: str, source: Path) -> tuple[ParsedMeta, str]`.
- Consumes: `split_frontmatter_parts`, `parse_meta(..., kind=...)`, `refuse_double_hyphen` from
  Tasks 1 to 4.
- Replaces: the two `split_frontmatter(text, path)` plus `parse_meta(raw_meta, path)` pairs in
  `orchestrate.py` (`_load_uncached` line 118 and `_load_cached` line 160). Together with
  `reconcile.py`'s single `split_frontmatter_parts` call site at line 1575, those are the three
  call sites the spec names as adopting the extended result.

Steps:

- [ ] Write the failing test. Append to `tests/test_frontmatter_parser.py`:

```python
def test_parse_document_reads_either_spelling_and_returns_the_body():
    fence_outcome, fence_body = parse_document(DOC, Path("a.md"))
    comment_outcome, comment_body = parse_document(COMMENT_DOC, Path("b.md"))

    assert fence_outcome.disposition == "tracked"
    assert comment_outcome.disposition == "tracked"
    assert fence_outcome.meta is not None
    assert comment_outcome.meta is not None
    assert fence_outcome.meta.id == comment_outcome.meta.id == "pc"
    assert fence_body == "# Body\ntext\n"
    assert comment_body == "# Body\ntext\n"


def test_parse_document_refuses_a_double_hyphen_naming_the_file_line():
    text = "<!-- doc-lattice\nid: pc\ntitle: a--b\n-->\n# Body\n"

    with pytest.raises(FrontmatterError) as excinfo:
        parse_document(text, Path("a.md"))

    assert "line 3" in str(excinfo.value)


def test_parse_document_leaves_prose_untracked():
    outcome, body = parse_document("# No frontmatter\n", Path("a.md"))

    assert outcome.disposition == "untracked"
    assert body == "# No frontmatter\n"
```

  and add `parse_document` to the module import list at the top of the test file.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k parse_document`
  Expected: `ImportError: cannot import name 'parse_document'`.

- [ ] Add the entry point to `src/doc_lattice/frontmatter_parser.py`, after `parse_meta`:

```python
def parse_document(text: str, source: Path) -> tuple[ParsedMeta, str]:
    """Split and classify one whole document, in either envelope spelling.

    The one entry point the load paths use. Splitting and classifying are separate functions
    because the rewriter needs the split alone, but the comment spelling's rules span both: the
    ``--`` refusal is measured against the file's own line numbers, which only the split knows,
    and the fail-closed classification needs the envelope kind, which only the split derives.

    Args:
        text: The full file text, already newline-normalized by ``discovery.decode_doc``.
        source: The discovered path, named in every diagnostic this raises.

    Returns:
        The parse outcome and the document body after the envelope.

    Raises:
        UnreadableDocError: If an opening delimiter has no closing delimiter, or the YAML
            cannot be parsed.
        FrontmatterError: If the frontmatter has an unknown or malformed key, declares lattice
            intent with no ``id``, spells a near-miss comment opener, carries a byte-order mark
            before a comment opener, holds ``--`` inside a comment envelope, or is a comment
            envelope that is not a mapping carrying ``id``.
    """
    parts = split_frontmatter_parts(text, source)
    if parts is None:
        return _UNTRACKED, text
    if parts.kind == "comment":
        refuse_double_hyphen(
            parts.raw_meta,
            source,
            first_body_line=text[: parts.meta_start].count("\n") + 1,
        )
    return parse_meta(parts.raw_meta, source, kind=parts.kind), parts.body
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k parse_document`
  Expected: 3 passed.

- [ ] Adopt it in `src/doc_lattice/orchestrate.py`. Change the import on line 11 to
  `from .frontmatter_parser import parse_document`, then replace both call-site pairs.
  In `_load_uncached`:

```python
        text = read_doc(path)
        outcome, body = parse_document(text, path)
        _report_skip(outcome.disposition, path)
```

  In `_load_cached`:

```python
        text = decode_doc(doc_path, result.data)
        outcome, body = parse_document(text, doc_path)
        _report_skip(outcome.disposition, doc_path)
```

- [ ] Write the failing orchestrate test. Append to `tests/test_orchestrate.py`:

```python
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
```

  Add any missing imports (`Path`, `load_config`, `load_lattice`, `TargetId`) to the file's
  import block.

- [ ] Run both suites and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_orchestrate.py tests/test_frontmatter_parser.py -q`
  Expected: all pass.

- [ ] Commit:
  `git commit -am "feat(orchestrate): load either envelope spelling through parse_document"`

---

### Task 6: Misplacement warning as cached data

**Files:**
- Modify: `src/doc_lattice/constants.py`, `src/doc_lattice/markdown_compat.py`,
  `src/doc_lattice/frontmatter_parser.py`, `src/doc_lattice/orchestrate.py`
- Test: `tests/test_markdown_compat.py`, `tests/test_frontmatter_parser.py`,
  `tests/test_orchestrate.py`

**Interfaces:**
- Produces: `FrontmatterDisposition = Literal["tracked", "untracked", "id-less",
  "misplaced-envelope"]` in `constants.py`.
- Produces: `code_block_line_spans(body: str) -> list[tuple[int, int]]` in `markdown_compat.py`.
- Produces: `detect_misplaced_envelope(text: str) -> bool` in `frontmatter_parser.py`.
- Produces: `_report_misplaced_envelope(disposition: FrontmatterDisposition, path: Path) -> None`
  in `orchestrate.py`.
- Consumes: `parse_document` from Task 5; `Entry.disposition` and `CacheHit.disposition`, which
  are already typed `FrontmatterDisposition` and carry the new value with no code change.

Steps:

- [ ] Write the failing code-span test. Append to `tests/test_markdown_compat.py`:

```python
def test_code_block_line_spans_covers_fenced_and_indented_blocks():
    body = "# H\n\n```\n<!-- doc-lattice\n```\n\ntext\n\n    indented\n"

    spans = code_block_line_spans(body)

    assert (3, 5) in spans
    assert any(start <= 9 <= end for start, end in spans)
    assert not any(start <= 7 <= end for start, end in spans)
```

  and add `code_block_line_spans` to the `doc_lattice.markdown_compat` import list.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q -k code_block_line_spans`
  Expected: `ImportError: cannot import name 'code_block_line_spans'`.

- [ ] Add it to `src/doc_lattice/markdown_compat.py`, after `extract_headings`:

```python
def code_block_line_spans(body: str) -> list[tuple[int, int]]:
    """Return the 1-based inclusive line spans of every code block a render would show.

    Read from the pinned parser's full CommonMark parse rather than from the adapter's
    restricted heading scan, because the caller asks a rendering question ("would a reader see
    this as sample text") rather than an addressing one. Both fenced blocks and indented ones
    count, since either turns the text it holds into a quoted example.

    Args:
        body: Markdown document text.

    Returns:
        ``(start, end)`` line ranges in document order, both bounds inclusive.
    """
    normalized = normalize_newlines(body).replace("\0", "�")
    spans: list[tuple[int, int]] = []
    for token in _PARSER.parse(normalized):
        if token.type in ("fence", "code_block") and token.map is not None:
            spans.append((token.map[0] + 1, token.map[1]))
    return spans
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q`
  Expected: all pass.

- [ ] Write the failing detection tests. Append to `tests/test_frontmatter_parser.py`:

```python
def test_a_misplaced_envelope_below_line_one_is_detected():
    text = "# Title\n\n<!-- doc-lattice\nid: pc\n-->\n"

    outcome, body = parse_document(text, Path("a.md"))

    assert outcome.disposition == "misplaced-envelope"
    assert outcome.meta is None
    assert body == text


def test_a_quoted_envelope_example_is_not_a_misplacement():
    text = "# Title\n\n```markdown\n<!-- doc-lattice\nid: pc\n-->\n```\n"

    outcome, _body = parse_document(text, Path("a.md"))

    assert outcome.disposition == "untracked"


def test_a_tracked_file_quoting_the_other_spelling_stays_tracked():
    text = "---\nid: pc\n---\n# Body\n\n<!-- doc-lattice\nid: other\n-->\n"

    outcome, _body = parse_document(text, Path("a.md"))

    assert outcome.disposition == "tracked"


def test_the_substring_precheck_short_circuits_a_file_without_the_sentinel():
    assert detect_misplaced_envelope("# Title\n\nordinary prose\n") is False
```

  and add `detect_misplaced_envelope` to the module import list.

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q -k misplaced or quoted_envelope`
  Expected: `ImportError: cannot import name 'detect_misplaced_envelope'`.

- [ ] Add the disposition member in `src/doc_lattice/constants.py`, replacing the
  `FrontmatterDisposition` declaration and extending its comment:

```python
# What one discovered file's frontmatter turned out to be. "untracked" and "id-less" are the two
# distinct ways a file is left out of the lattice, which a bare "no node" answer conflated: the
# first is prose the engine has nothing to say about, the second is a metadata block that lost or
# never had its `id`. "misplaced-envelope" is the third: an otherwise untracked file carrying the
# comment opener somewhere other than line 1 and outside a code block, which is the "I put the
# envelope after the H1 and the file silently vanished" hole. The load cache persists this so a
# warm run replays the diagnostic a cold run emitted.
FrontmatterDisposition = Literal["tracked", "untracked", "id-less", "misplaced-envelope"]
VALID_FRONTMATTER_DISPOSITIONS: frozenset[str] = frozenset(get_args(FrontmatterDisposition))
```

- [ ] Add the detector and the shared outcome in `src/doc_lattice/frontmatter_parser.py`. Beside
  `_UNTRACKED` and `_ID_LESS`, add:

```python
_MISPLACED = ParsedMeta(meta=None, disposition="misplaced-envelope")
```

  and after `refuse_double_hyphen`:

```python
def detect_misplaced_envelope(text: str) -> bool:
    """Report whether an untracked document carries the comment opener where it will not be read.

    Two stage on purpose. The substring pre-check is what every ordinary document pays, and the
    markdown-it parse runs only on the rare file that actually holds the sentinel, so a
    README quoting the syntax in a fenced example is answered correctly without charging every
    other file for the parse.

    Args:
        text: The full file text of a document that opened no envelope.

    Returns:
        True when a line stripping to the opener sits outside every code block.
    """
    if COMMENT_ENVELOPE_OPEN not in text:
        return False
    coded = code_block_line_spans(text)
    for number, line in enumerate(normalize_newlines(text).split("\n"), start=1):
        if line.strip() != COMMENT_ENVELOPE_OPEN:
            continue
        if not any(start <= number <= end for start, end in coded):
            return True
    return False
```

  Add `from .markdown_compat import code_block_line_spans` and
  `from .hashing import normalize_newlines` to the module imports, and change the
  `parts is None` branch of `parse_document` to:

```python
    if parts is None:
        return (_MISPLACED if detect_misplaced_envelope(text) else _UNTRACKED), text
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_frontmatter_parser.py -q`
  Expected: all pass.

- [ ] Add the report site in `src/doc_lattice/orchestrate.py`, after `_report_reused_anchors`:

```python
def _report_misplaced_envelope(disposition: FrontmatterDisposition, path: Path) -> None:
    """Report an untracked file carrying the comment envelope where it will not be read.

    A separate function from ``_report_skip`` for the reason that shapes ``_report_skip``
    itself: Python renders a warning with its raising location and filters repeats by that
    location, so two diagnostics sharing one site would suppress each other. ``stacklevel``
    stays at its default 1 for the same reason, and every load path funnels here so a warm run
    reproduces what the cold run it replays said.

    The message deliberately does not open with ``skipping ``, matching
    ``_report_reused_anchors``: AD-29 records that ``PYTHONWARNINGS`` cannot single out the
    id-less skip because ``discovery.py``'s symlink-escape warning shares that prefix.

    Args:
        disposition: What the parse concluded about the file. Only ``"misplaced-envelope"``
            is reported.
        path: The discovered path as this checkout sees it, named in the message.
    """
    if disposition != "misplaced-envelope":
        return
    warnings.warn(
        f"misplaced doc-lattice envelope in {format_path_for_display(path)}: the "
        "'<!-- doc-lattice' opener is only read as the file's first line, so this file is not a "
        "lattice node; move the envelope to the top of the file",
        stacklevel=1,
    )
```

  Then call it at all three report sites, immediately after each `_report_skip(...)` call: once
  in `_load_uncached`, and twice in `_load_cached` (the `CacheHit` branch with
  `result.disposition`, and the miss branch with `outcome.disposition`).

- [ ] Write the failing cache-parity test. Append to `tests/test_orchestrate.py`:

```python
def test_the_misplacement_warning_replays_on_every_cache_tier(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "late.md").write_text("# Title\n\n<!-- doc-lattice\nid: late\n-->\n", encoding="utf-8")
    (tmp_path / ".doc-lattice.yml").write_text(
        "docs_roots:\n  - docs\ncache_key: parity\ncache_trust_stat: true\n",
        encoding="utf-8",
    )
    project = load_config(None, tmp_path)

    messages = []
    for _ in range(3):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            load_lattice(project)
        messages.append([str(entry.message) for entry in captured])

    assert messages[0] == messages[1] == messages[2]
    assert any("misplaced doc-lattice envelope" in message for message in messages[0])
```

  Note: the config written here carries no `lattice_format` key, because `Config` is
  `extra="forbid"` and does not have the field until Task 15. Task 15's sweep step adds the key
  to this file along with every other test that writes a config.

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_orchestrate.py -q`
  Expected: all pass. The three runs are the cold run, the verify-tier hit, and the stat-tier hit,
  and their warning lists are compared for equality rather than for a flag.

- [ ] Commit:
  `git commit -am "feat(load): warn on a misplaced envelope and replay it from the cache"`

---

### Task 7: Renderer-parity pins

**Files:**
- Test: `tests/test_markdown_compat.py`, `tests/test_sections.py`

**Interfaces:**
- Consumes: `markdown_it.MarkdownIt("commonmark")`, `extract_headings`, `section_spans`,
  `split_body_lines` from `markdown_compat` and `sections`; `content_hash` from `hashing`.

This task adds no production code. It pins what `markdown-it-py==4.2.0` does with every accepted
envelope byte form, including the BOM decision Task 2 implemented, so a dependency bump that
changes the answer fails here rather than in a user's rendered README.

Steps:

- [ ] Write the failing parity test. Append to `tests/test_markdown_compat.py`:

```python
from markdown_it import MarkdownIt

_RENDER_PARSER = MarkdownIt("commonmark")


@pytest.mark.parametrize(
    ("name", "source", "expected_first_token"),
    [
        ("accepted", "<!-- doc-lattice\nid: a\n-->\n# H\n", "html_block"),
        ("accepted_empty_body", "<!-- doc-lattice\n-->\n# H\n", "html_block"),
        ("refused_bom", "﻿<!-- doc-lattice\nid: a\n-->\n# H\n", "paragraph_open"),
        ("refused_indent_four", "    <!-- doc-lattice\nid: a\n-->\n# H\n", "code_block"),
    ],
)
def test_every_envelope_byte_form_renders_as_its_pinned_block(
    name: str, source: str, expected_first_token: str
):
    tokens = _RENDER_PARSER.parse(source)

    assert tokens[0].type == expected_first_token, name


def test_the_comment_envelope_never_perturbs_heading_extraction_or_spans():
    body = "# H1\n\n## Two\ntext\n"
    fenced = f"---\nid: a\n---\n{body}"
    commented = f"<!-- doc-lattice\nid: a\n-->\n{body}"

    from doc_lattice.frontmatter_parser import parse_document

    _fence_meta, fence_body = parse_document(fenced, Path("a.md"))
    _comment_meta, comment_body = parse_document(commented, Path("b.md"))

    assert fence_body == comment_body == body
    assert extract_headings(fence_body) == extract_headings(comment_body)
    assert section_spans(extract_headings(fence_body), len(split_body_lines(fence_body))) == (
        section_spans(extract_headings(comment_body), len(split_body_lines(comment_body)))
    )
```

  Add `from pathlib import Path` if the file does not already import it.

- [ ] Run it and watch it fail or pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q -k envelope_byte_form or perturb`
  Expected: pass on the first run, because Tasks 2 and 5 already made the bodies identical. If the
  BOM row fails, Task 2's refusal is wrong and must be fixed before continuing.

- [ ] Write the failing drift-hash test. Append to `tests/test_sections.py`:

```python
def test_the_drift_hash_of_a_section_is_the_same_under_both_spellings(tmp_path: Path):
    from doc_lattice.frontmatter_parser import parse_document
    from doc_lattice.hashing import content_hash
    from doc_lattice.sections import section_text

    body = "# H1\n\n## Two {#two}\ntext\n"
    _fm, fence_body = parse_document(f"---\nid: a\n---\n{body}", Path("a.md"))
    _cm, comment_body = parse_document(f"<!-- doc-lattice\nid: a\n-->\n{body}", Path("b.md"))

    assert content_hash(section_text(fence_body, (3, 4))) == content_hash(
        section_text(comment_body, (3, 4))
    )
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_sections.py tests/test_markdown_compat.py -q`
  Expected: all pass.

- [ ] Commit:
  `git commit -am "test(markdown): pin renderer parity for every accepted envelope byte form"`

---

### Task 8: Probe-complete collision components

**Files:**
- Modify: `src/doc_lattice/markdown_compat.py`
- Test: `tests/test_markdown_compat.py`

**Interfaces:**
- Produces: `SluggedHeading(text: str, line: int, github_id: str, probes: tuple[str, ...])`.
- Produces: `full_heading_inventory(body: str) -> list[SluggedHeading]`.
- Produces: `collision_components(inventory: list[SluggedHeading])
  -> list[tuple[SluggedHeading, ...]]`.
- Consumes: `github_slug`, `_Slugger`'s pinned document-order collision rule, and the same full
  CommonMark parse `scripts/check_doc_links.py::_heading_texts` reads (a `heading_open` token
  followed by its `inline` token), extended here with `token.map[0] + 1` for the source line.

Addressability is unchanged: `extract_headings` still sees only column-zero ATX headings. Only
*tracing* runs over the full inventory, which is what makes the mixed-form case (setext
`Overview` then `# Overview`) fail closed instead of diverging silently.

Steps:

- [ ] Write the failing inventory test. Append to `tests/test_markdown_compat.py`:

```python
def test_the_full_inventory_sees_every_heading_form_github_assigns_an_id_to():
    body = "Overview\n--------\n\ntext\n\n# Overview\n\n> ## Quoted\n\n- ### Nested\n"

    inventory = full_heading_inventory(body)

    assert [(h.text, h.line, h.github_id) for h in inventory] == [
        ("Overview", 1, "overview"),
        ("Overview", 6, "overview-1"),
        ("Quoted", 8, "quoted"),
        ("Nested", 10, "nested"),
    ]


def test_the_inventory_ids_agree_with_the_shared_slugger():
    body = "# Notes\n\n# Notes\n\n# Notes-1\n"

    inventory = full_heading_inventory(body)

    assert [h.github_id for h in inventory] == github_ids_for_texts(h.text for h in inventory)
```

  Add `full_heading_inventory` to the `doc_lattice.markdown_compat` import list.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q -k full_inventory or inventory_ids`
  Expected: `ImportError: cannot import name 'full_heading_inventory'`.

- [ ] Implement the tracing slugger in `src/doc_lattice/markdown_compat.py`. Add the record
  beside `Heading`:

```python
@dataclass(frozen=True, slots=True)
class SluggedHeading:
    """One heading in the full GitHub inventory, with the ids its allocation examined.

    ``probes`` is every candidate id the document-order deduplicator tried for this heading,
    its base slug first and each dedup suffix after, ending with the id it kept. Ambiguity is
    derived from the probes rather than from bases or final ids, because dedup examines ids it
    never emits, and an id a later heading merely probed is one a rename can hand it.
    """

    text: str
    line: int
    github_id: str
    probes: tuple[str, ...]
```

  Give `_Slugger` a tracing method beside `slug` (leaving `slug` untouched so
  `github_ids_for_texts` keeps its exact behavior):

```python
    def slug_with_probes(self, text: str) -> tuple[str, tuple[str, ...]]:
        """Return the next unique slug for heading content and every candidate it examined."""
        base = github_slug(text)
        result = base
        probes = [base]
        while result in self._seen:
            self._seen[base] += 1
            result = f"{base}-{self._seen[base]}"
            probes.append(result)
        self._seen[result] = 0
        return result, tuple(probes)
```

  Add the inventory builder after `github_heading_ids`:

```python
def full_heading_inventory(body: str) -> list[SluggedHeading]:
    """Return every heading a GitHub render assigns an id to, with its allocation trace.

    Wider than ``extract_headings`` on purpose: this reads the pinned parser's unrestricted
    CommonMark stream, so setext headings, ATX headings indented one to three spaces, and
    headings nested in a list item or a block quote all arrive. That is the inventory GitHub
    allocates ids from, and a collision between a form the engine addresses and one it does not
    is the only way the lattice id and the GitHub fragment can diverge. Addressability itself is
    unchanged; running *allocation* over this inventory is a separate follow-up (GTX-277).

    Args:
        body: Markdown document text.

    Returns:
        One record per heading in document order, ids deduplicated by the pinned
        github-slugger collision rule.

    Raises:
        RuntimeError: If the pinned parser returns a malformed heading token pair.
    """
    normalized = normalize_newlines(body).replace("\0", "�")
    tokens = _PARSER.parse(normalized)
    slugger = _Slugger()
    inventory: list[SluggedHeading] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        content = tokens[index + 1] if index + 1 < len(tokens) else None
        if content is None or content.type != "inline" or token.map is None:
            msg = f"{MARKDOWN_COMPAT_VERSION} returned a malformed heading token pair"
            raise RuntimeError(msg)
        github_id, probes = slugger.slug_with_probes(content.content)
        inventory.append(
            SluggedHeading(
                text=content.content,
                line=token.map[0] + 1,
                github_id=github_id,
                probes=probes,
            )
        )
    return inventory
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q`
  Expected: all pass.

- [ ] Write the failing component tests, including both counterexample sequences the spec names.
  Append to `tests/test_markdown_compat.py`:

```python
def _components(body: str) -> list[list[str]]:
    return [
        [f"{h.text}@{h.line}" for h in component]
        for component in collision_components(full_heading_inventory(body))
    ]


def test_chained_dedup_suffixes_pull_every_shifted_heading_into_one_component():
    body = "# Notes\n\n# Notes\n\n# Notes-1\n\n# Notes-1-1\n"

    assert _components(body) == [["Notes@1", "Notes@3", "Notes-1@5", "Notes-1-1@7"]]


def test_a_heading_a_probe_never_reached_stays_out_of_the_component():
    body = "# Notes\n\n# Other\n\n# Notes\n"

    assert _components(body) == [["Notes@1", "Notes@5"]]


def test_probe_completeness_pulls_in_a_heading_only_a_probe_touches():
    # "Other" renamed to "Notes-1": the third heading's base request is still only `notes`, and
    # its final id shifts from `notes-1` to `notes-2`, so a rule reading requests alone would
    # call this clean while a rename of the middle heading silently rebinds the third.
    body = "# Notes\n\n# Notes-1\n\n# Notes\n"

    assert _components(body) == [["Notes@1", "Notes-1@3", "Notes@5"]]


def test_a_cross_inventory_collision_is_one_component():
    body = "Overview\n--------\n\ntext\n\n# Overview\n"

    assert _components(body) == [["Overview@1", "Overview@6"]]


def test_a_document_with_no_repeated_slug_has_no_components():
    assert _components("# One\n\n# Two\n\n# Three\n") == []
```

  Add `collision_components` to the `doc_lattice.markdown_compat` import list.

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q -k component`
  Expected: `ImportError: cannot import name 'collision_components'`.

- [ ] Implement the component walk in `src/doc_lattice/markdown_compat.py`, after
  `full_heading_inventory`:

```python
def collision_components(inventory: list[SluggedHeading]) -> list[tuple[SluggedHeading, ...]]:
    """Group headings whose ids move together under a reword into collision components.

    During allocation, every candidate id a heading examines (its base slug and each dedup
    suffix tried) links that heading to the id's current holder. The connected components of
    that graph are the collision components, and every generated id in a component is ambiguous:
    rewording any member can hand a member's id to a different heading, which resolves without
    breaking and therefore reads OK or STALE rather than BROKEN.

    Args:
        inventory: Headings in document order, from ``full_heading_inventory``.

    Returns:
        Each component of more than one heading, members in document order, components ordered
        by their first member. A heading in no component is not returned.
    """
    parent = list(range(len(inventory)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    holder: dict[str, int] = {}
    for index, heading in enumerate(inventory):
        for probe in heading.probes:
            if probe in holder:
                union(index, holder[probe])
        holder[heading.github_id] = index

    grouped: dict[int, list[SluggedHeading]] = {}
    for index, heading in enumerate(inventory):
        grouped.setdefault(find(index), []).append(heading)
    return [tuple(members) for _root, members in sorted(grouped.items()) if len(members) > 1]
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_markdown_compat.py -q`
  Expected: all pass, including the pre-existing golden fixture test, since `Heading` was not
  changed and `github_ids_for_texts` still routes through the untouched `_Slugger.slug`.

- [ ] Commit:
  `git commit -am "feat(markdown): derive probe-complete collision components over the full inventory"`

---

### Task 9: Collision provenance, safe heading display, and the cache bump

**Files:**
- Modify: `src/doc_lattice/text_utils.py`, `src/doc_lattice/model.py`,
  `src/doc_lattice/loader.py`, `src/doc_lattice/cache/schema.py`,
  `src/doc_lattice/constants.py`
- Test: `tests/test_text_utils.py`, `tests/test_model.py`, `tests/test_loader.py`,
  `tests/test_cache_schema.py`, `tests/test_cache.py`

**Interfaces:**
- Produces: `safe_heading_label(text: str) -> str` in `text_utils.py`.
- Produces: `CollisionMember(label: str, line: int)` and
  `format_collision(members: tuple[CollisionMember, ...]) -> str` in `model.py`.
- Produces: `SectionRecord` gains `collision: tuple[CollisionMember, ...] | None = None`.
- Produces: `Lattice` gains
  `collisions: Mapping[TargetId, tuple[CollisionMember, ...]] = field(default_factory=dict)`.
- Produces: `CollisionMemberModel(label: str, line: int)` and `SectionRecordModel` gains
  `collision: list[CollisionMemberModel] | None = None` in `cache/schema.py`.
- Produces: `CACHE_VERSION: int = 6` (the single bump in this plan).
- Consumes: `full_heading_inventory`, `collision_components` from Task 8; `strip_control_chars`
  from `text_utils.py`.

Both new dataclass fields and both new model fields are defaulted, so the ten positional
`Lattice({}, {}, {}, {}, {}, {})` constructions in `tests/cli/test_runtime.py` and the three
keyword `SectionRecord(anchor=..., start=..., end=...)` constructions in `tests/test_cache_*.py`
keep working untouched. The cache version bump is what discards pre-v7 entries rather than
reinterpreting them.

Steps:

- [ ] Write the failing display-helper test. Append to `tests/test_text_utils.py`:

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\x1b]0;title\x07Setup", "0;titleSetup"),
        ("\x9bmSetup", "mSetup"),
        ("Setup\x7f", "Setup"),
        ("Se\x00tup", "Setup"),
        ("Ordinary Heading", "Ordinary Heading"),
        ("Ünïcode ok", "Ünïcode ok"),
    ],
)
def test_safe_heading_label_strips_every_control_range(raw: str, expected: str):
    assert safe_heading_label(raw) == expected
```

  Add `safe_heading_label` to the `doc_lattice.text_utils` import list.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_text_utils.py -q -k safe_heading_label`
  Expected: `ImportError: cannot import name 'safe_heading_label'`.

- [ ] Add the helper to `src/doc_lattice/text_utils.py`, after `strip_control_chars`:

```python
def safe_heading_label(text: str) -> str:
    """Return heading text in the one representation every sink that names a heading prints.

    Naming a colliding heading puts raw Markdown body content into terminal, CI-log, DOT, and
    Mermaid sinks for the first time. AD-35's refusal covers frontmatter values only, and a
    heading is not a frontmatter value, so heading text is cleaned rather than refused: refusing
    it would make an unremarkable document unloadable over a character no reader sees. The
    derivation stores the already-cleaned label, so a cached run and an uncached run cannot
    diverge on sanitization. Each sink still applies its own quoting (Rich markup escaping, DOT
    and Mermaid string quoting) on top of this.

    Args:
        text: Raw inline heading source.

    Returns:
        The text with every C0 control, DEL, and C1 control removed.
    """
    return strip_control_chars(text)
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_text_utils.py -q`
  Expected: all pass.

- [ ] Write the failing model test. Append to `tests/test_model.py`:

```python
def test_format_collision_names_every_member_with_its_line():
    members = (CollisionMember(label="Setup", line=3), CollisionMember(label="Setup", line=9))

    assert format_collision(members) == 'ambiguous with "Setup" (line 3), "Setup" (line 9)'


def test_a_section_record_carries_no_collision_by_default():
    assert SectionRecord(anchor="a", start=1, end=2).collision is None
```

  Add `CollisionMember`, `format_collision`, and `SectionRecord` to the `doc_lattice.model`
  import list.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_model.py -q -k collision`
  Expected: `ImportError: cannot import name 'CollisionMember'`.

- [ ] Add the domain types to `src/doc_lattice/model.py`. Change the `dataclasses` import to
  `from dataclasses import dataclass, field`, and insert before `SectionRecord`:

```python
@dataclass(frozen=True, slots=True)
class CollisionMember:
    """One heading in a slug-collision component, ready to name in any sink.

    ``label`` is already sanitized by ``text_utils.safe_heading_label`` at derivation time, so
    the cached and uncached paths cannot disagree about it. ``line`` is the heading's 1-based
    source line in the document body.
    """

    label: str
    line: int


def format_collision(members: tuple[CollisionMember, ...]) -> str:
    """Render a collision component as the one phrase every sink prints.

    Args:
        members: The component's headings in document order.

    Returns:
        A single-line phrase naming each member and its line. The caller applies its own
        quoting; the labels carry no control character, so the phrase is safe to embed.
    """
    listed = ", ".join(f'"{member.label}" (line {member.line})' for member in members)
    return f"ambiguous with {listed}"
```

  Extend `SectionRecord`:

```python
@dataclass(frozen=True, slots=True)
class SectionRecord:
    """One anchored section: its resolved anchor id, inclusive 1-indexed line span, and any
    slug-collision component it belongs to.

    ``collision`` is None for a section whose id is unambiguous, which includes every id set by
    an explicit ``{#anchor}`` marker: being reword-stable is what the marker is for.
    """

    anchor: str
    start: int
    end: int
    collision: tuple[CollisionMember, ...] | None = None
```

  Extend `Lattice` with a final field and one docstring sentence:

```python
    collisions: Mapping[TargetId, tuple[CollisionMember, ...]] = field(default_factory=dict)
```

  Docstring addition: ``collisions`` maps every section TargetId whose id sits in a
  slug-collision component to that component's members, so an edge into one can be classified
  and named without re-deriving anything.

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_model.py -q`
  Expected: all pass.

- [ ] Write the failing loader test. Append to `tests/test_loader.py`:

```python
def test_derive_file_sections_records_collision_provenance():
    body = "# Notes\n\n# Notes\n"

    sections = derive_file_sections(body)

    assert [record.anchor for record in sections.sections] == ["notes", "notes-1"]
    for record in sections.sections:
        assert record.collision == (
            CollisionMember(label="Notes", line=1),
            CollisionMember(label="Notes", line=3),
        )


def test_a_marker_set_id_is_never_ambiguous():
    body = "# Notes {#first}\n\n# Notes\n"

    sections = derive_file_sections(body)

    by_anchor = {record.anchor: record for record in sections.sections}
    assert by_anchor["first"].collision is None
    assert by_anchor["notes-1"].collision is not None


def test_a_cross_inventory_collision_marks_the_addressable_member():
    body = "Overview\n--------\n\ntext\n\n# Overview\n"

    sections = derive_file_sections(body)

    assert [record.anchor for record in sections.sections] == ["overview"]
    assert sections.sections[0].collision == (
        CollisionMember(label="Overview", line=1),
        CollisionMember(label="Overview", line=6),
    )


def test_build_lattice_exposes_collisions_by_target_id():
    docs = [
        ParsedDoc(path=Path("docs/a.md"), meta=NodeMeta(id="a"), body="# Notes\n\n# Notes\n")
    ]

    lattice = build_lattice(docs)

    assert lattice.collisions[TargetId("a", "notes")][0].label == "Notes"
    assert TargetId("a") not in lattice.collisions
```

  Add `CollisionMember` and any missing names to the import block.

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_loader.py -q -k collision or ambiguous or marker_set`
  Expected: `AssertionError: assert None == (CollisionMember(...), ...)`.

- [ ] Implement the derivation in `src/doc_lattice/loader.py`. Extend the imports:

```python
from .markdown_compat import collision_components, full_heading_inventory
from .model import CollisionMember  # add to the existing .model import
from .text_utils import safe_heading_label
```

  Replace the body of `derive_file_sections`:

```python
def derive_file_sections(body: str) -> FileSections:
    """Derive a document's total line count, anchored section spans, and collision provenance.

    This is the single derivation the load cache stores and replays: the TOC, its de-duped
    anchor ids, each heading's inclusive line span, and, for a heading whose id sits in a
    slug-collision component, the component's members as safe display labels. AD-12 requires a
    cache hit to match the uncached result exactly, and a diagnostic naming colliding headings
    cannot be reconstructed from a boolean, so the members are derived here and persisted.

    Args:
        body: The verbatim document body after the frontmatter envelope.

    Returns:
        A FileSections with the 1-based total line count and one SectionRecord per heading, in
        document order.
    """
    total_lines = _line_count(body)
    toc = build_toc(body)
    anchors = anchor_ids(toc)
    spans = section_spans(toc, total_lines)
    members_by_line = _collision_members_by_line(body)
    records: list[SectionRecord] = []
    for heading, anchor, (start_line, end_line) in zip(toc, anchors, spans, strict=True):
        # A marker-set id is reword-stable by construction, so it is never ambiguous.
        collision = None if heading.anchor is not None else members_by_line.get(heading.line)
        records.append(
            SectionRecord(anchor=anchor, start=start_line, end=end_line, collision=collision)
        )
    return FileSections(total_lines=total_lines, sections=tuple(records))


def _collision_members_by_line(body: str) -> dict[int, tuple[CollisionMember, ...]]:
    """Map each colliding heading's source line to its component's safe display members.

    Keyed by line because that is what the two heading inventories share: the full GitHub
    inventory and the addressable ATX subset read the same normalized text, so a heading both
    see occupies the same 1-based line in each.

    Args:
        body: The verbatim document body.

    Returns:
        One entry per heading in a collision component, in the full inventory's terms.
    """
    found: dict[int, tuple[CollisionMember, ...]] = {}
    for component in collision_components(full_heading_inventory(body)):
        members = tuple(
            CollisionMember(label=safe_heading_label(heading.text), line=heading.line)
            for heading in component
        )
        for heading in component:
            found[heading.line] = members
    return found
```

  Extend `build_lattice` to publish the map. Beside the existing `spans` loop, accumulate:

```python
    collisions: dict[TargetId, tuple[CollisionMember, ...]] = {}
```

  and inside the `for record in file_sections.sections:` loop, after `anchored.append(tid)`:

```python
            if record.collision is not None:
                collisions[tid] = record.collision
```

  then add `collisions=collisions,` to the `Lattice(...)` construction.

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_loader.py -q`
  Expected: all pass.

- [ ] Write the failing cache-schema test. Append to `tests/test_cache_schema.py`:

```python
def test_collision_provenance_round_trips_through_the_cache():
    sections = derive_file_sections("# Notes\n\n# Notes\n")
    entry = make_entry(
        b"# Notes\n\n# Notes\n",
        ParsedMeta(meta=NodeMeta.model_validate({"id": "a"}), disposition="tracked"),
        "# Notes\n\n# Notes\n",
        sections,
        _fake_stat(),
        ROOT,
    )

    revived = Entry.model_validate_json(entry.model_dump_json())
    doc = reconstruct_doc(revived, Path("docs/a.md"))

    assert doc is not None
    assert doc.sections is not None
    assert doc.sections.sections == sections.sections


def test_the_cache_version_is_six():
    assert CACHE_VERSION == 6
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_cache_schema.py -q -k collision_provenance or version_is_six`
  Expected: the round-trip loses `collision`, and `CACHE_VERSION == 5`.

- [ ] Bump the version and extend the models. In `src/doc_lattice/constants.py`, change
  `CACHE_VERSION: int = 5` to `CACHE_VERSION: int = 6`. In
  `src/doc_lattice/cache/schema.py`, add the member model and extend the record:

```python
class CollisionMemberModel(BaseModel):
    """The serialized form of one member of a slug-collision component."""

    model_config = ConfigDict(extra="forbid")

    label: str
    line: int


class SectionRecordModel(BaseModel):
    """The serialized form of one anchored section span and its collision provenance.

    ``collision`` is defaulted rather than required, unlike ``Entry.disposition``, because a
    section that is in no component genuinely has none to record and a default of None cannot be
    read as a silent drop. Entries written before the field existed are discarded by the
    ``CACHE_VERSION`` bump that lands with it, never reinterpreted.
    """

    model_config = ConfigDict(extra="forbid")

    anchor: str
    start: int
    end: int
    collision: list[CollisionMemberModel] | None = None
```

  Update `reconstruct_doc`'s `sections` construction:

```python
    sections = FileSections(
        total_lines=node.total_lines,
        sections=tuple(
            SectionRecord(
                anchor=r.anchor,
                start=r.start,
                end=r.end,
                collision=(
                    None
                    if r.collision is None
                    else tuple(CollisionMember(label=m.label, line=m.line) for m in r.collision)
                ),
            )
            for r in node.sections
        ),
    )
```

  and `make_entry`'s `sections` list:

```python
            sections=[
                SectionRecordModel(
                    anchor=r.anchor,
                    start=r.start,
                    end=r.end,
                    collision=(
                        None
                        if r.collision is None
                        else [
                            CollisionMemberModel(label=m.label, line=m.line) for m in r.collision
                        ]
                    ),
                )
                for r in sections.sections
            ],
```

  Add `CollisionMember` to the `..model` import in `cache/schema.py`.

- [ ] Run the cache suites and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_cache_schema.py tests/test_cache_store.py tests/test_cache_state.py tests/test_cache.py tests/test_cache_lookup.py -q`
  Expected: all pass. `tests/test_cache.py:85`'s `assert old_cache.version < CACHE_VERSION` and
  the discard path in `cache/store.py::load` are what drop pre-v7 entries.

- [ ] Commit:
  `git commit -am "feat(cache): persist collision provenance and bump CACHE_VERSION to 6"`

---

### Task 10: The AMBIGUOUS edge state in check

**Files:**
- Modify: `src/doc_lattice/constants.py`, `src/doc_lattice/check.py`,
  `src/doc_lattice/report_render.py`, `src/doc_lattice/cli/commands/check.py`
- Test: `tests/test_check.py`, `tests/test_report_render.py`, `tests/cli/test_check.py`

**Interfaces:**
- Produces: `EdgeState = Literal["OK", "STALE", "UNRECONCILED", "BROKEN", "AMBIGUOUS"]`.
  Appended last so `EDGE_STATES`, which drives the deterministic check-summary breakdown order,
  keeps every existing state in place.
- Produces: `EdgeStatus` gains `collision: tuple[CollisionMember, ...] = ()`.
- Produces: `ambiguous_edges(lattice: Lattice) -> tuple[EdgeStatus, ...]` in `check.py`.
- Produces: `ambiguous_json(statuses: Sequence[EdgeStatus]) -> list[dict]` in `check.py`.
- Consumes: `Lattice.collisions` and `format_collision` from Task 9.

`ambiguous_edges` computes ambiguity from `lattice.collisions` alone and hashes nothing, so
`lint`, `impact`, `graph`, and `linear` can carry the state in Tasks 11 and 12 without paying for
a full `check_lattice` pass.

Steps:

- [ ] Write the failing state-domain and classification tests. Append to `tests/test_check.py`:

```python
def test_the_edge_state_domain_ends_with_ambiguous():
    assert EDGE_STATES == ("OK", "STALE", "UNRECONCILED", "BROKEN", "AMBIGUOUS")


def _ambiguous_lattice() -> Lattice:
    return build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Notes\n\n# Notes\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#notes", "seen": "a" * 32}]}
                ),
                body="# Down\n",
            ),
        ]
    )


def test_an_edge_into_a_collision_component_is_ambiguous():
    statuses = check_lattice(_ambiguous_lattice())

    assert [status.state for status in statuses] == ["AMBIGUOUS"]
    assert statuses[0].actual is None
    assert statuses[0].expected == "a" * 32
    assert [member.label for member in statuses[0].collision] == ["Notes", "Notes"]


def test_check_reports_drift_on_an_ambiguous_edge():
    assert has_drift(check_lattice(_ambiguous_lattice())) is True


def test_ambiguous_edges_finds_the_same_rows_without_hashing():
    lattice = _ambiguous_lattice()

    assert ambiguous_edges(lattice) == tuple(
        status for status in check_lattice(lattice) if status.state == "AMBIGUOUS"
    )


def test_the_check_json_payload_carries_the_collision():
    statuses = check_lattice(_ambiguous_lattice())

    payload = statuses_json(statuses, summarize_statuses(statuses))

    assert payload["edges"][0]["collision"] == [
        {"label": "Notes", "line": 1},
        {"label": "Notes", "line": 3},
    ]
    assert payload["summary"]["AMBIGUOUS"] == 1
```

  Add `EDGE_STATES`, `ambiguous_edges`, `build_lattice`, `ParsedDoc`, `NodeMeta`, `Lattice`, and
  `Path` to the import block as needed.

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_check.py -q -k ambiguous`
  Expected: `AssertionError` on the state tuple, then `ImportError` for `ambiguous_edges`.

- [ ] Add the state to `src/doc_lattice/constants.py`:

```python
# "AMBIGUOUS" is last so the ordered form keeps every earlier state in its published position.
# It is a first-class state, not an advisory: the same condition gets the same verdict in every
# command, and a warning-only design would leave CI green on a lattice reconcile itself refuses.
EdgeState = Literal["OK", "STALE", "UNRECONCILED", "BROKEN", "AMBIGUOUS"]
```

- [ ] Implement the classification in `src/doc_lattice/check.py`. Extend the imports with
  `from collections.abc import Mapping, Sequence` and
  `from .model import CollisionMember, Edge, Lattice, TargetId, format_collision`, extend
  `EdgeStatus`:

```python
@dataclass(frozen=True, slots=True)
class EdgeStatus:
    """The classification of one edge.

    ``collision`` is empty except on an ``AMBIGUOUS`` edge, where it names the headings whose
    ids move together, already sanitized for display.
    """

    source_id: str
    target_ref: str
    target_id: TargetId | None
    state: EdgeState
    expected: str | None
    actual: str | None
    collision: tuple[CollisionMember, ...] = ()
```

  add the shared builder and the two consumers:

```python
def _ambiguous(
    source_id: str, edge: Edge, collision: tuple[CollisionMember, ...]
) -> EdgeStatus:
    """Build the one AMBIGUOUS record shape every command reads.

    ``actual`` is None rather than the live hash: naming a hash for a target the tool refuses to
    identify would read as a drift comparison that was actually made.
    """
    return EdgeStatus(
        source_id, edge.target_ref, edge.target_id, "AMBIGUOUS", edge.seen, None, collision
    )


def ambiguous_edges(lattice: Lattice) -> tuple[EdgeStatus, ...]:
    """Return one AMBIGUOUS record per edge whose resolved target sits in a collision component.

    Hashes nothing, so a command that only needs the ambiguity findings does not pay for a full
    drift classification to get them. ``check_lattice`` produces byte-identical records for the
    same edges.

    Args:
        lattice: The built lattice.

    Returns:
        The ambiguous edges in node-id then edge order.
    """
    found: list[EdgeStatus] = []
    for node_id in sorted(lattice.nodes_by_id):
        for edge in lattice.nodes_by_id[node_id].derives_from:
            if edge.target_id is None:
                continue
            collision = lattice.collisions.get(edge.target_id)
            if collision is not None:
                found.append(_ambiguous(node_id, edge, collision))
    return tuple(found)


def ambiguous_json(statuses: Sequence[EdgeStatus]) -> list[dict]:
    """Build the shared ``ambiguous`` payload block impact, graph, lint, and linear all emit.

    Args:
        statuses: Edge classifications; only ``AMBIGUOUS`` members are serialized.

    Returns:
        One entry per ambiguous edge, naming the colliding headings and their lines.
    """
    return [
        {
            "source_id": status.source_id,
            "target_ref": status.target_ref,
            "target_id": status.target_id.as_ref() if status.target_id else None,
            "collision": [
                {"label": member.label, "line": member.line} for member in status.collision
            ],
        }
        for status in statuses
        if status.state == "AMBIGUOUS"
    ]
```

  extend `_classify` immediately after the BROKEN branch:

```python
    collision = lattice.collisions.get(edge.target_id)
    if collision is not None:
        return _ambiguous(source_id, edge, collision)
```

  and add the per-edge key to `statuses_json`'s edge payload:

```python
                "collision": [
                    {"label": member.label, "line": member.line} for member in status.collision
                ],
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_check.py -q`
  Expected: all pass.

- [ ] Write the failing renderer test. Append to `tests/test_report_render.py`:

```python
def test_an_ambiguous_row_names_the_colliding_headings():
    console = Console(file=StringIO(), no_color=True, width=200)
    status = EdgeStatus(
        "down",
        "up#notes",
        TargetId("up", "notes"),
        "AMBIGUOUS",
        None,
        None,
        (CollisionMember(label="Notes", line=1), CollisionMember(label="Notes", line=3)),
    )

    render_statuses(console, [status], summarize_statuses([status]))

    output = console.file.getvalue()
    assert "AMBIGUOUS" in output
    assert 'ambiguous with "Notes" (line 1), "Notes" (line 3)' in output
    assert output.rstrip().endswith("1 edge: 0 OK, 0 STALE, 0 UNRECONCILED, 0 BROKEN, 1 AMBIGUOUS")


def test_rich_markup_in_a_heading_label_is_escaped_not_interpreted():
    console = Console(file=StringIO(), no_color=True, width=200)
    status = EdgeStatus(
        "down",
        "up#x",
        TargetId("up", "x"),
        "AMBIGUOUS",
        None,
        None,
        (CollisionMember(label="[bold]hi[/bold]", line=2),),
    )

    render_statuses(console, [status], summarize_statuses([status]))

    assert "[bold]hi[/bold]" in console.file.getvalue()
```

  and extend the pre-existing `test_state_colors_cover_every_edge_state` expectation set if it
  spells the states out.

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_report_render.py -q`
  Expected: `KeyError: 'AMBIGUOUS'` from `_STATE_COLORS`.

- [ ] Update `src/doc_lattice/report_render.py`. Add `"AMBIGUOUS": "red"` to `_STATE_COLORS`,
  import `format_collision` from `.model`, and extend the row print in `render_statuses`:

```python
    for status in statuses:
        color = _STATE_COLORS[status.state]
        detail = f" ({escape(format_collision(status.collision))})" if status.collision else ""
        console.print(
            f"[{color}]{status.state:<{_STATE_COL_WIDTH}}[/{color}] "
            f"{escape(status.source_id)} -> {escape(status.target_ref)}{detail}",
            highlight=False,
            soft_wrap=True,
        )
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_report_render.py -q`
  Expected: all pass.

- [ ] Update the `--only` help text in `src/doc_lattice/cli/commands/check.py` to read
  `"Show only these states (repeatable): OK, STALE, UNRECONCILED, BROKEN, AMBIGUOUS. "`.

- [ ] Write the failing CLI exit-code and contract test. Append to `tests/cli/test_check.py`:

```python
def test_check_exits_one_on_an_ambiguous_edge_and_names_it_in_both_formats(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Notes\n\n# Notes\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#notes\n---\n# Down\n", encoding="utf-8"
    )
    # No `lattice_format` key: Config does not have the field until Task 15, whose sweep adds it.
    config = tmp_path / ".doc-lattice.yml"
    config.write_text("docs_roots:\n  - docs\n", encoding="utf-8")

    human = runner.invoke(app, ["check", "--config", str(config)])
    payload = runner.invoke(app, ["check", "--config", str(config), "--format", "json"])

    assert human.exit_code == 1
    assert payload.exit_code == 1
    assert 'ambiguous with "Notes" (line 1), "Notes" (line 3)' in human.stdout
    edge = json.loads(payload.stdout)["edges"][0]
    assert edge["state"] == "AMBIGUOUS"
    assert edge["collision"] == [
        {"label": "Notes", "line": 1},
        {"label": "Notes", "line": 3},
    ]
    assert json.loads(payload.stdout)["summary"]["AMBIGUOUS"] == 1
```

- [ ] Run the CLI check suite and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/cli/test_check.py tests/test_check.py tests/test_report_render.py -q`
  Expected: all pass; update any pre-existing summary-line assertion that spells the four old
  states to include `, 0 AMBIGUOUS`.

- [ ] Commit:
  `git commit -am "feat(check): add the AMBIGUOUS edge state and name its colliding headings"`

---

### Task 11: AMBIGUOUS in lint

**Files:**
- Modify: `src/doc_lattice/lint.py`, `src/doc_lattice/report_render.py`
- Test: `tests/test_lint.py`, `tests/cli/test_lint.py`

**Interfaces:**
- Produces: `LintResult` gains `ambiguous: tuple[EdgeStatus, ...] = ()`.
- Produces: `render_ambiguous(console: Console, statuses: Sequence[EdgeStatus]) -> None` in
  `report_render.py`, the one human renderer `lint`, `impact`, and `linear` share.
- Consumes: `ambiguous_edges`, `ambiguous_json`, `EdgeStatus` from Task 10.

Steps:

- [ ] Write the failing test. Append to `tests/test_lint.py`:

```python
def _ambiguous_lattice() -> Lattice:
    return build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Notes\n\n# Notes\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#notes"}]}
                ),
                body="# Down\n",
            ),
        ]
    )


def test_lint_reports_an_ambiguous_edge():
    result = lint_lattice(_ambiguous_lattice())

    assert [status.target_ref for status in result.ambiguous] == ["up#notes"]


def test_the_lint_json_payload_carries_the_ambiguous_block():
    payload = lint_json(lint_lattice(_ambiguous_lattice()))

    assert payload["ambiguous"] == [
        {
            "source_id": "down",
            "target_ref": "up#notes",
            "target_id": "up#notes",
            "collision": [{"label": "Notes", "line": 1}, {"label": "Notes", "line": 3}],
        }
    ]


def test_a_clean_lattice_reports_an_empty_ambiguous_block():
    lattice = build_lattice(
        [ParsedDoc(path=Path("docs/a.md"), meta=NodeMeta(id="a"), body="# One\n")]
    )

    assert lint_json(lint_lattice(lattice))["ambiguous"] == []
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_lint.py -q -k ambiguous`
  Expected: `AttributeError: 'LintResult' object has no attribute 'ambiguous'`.

- [ ] Extend `src/doc_lattice/lint.py`. Add
  `from .check import EdgeStatus, ambiguous_edges, ambiguous_json`, extend the result:

```python
@dataclass(frozen=True, slots=True)
class LintResult:
    """Violations that fail the gate, the unjudged skips, and the ambiguous edges.

    ``ambiguous`` is defaulted so a caller constructing a result by hand keeps working; every
    production construction comes from ``lint_lattice``, which fills it.
    """

    violations: tuple[LadderViolation, ...]
    skipped: tuple[SkippedEdge, ...]
    ambiguous: tuple[EdgeStatus, ...] = ()
```

  add `"ambiguous": ambiguous_json(result.ambiguous),` to `lint_json`'s returned mapping, and
  fill the field in `lint_lattice`'s return:

```python
    return LintResult(
        violations=tuple(violations),
        skipped=tuple(skipped),
        ambiguous=ambiguous_edges(lattice),
    )
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_lint.py -q`
  Expected: all pass.

- [ ] Add the shared human renderer to `src/doc_lattice/report_render.py`, after `render_lint`:

```python
def render_ambiguous(console: Console, statuses: Sequence[EdgeStatus]) -> None:
    """Render ambiguous-target findings, one record per line.

    The one human spelling ``lint``, ``impact``, and ``linear`` share, so the same condition
    reads the same way wherever it is reported. ``check`` renders its own row instead, because
    the state is part of that command's per-edge listing rather than an appended block.

    Args:
        console: Destination console.
        statuses: Edge classifications; only ``AMBIGUOUS`` members are printed.
    """
    # highlight=False and soft_wrap for the reason every renderer in this module carries them:
    # Rich's highlighter bolds bare numbers and bold survives no_color, and each record must
    # stay one line at any width so a pipe or a grep gets the whole record.
    for status in statuses:
        if status.state != "AMBIGUOUS":
            continue
        console.print(
            f"[red]AMBIGUOUS[/red]  {escape(status.source_id)} -> "
            f"{escape(status.target_ref)} ({escape(format_collision(status.collision))})",
            highlight=False,
            soft_wrap=True,
        )
```

  Add `from collections.abc import Mapping, Sequence` to the imports.

- [ ] Call it from the lint adapter. In `src/doc_lattice/cli/commands/lint.py`, in the human
  branch, replace `render_lint(runtime.stdout, result)` with:

```python
            render_ambiguous(runtime.stdout, result.ambiguous)
            render_lint(runtime.stdout, result)
```

  and add `render_ambiguous` to the `...report_render` import.

- [ ] Write the failing CLI test. Append to `tests/cli/test_lint.py`:

```python
def test_lint_names_an_ambiguous_target_in_human_and_json(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Notes\n\n# Notes\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#notes\n---\n# Down\n", encoding="utf-8"
    )
    # No `lattice_format` key: `Config` is extra="forbid" and does not have the field until
    # Task 15, whose sweep step adds it here.
    (tmp_path / ".doc-lattice.yml").write_text("docs_roots:\n  - docs\n", encoding="utf-8")

    human = runner.invoke(app, ["lint", "--config", str(tmp_path / ".doc-lattice.yml")])
    payload = runner.invoke(
        app, ["lint", "--config", str(tmp_path / ".doc-lattice.yml"), "--format", "json"]
    )

    assert 'ambiguous with "Notes" (line 1), "Notes" (line 3)' in human.stdout
    assert json.loads(payload.stdout)["ambiguous"][0]["target_ref"] == "up#notes"
```

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/cli/test_lint.py tests/test_lint.py -q`
  Expected: all pass.

- [ ] Commit:
  `git commit -am "feat(lint): report ambiguous targets in human and JSON output"`

---

### Task 12: AMBIGUOUS in impact, graph, and linear

**Files:**
- Modify: `src/doc_lattice/impact.py`, `src/doc_lattice/render.py`,
  `src/doc_lattice/linear_render.py`, `src/doc_lattice/cli/commands/impact.py`,
  `src/doc_lattice/cli/commands/graph.py`, `src/doc_lattice/cli/commands/linear.py`
- Test: `tests/test_impact.py`, `tests/test_render.py`, `tests/test_linear_render.py`,
  `tests/cli/test_impact.py`, `tests/cli/test_graph.py`, `tests/cli/test_linear.py`

**Interfaces:**
- Produces: `impact_json(affected: list[tuple[Node, int]],
  ambiguous: Sequence[EdgeStatus] = ()) -> dict`.
- Produces: `render_impact(console: Console, affected: list[tuple[Node, int]],
  ambiguous: Sequence[EdgeStatus] = ()) -> None`.
- Produces: `to_mermaid` / `to_dot` / `to_json` each take
  `ambiguous_edges: set[tuple[str, TargetId]]` as a third positional parameter defaulting to an
  empty frozenset, and `_graph_edges` returns
  `list[tuple[str, str, bool, bool]]` (upstream, downstream, is_stale, is_ambiguous).
- Produces: `findings_json(findings: Sequence[Finding],
  ambiguous: Sequence[EdgeStatus] = ()) -> dict` and
  `render_findings(console: Console, findings: Sequence[Finding],
  ambiguous: Sequence[EdgeStatus] = ()) -> None`.
- Consumes: `ambiguous_edges`, `ambiguous_json`, `EdgeStatus` from Task 10;
  `render_ambiguous` from Task 11; `Lattice.collisions` and `format_collision` from Task 9.

Steps:

- [ ] Write the failing impact test. Append to `tests/test_impact.py`:

```python
def test_impact_json_carries_the_ambiguous_block():
    lattice = build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Notes\n\n# Notes\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#notes"}]}
                ),
                body="# Down\n",
            ),
        ]
    )

    payload = impact_json(impact(lattice, "up"), ambiguous_edges(lattice))

    assert [entry["id"] for entry in payload["affected"]] == ["down"]
    assert payload["ambiguous"][0]["collision"] == [
        {"label": "Notes", "line": 1},
        {"label": "Notes", "line": 3},
    ]


def test_impact_json_reports_an_empty_ambiguous_block_by_default():
    lattice = build_lattice(
        [ParsedDoc(path=Path("docs/a.md"), meta=NodeMeta(id="a"), body="# One\n")]
    )

    assert impact_json(impact(lattice, "a"))["ambiguous"] == []
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_impact.py -q -k ambiguous`
  Expected: `TypeError: impact_json() takes 1 positional argument but 2 were given`.

- [ ] Extend `src/doc_lattice/impact.py`:

```python
def impact_json(
    affected: list[tuple[Node, int]], ambiguous: Sequence[EdgeStatus] = ()
) -> dict:
    """Build the JSON-ready impact report payload.

    Args:
        affected: Affected nodes paired with their minimum impact depths.
        ambiguous: Ambiguous edges in the same lattice, from ``check.ambiguous_edges``. The
            block is always present, empty when there are none, so a consumer never has to
            distinguish "absent" from "none found".

    Returns:
        A plain dictionary containing the ordered affected-node payloads and the shared
        ambiguous block.
    """
    return {
        "affected": [
            {
                "id": node.id,
                "title": node.title,
                "path": str(node.path),
                "tickets": list(node.tickets),
                "depth": node_depth,
            }
            for node, node_depth in affected
        ],
        "ambiguous": ambiguous_json(ambiguous),
    }
```

  Add `from collections.abc import Sequence` and
  `from .check import EdgeStatus, ambiguous_json`.

  Extend `render_impact` in `src/doc_lattice/report_render.py` to take
  `ambiguous: Sequence[EdgeStatus] = ()` and call `render_ambiguous(console, ambiguous)` before
  its node loop, documenting that ambiguity is printed first because it is a finding while the
  node list is informational.

  Wire the adapter in `src/doc_lattice/cli/commands/impact.py`: compute
  `ambiguous = ambiguous_edges(lattice)` inside the `exit_on_project_error` block and pass it to
  both `impact_json(affected, ambiguous)` and `render_impact(runtime.stdout, affected, ambiguous)`.

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_impact.py tests/cli/test_impact.py -q`
  Expected: all pass.

- [ ] Write the failing graph test. Append to `tests/test_render.py`:

```python
def _ambiguous_lattice() -> Lattice:
    return build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Notes\n\n# Notes\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#notes"}]}
                ),
                body="# Down\n",
            ),
        ]
    )


def test_graph_json_marks_the_edge_and_names_the_colliding_headings():
    lattice = _ambiguous_lattice()
    ambiguous = {(status.source_id, status.target_id) for status in ambiguous_edges(lattice)}

    payload = to_json(lattice, set(), ambiguous)

    assert payload["edges"][0]["ambiguous"] is True
    assert payload["ambiguous_targets"] == [
        {
            "target_id": "up#notes",
            "members": [{"label": "Notes", "line": 1}, {"label": "Notes", "line": 3}],
        }
    ]


def test_dot_and_mermaid_mark_the_edge_and_comment_the_component():
    lattice = _ambiguous_lattice()
    ambiguous = {(status.source_id, status.target_id) for status in ambiguous_edges(lattice)}

    dot = to_dot(lattice, set(), ambiguous)
    mermaid = to_mermaid(lattice, set(), ambiguous)

    assert 'style=dotted color="red"' in dot
    assert '// ambiguous up#notes: "Notes" (line 1), "Notes" (line 3)' in dot
    assert "-. ambiguous .->" in mermaid
    assert '%% ambiguous up#notes: "Notes" (line 1), "Notes" (line 3)' in mermaid
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_render.py -q -k ambiguous`
  Expected: `TypeError: to_json() takes 2 positional arguments but 3 were given`.

- [ ] Extend `src/doc_lattice/render.py`. Change `_graph_edges` to accept
  `ambiguous_edges: set[tuple[str, TargetId]]` and collapse a fourth flag alongside `is_stale`,
  then add the shared naming block and pass it through all three renderers:

```python
def _ambiguous_lines(lattice: Lattice, prefix: str) -> list[str]:
    """Render one comment line per ambiguous target, naming its colliding headings.

    The same facts every format carries, spelled as a comment in DOT and Mermaid because neither
    has a place for a finding that is not an edge. The labels are already control-free, so a
    comment cannot be broken out of.

    Args:
        lattice: The built lattice.
        prefix: The destination format's line-comment marker.

    Returns:
        One line per ambiguous target, ordered by target ref.
    """
    return [
        f"{prefix} ambiguous {target_id.as_ref()}: "
        + ", ".join(f'"{member.label}" (line {member.line})' for member in members)
        for target_id, members in sorted(
            lattice.collisions.items(), key=lambda item: item[0].as_ref()
        )
    ]
```

  In `to_dot`, insert `lines.extend(_ambiguous_lines(lattice, "   //"))` after the opening
  `digraph lattice {` line, and give an ambiguous edge
  `' [style=dotted color="red"]'` (an edge that is both stale and ambiguous takes the ambiguous
  style, since ambiguity is the stronger finding). In `to_mermaid`, insert
  `lines.extend(_ambiguous_lines(lattice, "    %%"))` after `graph TD`, and use
  `-. ambiguous .->` for an ambiguous edge. In `to_json`, add `"ambiguous": is_ambiguous` to each
  edge entry and a top-level:

```python
        "ambiguous_targets": [
            {
                "target_id": target_id.as_ref(),
                "members": [{"label": m.label, "line": m.line} for m in members],
            }
            for target_id, members in sorted(
                lattice.collisions.items(), key=lambda item: item[0].as_ref()
            )
        ],
```

  Wire the adapter in `src/doc_lattice/cli/commands/graph.py`: build
  `ambiguous = {(status.source_id, status.target_id) for status in ambiguous_edges(lattice)
  if status.target_id is not None}` beside the existing `stale` set and pass it as the third
  argument to all three renderers.

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_render.py tests/cli/test_graph.py -q`
  Expected: all pass.

- [ ] Write the failing linear test. Append to `tests/test_linear_render.py`:

```python
def test_findings_json_carries_the_ambiguous_block():
    status = EdgeStatus(
        "down",
        "up#notes",
        TargetId("up", "notes"),
        "AMBIGUOUS",
        None,
        None,
        (CollisionMember(label="Notes", line=1), CollisionMember(label="Notes", line=3)),
    )

    payload = findings_json([], [status])

    assert payload["findings"] == []
    assert payload["ambiguous"][0]["target_ref"] == "up#notes"


def test_render_findings_prints_ambiguity_before_the_all_clear_line():
    console = Console(file=StringIO(), no_color=True, width=200)
    status = EdgeStatus(
        "down", "up#n", TargetId("up", "n"), "AMBIGUOUS", None, None,
        (CollisionMember(label="N", line=2),),
    )

    render_findings(console, [], [status])

    output = console.file.getvalue()
    assert "AMBIGUOUS" in output
    assert output.index("AMBIGUOUS") < output.index("no stale-shipped findings")
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_linear_render.py -q -k ambiguous`
  Expected: `TypeError: findings_json() takes 1 positional argument but 2 were given`.

- [ ] Extend `src/doc_lattice/linear_render.py`: give both functions the
  `ambiguous: Sequence[EdgeStatus] = ()` parameter, add `"ambiguous": ambiguous_json(ambiguous)`
  to the payload, and call `render_ambiguous(console, ambiguous)` as the first statement of
  `render_findings` (before the empty-findings early return, so an ambiguous lattice with no
  findings still reports it). Import `ambiguous_json` and `EdgeStatus` from `.check` and
  `render_ambiguous` from `.report_render`. Wire the adapter in
  `src/doc_lattice/cli/commands/linear.py`: compute `ambiguous = ambiguous_edges(lattice)` inside
  the `exit_on_project_error` block and pass it to both output branches.

- [ ] Write the failing safe-display regression tests, one per naming sink. Append to
  `tests/test_render.py`:

```python
def _hostile_lattice() -> Lattice:
    # One heading text carrying an OSC introducer, a CSI introducer, a DEL, a DOT-hostile
    # backslash and quote, and a Mermaid-hostile quote. The label is sanitized at derivation
    # time, so every sink names the same cleaned text and then applies its own quoting.
    body = '# A\x1b]0;x\x07"b\\c\x9bm\x7f\n\n# A\x1b]0;x\x07"b\\c\x9bm\x7f\n'
    return build_lattice(
        [ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body=body)]
    )


def test_no_sink_that_names_a_heading_emits_a_control_character():
    lattice = _hostile_lattice()

    dot = to_dot(lattice, set(), set())
    mermaid = to_mermaid(lattice, set(), set())
    payload = json.dumps(to_json(lattice, set(), set()))

    for rendered in (dot, mermaid, payload):
        assert "\x1b" not in rendered
        assert "\x9b" not in rendered
        assert "\x7f" not in rendered
        assert "\x07" not in rendered


def test_a_persisted_label_is_already_sanitized_so_both_paths_agree():
    lattice = _hostile_lattice()

    members = next(iter(lattice.collisions.values()))

    assert members[0].label == 'A]0;x"b\\cm'
    assert members[0].label == members[1].label
```

  Add `json` to the test module imports.

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_render.py -q -k control_character or sanitized`
  Expected: pass. `safe_heading_label` ran at derivation time (Task 9), so no sink has to strip
  anything; each only applies its own quoting (`_dot_escape` doubles the backslash and escapes
  the quote, `_mermaid_escape` replaces the quote with an apostrophe, and `json.dumps` escapes
  what it must). If the expected label literal in the second test is wrong, print
  `next(iter(lattice.collisions.values()))[0].label` and paste it verbatim rather than loosening
  the assertion.

- [ ] Write the failing cross-tier output-parity test, which AD-12 requires and which flag
  equality would not catch. Append to `tests/cli/test_contract.py`:

```python
def test_ambiguous_output_is_byte_identical_across_every_cache_tier(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "up.md").write_text("---\nid: up\n---\n# Notes\n\n# Notes\n", encoding="utf-8")
    (docs / "down.md").write_text(
        "---\nid: down\nderives_from:\n  - ref: up#notes\n---\n# Down\n", encoding="utf-8"
    )
    # No `lattice_format` key: Config does not have the field until Task 15, whose sweep adds it.
    config = tmp_path / ".doc-lattice.yml"
    config.write_text(
        "docs_roots:\n  - docs\ncache_key: tiers\ncache_trust_stat: true\n", encoding="utf-8"
    )

    for argv in (
        ["check", "--config", str(config)],
        ["check", "--config", str(config), "--format", "json"],
        ["lint", "--config", str(config), "--format", "json"],
        ["impact", "up", "--config", str(config), "--format", "json"],
        ["graph", "--config", str(config), "--format", "json"],
    ):
        runs = [runner.invoke(app, argv) for _ in range(3)]
        assert runs[0].stdout == runs[1].stdout == runs[2].stdout, argv
        assert 'Notes' in runs[2].stdout, argv
```

- [ ] Run the whole affected set and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_linear_render.py tests/cli/test_linear.py tests/test_render.py tests/test_impact.py tests/cli/test_contract.py -q`
  Expected: all pass. The three runs per command are the cold run, the verify-tier hit, and the
  stat-tier hit, and their stdout is compared byte-for-byte rather than by a flag, which is what
  the persisted sanitized labels make possible.

- [ ] Commit:
  `git commit -am "feat(cli): carry AMBIGUOUS through impact, graph, and linear output"`

---

### Task 13: Context-inclusive target hash

**Files:**
- Modify: `src/doc_lattice/resolve.py`
- Test: `tests/test_resolve.py`, `tests/test_check.py`

**Interfaces:**
- Produces: `ancestor_headings(lattice: Lattice, target_id: TargetId) -> tuple[str, ...]` in
  `resolve.py`.
- Consumes: `Lattice.ancestors`, `Lattice.index`, `sections.split_body_lines`,
  `markdown_compat.strip_heading_anchor`, `sections.section_text`.
- Unchanged: `cached_target_hash(lattice, target_id, cache)` and `content_hash`.

Design note, and the one deliberate deviation from the spec's literal wording. The spec says the
prefix is "the ancestor heading chain (raw inline heading source, the same source the slugger
reads)". Reaching `Heading.text` for an ancestor means either re-parsing the document per hash or
adding heading text to the cached `SectionRecord`. This plan instead takes each ancestor's
**heading source line** with its `{#anchor}` marker removed, which is exactly the treatment
`section_text` already gives the section's own heading line. The hash input then reads as the
document with the intervening content removed, which is easier to reason about, needs no cache
field, and satisfies every behavior the spec requires: rewording an ancestor restales, and moving
a section under a different parent restales. The one behavioral difference is that changing an
ancestor's heading *level* also restales, which is a restructure of the document and correct to
treat as a change.

Steps:

- [ ] Write the failing tests. Append to `tests/test_resolve.py`:

```python
def _two_products() -> Lattice:
    body = (
        "# Products\n\n"
        "## Product A\n\n"
        "### Setup\nrun the installer\n\n"
        "## Product B\n\n"
        "### Setup\nrun the installer\n"
    )
    return build_lattice(
        [ParsedDoc(path=Path("docs/p.md"), meta=NodeMeta(id="p"), body=body)]
    )


def test_identical_sections_under_different_parents_hash_differently():
    lattice = _two_products()
    cache: dict[TargetId, str] = {}

    first = cached_target_hash(lattice, TargetId("p", "setup"), cache)
    second = cached_target_hash(lattice, TargetId("p", "setup-1"), cache)

    assert first != second


def test_the_ancestor_chain_is_the_heading_lines_outermost_first():
    lattice = _two_products()

    assert ancestor_headings(lattice, TargetId("p", "setup")) == ("# Products", "## Product A")


def test_a_top_level_section_hashes_exactly_its_own_text():
    lattice = build_lattice(
        [ParsedDoc(path=Path("docs/a.md"), meta=NodeMeta(id="a"), body="# Only\nbody\n")]
    )

    assert target_content(lattice, TargetId("a", "only")) == section_text(
        lattice.nodes_by_id["a"].body, (1, 2)
    )


def test_a_whole_file_target_is_unaffected_by_context():
    lattice = _two_products()

    assert target_content(lattice, TargetId("p")) == lattice.nodes_by_id["p"].body


def test_a_marker_removed_from_an_ancestor_heading_does_not_change_the_chain():
    body = "## Parent {#parent}\n\n### Child\nbody\n"
    lattice = build_lattice(
        [ParsedDoc(path=Path("docs/a.md"), meta=NodeMeta(id="a"), body=body)]
    )

    assert ancestor_headings(lattice, TargetId("a", "child")) == ("## Parent",)
```

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_resolve.py -q -k ancestor or different_parents`
  Expected: `ImportError: cannot import name 'ancestor_headings'`, and the two-products hashes
  currently compare equal.

- [ ] Implement it in `src/doc_lattice/resolve.py`. Extend the imports with
  `from .markdown_compat import strip_heading_anchor` and
  `from .sections import section_text, split_body_lines`, then replace `target_content` and add
  the helper:

```python
def target_content(lattice: Lattice, target_id: TargetId) -> str:
    """Return the content a target id covers, for hashing.

    A section target's content is prefixed with its ancestor heading chain, so context is part
    of target identity. Two byte-identical sections under different parents (a templated
    ``### Setup`` under ``## Product A`` and ``## Product B``) no longer hash the same, which is
    what closes the transient-collision hole: adding B's ``Setup`` and renaming A's in one
    change would otherwise transfer ``#setup`` between products with no run ever seeing a
    collision and the old ``seen`` still matching. A section that moves under a different
    parent, or whose ancestor is reworded, therefore goes STALE even when its own bytes did not
    change, which is correct: the context is part of what the downstream document derived from.
    Whole-file targets are unaffected.

    Args:
        lattice: The built lattice.
        target_id: A resolved TargetId present in ``lattice.index``.

    Returns:
        The whole node body for a ``file`` location, or the ancestor heading chain followed by
        the anchored section text for a ``section`` location.

    Raises:
        BrokenRefError: If ``target_id`` is not in the index.
    """
    location = lattice.index.get(target_id)
    if location is None:
        msg = f"ref resolves to unknown id {target_id.as_ref()!r}; fix the ref or add the anchor"
        raise BrokenRefError(msg)
    node = node_for_path(lattice, location.path)
    if location.kind == "file":
        return node.body
    section = section_text(node.body, location.span)
    chain = ancestor_headings(lattice, target_id)
    return "\n".join([*chain, section]) if chain else section


def ancestor_headings(lattice: Lattice, target_id: TargetId) -> tuple[str, ...]:
    """Return each enclosing section's heading line, outermost first.

    The marker is removed with ``strip_heading_anchor``, which is the same treatment
    ``sections.section_text`` gives a section's own heading line, so adding or removing a
    ``{#anchor}`` on an ancestor does not restale every descendant edge.

    Args:
        lattice: The built lattice.
        target_id: A resolved section TargetId present in ``lattice.index``.

    Returns:
        The ancestors' heading source lines, outermost first, empty for a top-level section.
    """
    ancestors = lattice.ancestors.get(target_id, ())
    if not ancestors:
        return ()
    lines = split_body_lines(node_for_path(lattice, lattice.index[target_id].path).body)
    return tuple(
        strip_heading_anchor(lines[lattice.index[ancestor].span[0] - 1])
        for ancestor in ancestors
    )
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_resolve.py -q`
  Expected: all pass.

- [ ] Write the failing migration and reword tests. Append to `tests/test_check.py`:

```python
def test_a_pre_v7_seen_value_on_a_nested_target_reads_stale_and_re_blesses():
    from doc_lattice.hashing import content_hash
    from doc_lattice.resolve import cached_target_hash
    from doc_lattice.sections import section_text

    body = "# Parent\n\n## Child\nbody\n"
    up = ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body=body)
    pre_v7 = content_hash(section_text(body, (3, 4)))
    down = ParsedDoc(
        path=Path("docs/down.md"),
        meta=NodeMeta.model_validate(
            {"id": "down", "derives_from": [{"ref": "up#child", "seen": pre_v7}]}
        ),
        body="# Down\n",
    )
    lattice = build_lattice([up, down])

    assert check_lattice(lattice)[0].state == "STALE"

    re_blessed = cached_target_hash(lattice, TargetId("up", "child"), {})
    revived = build_lattice(
        [
            up,
            ParsedDoc(
                path=down.path,
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#child", "seen": re_blessed}]}
                ),
                body=down.body,
            ),
        ]
    )
    assert check_lattice(revived)[0].state == "OK"


def test_rewording_an_ancestor_stales_a_child_targeted_edge():
    from doc_lattice.resolve import cached_target_hash

    before = build_lattice(
        [ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"),
                   body="# Parent\n\n## Child\nbody\n")]
    )
    seen = cached_target_hash(before, TargetId("up", "child"), {})
    after = build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"),
                      body="# Reworded Parent\n\n## Child\nbody\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#child", "seen": seen}]}
                ),
                body="# Down\n",
            ),
        ]
    )

    assert check_lattice(after)[0].state == "STALE"
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_check.py tests/test_resolve.py -q`
  Expected: all pass.

- [ ] Commit:
  `git commit -am "feat(resolve): make the ancestor heading chain part of a section's hash input"`

---

### Task 14: Reconcile under the second envelope

**Files:**
- Modify: `src/doc_lattice/reconcile.py`
- Test: `tests/test_reconcile.py`, `tests/cli/test_reconcile.py`

**Interfaces:**
- Consumes: `split_frontmatter_parts` returning `FrontmatterParts` with `kind`, `meta_start`,
  `meta_end` (Task 1 and 2); `refuse_double_hyphen` (Task 4); `Lattice.collisions` and
  `format_collision` (Task 9).
- Unchanged: the reassembly expression in `apply_reconcile`, which
  `tests/test_conventions.py`'s `ENVELOPE_ORDER` guard pins by field name. Because
  `open_fence` and `close_fence` hold the comment delimiters for a comment-spelled file, the
  rewriter preserves whichever spelling a file uses and never converts one to the other, with
  no branch and no change to the guarded expression.

Steps:

- [ ] Write the failing spelling-preservation tests. Append to `tests/test_reconcile.py`:

```python
COMMENT_DOWNSTREAM = (
    "<!-- doc-lattice\n"
    "id: down\n"
    "derives_from:\n"
    "  - ref: up#one\n"
    "    seen: oldoldoldoldoldoldoldoldoldoldol\n"
    "-->\n"
    "# Down\nbody\n"
)


def test_reconcile_rewrites_a_comment_envelope_and_preserves_its_delimiters():
    new_text, applied = apply_reconcile(
        COMMENT_DOWNSTREAM, {"up#one": "b" * 32}, Path("down.md")
    )

    assert applied == {"up#one"}
    assert new_text.startswith("<!-- doc-lattice\n")
    assert "\n-->\n# Down\nbody\n" in new_text
    assert "---" not in new_text
    assert f"seen: {'b' * 32}" in new_text


def test_reconcile_never_converts_a_fence_to_a_comment_envelope():
    text = (
        "---\nid: down\nderives_from:\n  - ref: up#one\n"
        "    seen: oldoldoldoldoldoldoldoldoldoldol\n---\n# Down\n"
    )

    new_text, applied = apply_reconcile(text, {"up#one": "b" * 32}, Path("down.md"))

    assert applied == {"up#one"}
    assert new_text.startswith("---\n")
    assert "<!-- doc-lattice" not in new_text


def test_a_rewrite_that_would_introduce_a_double_hyphen_is_refused():
    text = (
        "<!-- doc-lattice\nid: down\nderives_from:\n  - ref: up#one\n"
        "    seen: oldoldoldoldoldoldoldoldoldoldol\n-->\n# Down\n"
    )

    with pytest.raises(UnreadableDocError) as excinfo:
        apply_reconcile(text, {"up#one": "aa--bb" + "c" * 26}, Path("down.md"))

    message = str(excinfo.value)
    assert "'--'" in message
    assert "nothing was rewritten" in message


def test_the_alias_relocation_candidate_is_pinned_either_way_its_verdict_lands():
    # Adversarial review's candidate: an escaped "--" reachable through a relocated seen anchor.
    # Whatever the rewriter does with it, the outcome is closed by this gate rather than by
    # reasoning about the rewriter. Either the rewrite is refused, or the re-emitted envelope
    # still holds no raw "--".
    text = (
        "<!-- doc-lattice\n"
        "id: down\n"
        'marker: &m "a\\u002D\\u002Db"\n'
        "derives_from:\n"
        "  - ref: up#one\n"
        "    seen: *m\n"
        "-->\n"
        "# Down\n"
    )

    try:
        new_text, applied = apply_reconcile(text, {"up#one": "b" * 32}, Path("down.md"))
    except UnreadableDocError as exc:
        assert "nothing was rewritten" in str(exc)
        return
    if applied:
        envelope = new_text.split("\n-->\n", 1)[0]
        assert "--" not in envelope.removeprefix("<!-- doc-lattice\n")
```

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_reconcile.py -q -k comment_envelope or double_hyphen or alias_relocation`
  Expected: the rewrite tests already pass (the reassembly is spelling-agnostic by construction),
  and `test_a_rewrite_that_would_introduce_a_double_hyphen_is_refused` fails with
  `DID NOT RAISE`.

- [ ] Add the output-side refusal in `src/doc_lattice/reconcile.py`. Import
  `refuse_double_hyphen` alongside `split_frontmatter_parts`, and insert one call in
  `apply_reconcile` between the reparse gate and the reassembly:

```python
    _verify_reconciled_meta(new_meta, _expected_frontmatter(data, plan.entry_updates), source)
    if parts.kind == "comment":
        _refuse_rewritten_double_hyphen(new_meta, parts, current_file_text, source)
    rewritten = (
        f"{parts.prefix}{parts.open_fence}\n{new_meta}"
        f"{parts.close_fence}{parts.close_fence_newline}{parts.body}"
    )
```

  and add the wrapper beside `_verify_reconciled_meta`:

```python
def _refuse_rewritten_double_hyphen(
    new_meta: str, parts: FrontmatterParts, current_file_text: str, source: Path
) -> None:
    """Re-run the ``--`` refusal against the rewritten comment envelope before it is staged.

    Not argued by construction from "reconcile only writes hex ``seen`` values": the rewriter
    can re-spell content beyond the value it targets, and adversarial review produced a YAML
    alias-relocation candidate where an escaped ``"--"`` could be re-emitted literally. The
    commit transaction never rereads what it stages, so this is the last point at which such a
    rewrite can be refused instead of published.

    Args:
        new_meta: The spliced envelope body about to be reattached.
        parts: The split result the rewrite was measured against.
        current_file_text: The fresh read, used to locate the body's first file line.
        source: The downstream file, for the error message.

    Raises:
        UnreadableDocError: If the rewritten body carries ``--``.
    """
    first_body_line = current_file_text[: parts.meta_start].count("\n") + 1
    try:
        refuse_double_hyphen(new_meta, source, first_body_line=first_body_line)
    except FrontmatterError as exc:
        msg = f"{exc}, so nothing was rewritten"
        raise UnreadableDocError(msg, source=source) from exc
```

  Import `FrontmatterError` and `FrontmatterParts` in `reconcile.py`.

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_reconcile.py -q -k comment_envelope or double_hyphen or alias_relocation`
  Expected: all pass.

- [ ] Write the failing ambiguity-refusal test. Append to `tests/test_reconcile.py`:

```python
def test_reconcile_refuses_to_bless_an_ambiguous_edge():
    lattice = build_lattice(
        [
            ParsedDoc(path=Path("docs/up.md"), meta=NodeMeta(id="up"), body="# Notes\n\n# Notes\n"),
            ParsedDoc(
                path=Path("docs/down.md"),
                meta=NodeMeta.model_validate(
                    {"id": "down", "derives_from": [{"ref": "up#notes"}]}
                ),
                body="# Down\n",
            ),
        ]
    )

    with pytest.raises(ValidationError) as excinfo:
        reconcile(lattice, "down", None, reconcile_all=False)

    message = str(excinfo.value)
    assert 'ambiguous with "Notes" (line 1), "Notes" (line 3)' in message
    assert "reword" in message
    assert "{#anchor}" in message
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_reconcile.py -q -k refuses_to_bless`
  Expected: `DID NOT RAISE <class 'ValidationError'>`.

- [ ] Add the refusal in `src/doc_lattice/reconcile.py`'s `reconcile`, immediately after the
  `if edge.target_id is None:` block and before `new_seen = cached_target_hash(...)`:

```python
            collision = lattice.collisions.get(edge.target_id)
            if collision is not None:
                # Refusing keeps the tool from blessing a dependency the declaration cannot
                # unambiguously name. Writing `seen` here would lock a hash to an id document
                # order can hand to a different heading, which resolves without breaking.
                raise ValidationError(
                    f"cannot reconcile {node_id!r} -> {edge.target_ref!r}: the target id is "
                    f"{format_collision(collision)}; disambiguate by rewording one of the "
                    "colliding headings, or give the target an explicit '{#anchor}' marker"
                )
```

  Import `format_collision` from `.model`.

- [ ] Run the reconcile suites and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_reconcile.py tests/test_reconcile_commit.py tests/test_reconcile_transaction.py tests/test_reconcile_fuzz.py tests/test_conventions.py tests/cli/test_reconcile.py -q -n auto --dist loadfile`
  Expected: all pass. `tests/test_conventions.py`'s reparse-gate guard must be green without
  edits, because the reassembly expression and every `ENVELOPE_ORDER` field name are unchanged.

- [ ] Commit:
  `git commit -am "feat(reconcile): preserve envelope spelling, refuse '--' output and ambiguous edges"`

---

### Task 15: The `lattice_format` version-skew guard

**Files:**
- Modify: `src/doc_lattice/constants.py`, `src/doc_lattice/config.py`,
  `src/doc_lattice/scaffold.py`, `CHANGELOG.md`
- Test: `tests/test_config.py`, `tests/test_scaffold.py`, plus every suite that writes a
  `.doc-lattice.yml`

**Interfaces:**
- Produces: `LATTICE_FORMAT_VERSION: Literal[2] = 2` in `constants.py`.
- Produces: `Config.lattice_format: int | None = None` with a value validator.
- Produces: `load_config` refusing a **present** config file that omits the key.
- Consumes: `render_config(docs_roots, linear_team)` in `scaffold.py`.

Scope note that must be stated plainly. Zero-config runs (no `.doc-lattice.yml` anywhere) stay
supported and are exempt: the guard is a key in a file, and a run with no file has none to
declare. That exemption is not a convenience, it is forced by the spec's own scope rule that
`tests/conftest.py`'s `lattice_dir` fixture must not be modified, and `lattice_dir` writes no
config. Older engines still reject a v7 config for free, because `Config` is
`strict=True, extra="forbid"`.

Steps:

- [ ] Write the failing config tests. Append to `tests/test_config.py`:

```python
def test_a_config_without_lattice_format_is_refused_with_a_migration_pointer(tmp_path: Path):
    config = tmp_path / ".doc-lattice.yml"
    config.write_text("docs_roots:\n  - docs\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config, tmp_path)

    message = str(excinfo.value)
    assert "lattice_format: 2" in message
    assert "CHANGELOG.md" in message


def test_a_config_declaring_the_key_loads(tmp_path: Path):
    config = tmp_path / ".doc-lattice.yml"
    config.write_text("lattice_format: 2\ndocs_roots:\n  - docs\n", encoding="utf-8")

    assert load_config(config, tmp_path).config.lattice_format == 2


def test_a_wrong_lattice_format_names_the_engine_it_needs(tmp_path: Path):
    config = tmp_path / ".doc-lattice.yml"
    config.write_text("lattice_format: 3\ndocs_roots:\n  - docs\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config, tmp_path)

    assert "lattice_format 3" in str(excinfo.value)


def test_zero_config_stays_supported(tmp_path: Path):
    assert load_config(None, tmp_path).config.lattice_format is None


def test_a_pre_v7_config_model_rejects_the_key():
    # Reconstructed in-test rather than imported, because the point is what an engine that
    # predates the field does, and this engine has it. `extra="forbid"` is what makes every
    # pre-v7 release hard-error on a converted repository before loading or reconciling.
    class PreV7Config(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")

        docs_roots: list[str] = Field(default_factory=lambda: ["docs"])
        ignore_globs: list[str] = Field(default_factory=list)
        linear_team: str | None = None
        cache_key: str | None = None
        cache_trust_stat: bool = False

    with pytest.raises(PydanticValidationError):
        PreV7Config.model_validate({"docs_roots": ["docs"], "lattice_format": 2})
```

  Add `from pydantic import BaseModel, ConfigDict, Field` and
  `from pydantic import ValidationError as PydanticValidationError` to the test imports.

- [ ] Run them and watch them fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_config.py -q -k lattice_format or pre_v7`
  Expected: `DID NOT RAISE <class 'ConfigError'>` on the first case.

- [ ] Add the constant to `src/doc_lattice/constants.py`, beside the reconcile journal versions:

```python
# The lattice format this engine reads, declared as `lattice_format` in `.doc-lattice.yml` and
# required there from 7.0. Both skew directions fail loud with no cooperation from code that
# predates the feature: a v7 engine refuses a config that omits the key, and every pre-v7 engine
# refuses one that carries it, because `Config` is `strict=True, extra="forbid"`. Literal-typed
# because `Config` pins the field to this exact value. The hash scheme is deliberately not
# versioned separately: the config boundary is the guard, and a second version channel would be
# redundant surface (AD-15 spirit).
LATTICE_FORMAT_VERSION: Literal[2] = 2
```

- [ ] Add the field and the presence check in `src/doc_lattice/config.py`. Import
  `LATTICE_FORMAT_VERSION` from `.constants`, add the field to `Config` above `docs_roots`:

```python
    lattice_format: int | None = None
```

  add the validator beside `_validate_cache_key`:

```python
    @field_validator("lattice_format")
    @classmethod
    def _validate_lattice_format(cls, value: int | None) -> int | None:
        """Reject a lattice_format this engine does not read."""
        if value is not None and value != LATTICE_FORMAT_VERSION:
            msg = (
                f"lattice_format {value} is not a format this engine reads; it reads "
                f"lattice_format {LATTICE_FORMAT_VERSION}. Install the doc-lattice release that "
                "matches the lattice, or change the key"
            )
            raise ValueError(msg)
        return value
```

  and add the presence check to `load_config`, after `config = Config.model_validate(raw)`:

```python
    # Required only when a config file was actually read. A zero-config run has no file to
    # declare it in, and an engine of any version reading the same tree has none either, so
    # there is no skew for the key to catch.
    if source is not None and config.lattice_format is None:
        msg = (
            f"config {format_path_for_display(source)} does not declare "
            f"'lattice_format: {LATTICE_FORMAT_VERSION}', which doc-lattice 7 requires. Add the "
            "key, then run 'doc-lattice reconcile --all' to re-bless the lattice under the 7.0 "
            "content hash; see the 7.0.0 migration in CHANGELOG.md"
        )
        raise ConfigError(msg)
```

- [ ] Run them and watch them pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_config.py -q -k lattice_format or pre_v7 or zero_config`
  Expected: all pass.

- [ ] Write the failing scaffold test. Append to `tests/test_scaffold.py`:

```python
def test_the_scaffolded_config_declares_the_lattice_format_first():
    text = render_config(("docs",), None)

    assert "lattice_format: 2\n" in text
    assert text.index("lattice_format:") < text.index("docs_roots:")
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_scaffold.py -q -k lattice_format`
  Expected: `AssertionError: assert 'lattice_format: 2\n' in ...`.

- [ ] Update `src/doc_lattice/scaffold.py`: import `LATTICE_FORMAT_VERSION` from `.constants`,
  and change `render_config`'s data construction to:

```python
    data: dict[str, int | list[str] | str] = {
        "lattice_format": LATTICE_FORMAT_VERSION,
        "docs_roots": list(docs_roots),
    }
```

  Extend the `render_config` docstring with one sentence: the required `lattice_format` key leads
  the active block, because it is the version-skew guard and an adopter reading the file should
  meet it first.

- [ ] Run it and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_scaffold.py -q`
  Expected: all pass.

- [ ] Write the `### Migration` note that authorizes the generated-output change. Under
  `## [Unreleased]` in `CHANGELOG.md`, add a `### Migration` subsection with the three steps
  (add `lattice_format: 2`; run `reconcile` to re-bless; fix newly `AMBIGUOUS` edges). Then run
  the migration gate:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_migration_rule.py`
  Expected: pass, because `render_config`'s output changed and the subsection now exists.

- [ ] Find and update every test that writes a config file:
  `rg -n "doc-lattice\.yml" tests | rg "write_text|write\(" ` and
  `rg -n "docs_roots" tests`. Add `lattice_format: 2\n` as the first line of every written config
  in `tests/test_config.py`, `tests/test_cache.py`, `tests/test_orchestrate.py`,
  `tests/test_path_display_contract.py`, `tests/cli/test_check.py`, `tests/cli/test_contract.py`,
  `tests/cli/test_lint.py`, `tests/cli/test_graph.py`, and `tests/cli/test_reconcile.py`, except
  where the test is specifically about a missing or invalid key. Update
  `tests/cli/test_init.py`'s spelled-out expectations of what `init` writes, and
  `tests/test_readme_contract.py` is fixed by the README edit in Task 16.
  `tests/conftest.py` is untouched: `lattice_dir` writes no config and runs zero-config.

- [ ] Run the full suite and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest -q -n auto --dist loadfile`
  Expected: the only remaining failure is `tests/test_readme_contract.py`'s YAML fence
  comparison, which Task 16 fixes.

- [ ] Commit:
  `git commit -am "feat(config): require lattice_format 2 and scaffold it"`

---

### Task 16: Contract documents

**Files:**
- Modify: `ARCHITECTURE.md`, `README.md`
- Test: `tests/test_readme_contract.py`

**Interfaces:**
- Consumes: `render_config(("docs",), None)`, which `test_readme_contract.py` compares
  byte-for-byte against README's sample `.doc-lattice.yml` fence.
- Consumes: `scripts/check_doc_links.py`, which resolves every relative link and `#anchor` in the
  sorted root `*.md` files against a full-CommonMark heading inventory. A new heading is a new
  link target; a renamed one breaks existing deep links.

Steps:

- [ ] Add the new decision to `ARCHITECTURE.md`, after AD-43, as
  `### AD-44: Metadata is GitHub-invisible by a second envelope, and auto-slug identity is made
  safe to use`. Record, in the file's existing Date / Status / Context / Decision /
  Consequences shape:
  - Both spellings accepted unconditionally and forever, with no spelling-selector config
    (AD-15: a selector here would be speculative, since nothing needs to forbid either one).
  - The comment opener is byte-exact at column zero, with a near-miss tier that is an error
    rather than untracked prose, and no BOM allowance, because
    `markdown-it-py==4.2.0` parses a BOM-prefixed opener as a paragraph rather than an
    `html_block` and the envelope would render.
  - The `--` refusal, enforced on input and on rewritten output, in the AD-35
    refuse-don't-respell spirit.
  - The fail-closed classification of the branded envelope: it names this engine, so it has no
    untracked or id-less tier.
  - The auto-slug convention for GitHub-published repositories, and the marker as the opt-in
    reword-stable alternative.
  - Probe-complete ambiguous-target detection, the `AMBIGUOUS` edge state, and the reconcile
    refusal.
  - Full-inventory collision tracing with ATX-only addressability preserved, and GTX-277 named
    as the follow-up that would run allocation over the full inventory.
  - The context-inclusive target hash, and that whole-file targets are unaffected.
  - The `lattice_format` version-skew guard, including the zero-config exemption and why it
    exists.
  - The safe heading-display representation, widening `text_utils.strip_control_chars` beyond
    the AD-34 scope note to cover heading text at every naming sink, and why heading text is
    cleaned rather than refused (AD-35 refuses frontmatter values; a heading is body content and
    refusing it would make an unremarkable document unloadable).

- [ ] Amend AD-31 with a `**Layer 2a: declared envelopes.**` paragraph inserted immediately after
  the Layer 2 table: the rewriter operates purely on the inner YAML and re-emits the file's
  original delimiters byte-for-byte, so the two envelopes share every inner layer (semantic
  schema, supported YAML spellings, occurrence addressing) by construction, and reconcile never
  converts one spelling to the other in either direction.

- [ ] Run the link gate and watch it pass:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_doc_links.py`
  Expected: pass.

- [ ] Update `README.md`'s edge-state material:
  - The "Drift states" table (line 71) gains a row:
    `| **AMBIGUOUS** | The ref resolves, but its target id sits in a slug-collision component,
    so document order can hand that id to a different heading. `check` exits 1, `reconcile`
    refuses to write `seen`, and every command names the colliding headings. |`
    Change the lead-in from "one of four states" to "one of five states".
  - The command table row at line 241 and the `--only` help prose gain `AMBIGUOUS`.
  - Every spelled-out summary breakdown (lines 176, 190, 380, 386) gains `, 0 AMBIGUOUS`.
  - The hashing paragraph after the table gains one sentence: a section ref hashes the section's
    ancestor heading chain followed by the section text, so a section that moves under a
    different parent or whose ancestor is reworded goes STALE; a file ref is unaffected.

- [ ] Update `README.md`'s "Frontmatter reference" section (line 429): document the second
  spelling next to the first, with the worked example from the spec, and state that the comment
  body is the identical YAML, that the opener must be exactly `<!-- doc-lattice` on line 1 at
  column zero with no BOM, that the body ends at the first column-zero `-->`, that the body must
  not contain `--`, and that a comment envelope that is not a mapping carrying `id` is a
  `FRONTMATTER_ERROR` with exit 2 rather than being skipped.

- [ ] Add a `## Publishing on GitHub` section to `README.md` recommending the comment envelope
  plus auto-slug refs, and surfacing the two facts that today live only in code comments:
  `{#anchor}` renders as literal heading text on GitHub, and the marker therefore changes the
  GitHub-side fragment of that heading (`## Notes {#n}` renders the id `notes-n`).

- [ ] Update `README.md`'s Configuration section (line 599): replace the sample YAML fence with
  the exact current output of `render_config(("docs",), None)`, which now leads with
  `lattice_format: 2`, and document the key in the surrounding prose as required whenever a
  `.doc-lattice.yml` is present, with the zero-config exemption stated.

- [ ] Run the README contract and the link gate:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_readme_contract.py -q && env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_doc_links.py`
  Expected: both pass. If the fence comparison fails, print
  `render_config(("docs",), None)` and paste it verbatim.

- [ ] Commit:
  `git commit -am "docs: record AD-44, amend AD-31, and publish the v7 contract in README"`

---

### Task 17: Release 7.0.0

**Files:**
- Modify: `src/doc_lattice/__init__.py`, `pyproject.toml`, `CHANGELOG.md`, `README.md`,
  `MANAGED_CI.md`, `scripts/migration_baseline.json`
- Test: `tests/test_package_metadata.py`, `tests/test_readme_contract.py`,
  `tests/test_managed_ci_recipe.py`

**Interfaces:**
- Consumes: `scripts/check_version_sync.py`, whose `PIN_MANIFEST` declares
  `{"README.md": 3, "MANAGED_CI.md": 5}` as the exact recognized install-pin count per document
  and whose `HISTORICAL_PIN_DOCS` exempts `CHANGELOG.md`.
- Consumes: `scripts/check_migration_rule.py --update`, which rolls the baseline stamp forward in
  the same commit that promotes the heading.

**Pinned install refs: unchanged in number.** This release adds no new install instruction. The
"Publishing on GitHub" section from Task 16 carries no `doc-lattice==X.Y.Z` or `doc-lattice@vX.Y.Z`
ref, so `PIN_MANIFEST` stays `{"README.md": 3, "MANAGED_CI.md": 5}` and only the version inside
those eight existing pins moves from 6.0.0 to 7.0.0.

Steps:

- [ ] Write the failing version test. Append to `tests/test_package_metadata.py`:

```python
def test_the_declared_version_is_seven():
    assert __version__ == "7.0.0"
```

- [ ] Run it and watch it fail:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_package_metadata.py -q -k version_is_seven`
  Expected: `AssertionError: assert '6.0.0' == '7.0.0'`.

- [ ] Bump `__version__ = "7.0.0"` in `src/doc_lattice/__init__.py` and `version = "7.0.0"` in
  `pyproject.toml`.

- [ ] Promote `## [Unreleased]` to `## [7.0.0] - 2026-08-31` in `CHANGELOG.md`, keeping the
  `### Migration` subsection Task 15 wrote and expanding it to the three steps plus the
  conversion note:
  1. Add `lattice_format: 2` to `.doc-lattice.yml`. doc-lattice 7 refuses to run without it, and
     every earlier release refuses to run with it, so the two never operate on the same tree by
     accident. A zero-config project has nothing to add.
  2. The section content hash now includes the target's ancestor heading chain, so every `seen`
     value on a section that sits under a parent heading mismatches once. Run
     `doc-lattice reconcile --all` to re-bless the lattice. Whole-file refs and top-level
     sections are unaffected.
  3. An edge whose target id sits in a slug-collision component now fails `AMBIGUOUS`, `check`
     exits 1 on it, and `reconcile` refuses to write its `seen`. Fix each one by rewording a
     colliding heading or giving the target an explicit `{#anchor}` marker.
  Then the conversion note: to make a tracked file's metadata invisible on GitHub, replace its
  opening `---` with `<!-- doc-lattice`, replace its closing `---` with `-->`, and rerun
  `check`. The YAML between them is unchanged. A file whose id or any other value contains `--`
  keeps the fence spelling or renames the value.

- [ ] Bump the eight existing install pins from `6.0.0` to `7.0.0`: three in `README.md`, five in
  `MANAGED_CI.md`. Find them with
  `rg -n "doc-lattice(==|@v)[0-9]+\.[0-9]+\.[0-9]+" README.md MANAGED_CI.md`.

- [ ] Roll the migration baseline in this same commit:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_migration_rule.py --update`

- [ ] Run every release gate and watch them pass:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_version_sync.py
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_doc_links.py
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_migration_rule.py
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest tests/test_package_metadata.py \
  tests/test_readme_contract.py tests/test_managed_ci_recipe.py tests/test_check_version_sync.py \
  tests/test_check_migration_rule.py tests/test_release_gate.py tests/test_release_target.py -q
```

  Expected: all pass.

- [ ] Commit:
  `git commit -am "release: 7.0.0"`

---

### Task 18: Full verification

**Files:** none modified unless a gate fails.

**Interfaces:** the complete handoff verification set from CLAUDE.md.

Steps:

- [ ] Run the full test suite:
  `env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev pytest -q -n auto --dist loadfile`
  Expected: all pass, coverage at least 80 percent. `-n auto --dist loadfile` is required:
  bare `-n auto` splits `tests/test_reconcile_fuzz.py`'s module-level claim accumulator.

- [ ] Run the lint and format gates:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev ruff check src tests scripts
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev ruff format --check src tests scripts
```

  Expected: no findings. Ruff's line length is 100.

- [ ] Run the type and boundary gates:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev ty check src scripts
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_typing_boundaries.py src
```

  Expected: no findings. Narrow suppressions, if any are needed, are spelled
  `# ty: ignore[code]`, never mypy style.

- [ ] Run the documentation and release gates:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_version_sync.py
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_doc_links.py
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/check_migration_rule.py
```

  Expected: all pass.

- [ ] Run the generator and benchmark gates, because Task 8 touched `markdown_compat.py`:

```bash
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/generate_github_slugger_data.py --check
env -u VIRTUAL_ENV -u FORCE_COLOR uv run --group dev python scripts/bench_sections.py
```

  Expected: the generator check passes unchanged (`_github_slugger_data.py` was never
  hand-edited, the Node pin in `.nvmrc` was not touched, and `_Slugger.slug` itself is
  unmodified), and the benchmark reports no regression from the added full-inventory parse. If
  `generate_github_slugger_data.py --check` needs Node, run `nvm use` first: the generator
  rejects any interpreter but the exact pinned version.

- [ ] Confirm `git status` is clean and every commit from Tasks 1 to 17 is present:
  `git status --short && git log --oneline -18`
  Expected: clean tree, eighteen or fewer commits on the branch, none of them touching
  `tests/conftest.py`.
