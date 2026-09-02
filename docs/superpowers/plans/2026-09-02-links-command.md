# `links` Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `scripts/check_doc_links.py` into the package as the consumer-facing `doc-lattice links` command over a configured `link_sources` set, run it from both generated adopter surfaces, and make this repository's own gate run through the shipped command.

**Architecture:** A pure selector-grammar module (`link_selectors.py`) is shared by config validation, the scaffold, and a new read-only I/O module (`link_check.py`) that expands selectors with its own no-follow walk and runs the moved checker over typed findings. A thin Typer adapter (`cli/commands/links.py`) renders findings to stderr as `path:line: message` lines or to stdout as GitHub annotations. `init` derives `link_sources` from the docs roots, and the generated pre-commit and workflow blocks gain the command under the migration rule.

**Tech Stack:** Python 3.13+, pydantic (strict config), markdown-it-py 4.2.0 (pinned), Typer, Rich, pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-09-02-links-command-design.md`

## Global Constraints

- Python 3.13 or later; every command runs through `uv run --group dev ...`.
- Run pytest as `env -u FORCE_COLOR uv run --group dev python -m pytest -n auto --dist loadfile` (the dev shell exports `FORCE_COLOR`, and `--dist loadfile` is required by the fuzz suite).
- Ruff line length 100. Every module has a module docstring; public functions use Google-style docstrings.
- No em dashes anywhere in source or documents (`tests/test_conventions.py` enforces it). Use `--` in prose comments where the existing code does.
- No `typing.Any` or `typing.cast` outside the AD-3 allowlist; the new modules use neither.
- No `except Exception` or `except BaseException`.
- Custom exceptions extend `ProjectError`. No new error code is added: selection and selector errors are `ConfigError`, document read failures are `UnreadableDocError`.
- Path display: any expression named `path`, `source`, `destination`, `link`, `cwd`, `project_root` (and `root`, `target`, `entry` in the modules that scope them) may only reach an f-string wrapped inline in `format_path_for_display(...)`. Bind the displayed string to a name like `displayed` first if you need it twice.
- `ty` suppressions are spelled `# ty: ignore[code]`.
- The committed `scripts/migration_baseline.json` is NOT regenerated in this change. The `### Migration` subsection under `## [Unreleased]` authorizes the generated-output diff.
- Commit after every task with the message shown; do not add attribution lines.

---

## File structure

Create:
- `src/doc_lattice/link_selectors.py`: pure selector grammar (validate, match one segment, escape a literal).
- `src/doc_lattice/link_check.py`: read-only I/O boundary: `select_link_sources`, `check_links`, `LinkFinding`, and the moved checker internals.
- `src/doc_lattice/cli/commands/links.py`: the `links` Typer adapter.
- `tests/test_link_selectors.py`, `tests/test_link_check.py`, `tests/cli/test_links.py`, `tests/test_link_gate_wiring.py` (repository-only).
- `.doc-lattice.yml` at the repository root.

Modify: `config.py`, `constants.py`, `cli/options.py`, `cli/github.py`, `cli/runtime.py`, `cli/application.py`, `scaffold.py`, `cli/commands/init.py`, `markdown_compat.py` (docstring), `scripts/check_migration_rule.py`, `scripts/check_version_sync.py`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `pyproject.toml`, and the tests and documents each task names.

Delete: `scripts/check_doc_links.py`, `tests/test_check_doc_links.py` (Task 7).

One deliberate wording change from the script: its two document-level messages say "maintained document", which is this repository's vocabulary and names no concept an adopter has. They become "link source". Every link-level message is byte-identical.

---

### Task 1: Selector grammar module

**Files:**
- Create: `src/doc_lattice/link_selectors.py`
- Test: `tests/test_link_selectors.py`

**Interfaces:**
- Produces: `validate_link_selector(entry: str) -> tuple[str, ...]` (raises `ValueError` whose message starts with a verb phrase, e.g. `"contains a backslash; ..."`, so a caller can prefix it with the entry), `segment_matches(name: str, pattern: str) -> bool`, `escape_selector_literal(text: str) -> str`, constants `SELECTOR_SEPARATOR = "/"`, `RECURSIVE_SEGMENT = "**"`.
- Consumes: `doc_lattice.text_utils.strip_control_chars`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the link_sources selector grammar."""

from fnmatch import fnmatchcase

import pytest

from doc_lattice.link_selectors import (
    escape_selector_literal,
    segment_matches,
    validate_link_selector,
)


@pytest.mark.parametrize(
    ("entry", "segments"),
    [
        ("*.md", ("*.md",)),
        ("ARCHITECTURE.md", ("ARCHITECTURE.md",)),
        ("docs/**/*.md", ("docs", "**", "*.md")),
        ("**", ("**",)),
        ("notes [draft]/*.md", ("notes [draft]", "*.md")),
        ("[]]x.md", ("[]]x.md",)),
        ("[!]]x.md", ("[!]]x.md",)),
        ("a]b.md", ("a]b.md",)),
    ],
)
def test_valid_selectors_split_into_segments(entry, segments):
    assert validate_link_selector(entry) == segments


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("", "is empty"),
        ("\x1bdocs/*.md", "control character"),
        ("docs\\guide.md", "backslash"),
        ("/etc/*.md", "is absolute"),
        ("C:docs/*.md", "is absolute"),
        ("docs/", "ends in a separator"),
        ("docs//guide.md", "empty segment"),
        ("./guide.md", "'.' or '..' segment"),
        ("../guide.md", "'.' or '..' segment"),
        ("docs/a**/*.md", "'**' inside a segment"),
        ("notes[1.md", "unclosed"),
        ("[!x.md", "unclosed"),
    ],
)
def test_invalid_selectors_name_the_defect(entry, reason):
    with pytest.raises(ValueError, match=reason):
        validate_link_selector(entry)


def test_segment_matching_is_case_sensitive():
    assert segment_matches("README.md", "*.md")
    assert not segment_matches("README.MD", "*.md")
    assert not segment_matches("readme.md", "README.md")


def test_segment_matching_carries_fnmatch_classes():
    assert segment_matches("x.md", "[!d]*.md")
    assert not segment_matches("docs.md", "[!d]*.md")
    assert segment_matches("b.md", "[a-c].md")
    assert segment_matches("[.md", "[[].md")


@pytest.mark.parametrize("text", ["notes [draft]", "*.md", "??", "a[b*c?d", "plain.md"])
def test_an_escaped_literal_matches_only_itself(text):
    escaped = escape_selector_literal(text)
    assert fnmatchcase(text, escaped)
    assert validate_link_selector(escaped) == (escaped,)
    assert not fnmatchcase(text + "x", escaped)


def test_escape_spells_each_metacharacter_as_a_class():
    assert escape_selector_literal("a[b*c?d") == "a[[]b[*]c[?]d"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_selectors.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc_lattice.link_selectors'`

- [ ] **Step 3: Write the module**

```python
"""The ``link_sources`` selector grammar: lexical validation, segment matching, literal escaping.

Pure and filesystem-free, so ``config`` can validate a selector at load without reaching the
walk that expands it, and ``scaffold`` can spell one for a literal root without reaching the
filesystem at all. The walk itself lives in ``link_check``.

A selector is project-relative and POSIX on every platform: ``/`` is the only separator, and a
backslash is refused rather than read as one, so a config is accepted or rejected identically
wherever it runs. Within one segment ``*``, ``?``, and bracket classes carry ``fnmatch``
semantics, case-sensitively by code point and never crossing ``/``; ``**`` is accepted only as a
whole segment and matches zero or more directories. An unclosed ``[`` is refused rather than read
as a literal, which is what ``fnmatch`` would do, because a selector that silently means something
other than what was written is how a mandatory gate ends up green over the wrong files.
"""

from fnmatch import fnmatchcase
from pathlib import PureWindowsPath

from .text_utils import strip_control_chars

SELECTOR_SEPARATOR = "/"
RECURSIVE_SEGMENT = "**"
_DOT_SEGMENTS = frozenset({".", ".."})
# ``[`` first: the later two introduce brackets of their own that must not be re-escaped.
_LITERAL_ESCAPES = (("[", "[[]"), ("*", "[*]"), ("?", "[?]"))


def validate_link_selector(entry: str) -> tuple[str, ...]:
    """Return a selector's segments, or raise ``ValueError`` naming the first defect.

    The message is a predicate about the entry with no subject, such as ``"contains a
    backslash; '/' is the only separator"``, so a caller can prefix it with however it spells
    the entry.

    Args:
        entry: One ``link_sources`` entry as written.

    Returns:
        The segments between separators, at least one.

    Raises:
        ValueError: If the entry is empty, carries a control character or a backslash, is
            absolute or drive-prefixed, ends in a separator, has an empty or dot segment,
            spells ``**`` inside a segment, or leaves a bracket class unclosed.
    """
    if not entry:
        msg = "is empty"
        raise ValueError(msg)
    if strip_control_chars(entry) != entry:
        msg = "contains a control character"
        raise ValueError(msg)
    if "\\" in entry:
        msg = "contains a backslash; '/' is the only separator"
        raise ValueError(msg)
    if entry.startswith(SELECTOR_SEPARATOR) or PureWindowsPath(entry).drive:
        msg = "is absolute; a selector is relative to the project root"
        raise ValueError(msg)
    if entry.endswith(SELECTOR_SEPARATOR):
        msg = "ends in a separator; a selector names files, not a directory"
        raise ValueError(msg)
    segments = tuple(entry.split(SELECTOR_SEPARATOR))
    for segment in segments:
        if segment == "":
            msg = "has an empty segment"
            raise ValueError(msg)
        if segment in _DOT_SEGMENTS:
            msg = "has a '.' or '..' segment"
            raise ValueError(msg)
        if RECURSIVE_SEGMENT in segment and segment != RECURSIVE_SEGMENT:
            msg = "spells '**' inside a segment; it is accepted only as a whole segment"
            raise ValueError(msg)
        if _has_unclosed_bracket(segment):
            msg = "leaves a '[' bracket class unclosed"
            raise ValueError(msg)
    return segments


def _has_unclosed_bracket(segment: str) -> bool:
    """Report whether a ``[`` in the segment never finds the ``]`` that closes it.

    Mirrors the scan ``fnmatch.translate`` performs: an optional ``!`` may follow the ``[``, and a
    ``]`` in the first position after that is a member of the class rather than its close.
    """
    index = 0
    end = len(segment)
    while index < end:
        if segment[index] != "[":
            index += 1
            continue
        cursor = index + 1
        if cursor < end and segment[cursor] == "!":
            cursor += 1
        if cursor < end and segment[cursor] == "]":
            cursor += 1
        while cursor < end and segment[cursor] != "]":
            cursor += 1
        if cursor >= end:
            return True
        index = cursor + 1
    return False


def segment_matches(name: str, pattern: str) -> bool:
    """Report whether one directory entry name matches one non-recursive selector segment.

    Args:
        name: A single entry name, with no separator in it.
        pattern: One validated selector segment other than ``**``.

    Returns:
        True when ``fnmatch`` matches them case-sensitively.
    """
    return fnmatchcase(name, pattern)


def escape_selector_literal(text: str) -> str:
    """Return ``text`` spelled so the grammar reads every character literally.

    Args:
        text: A path or path fragment meant as itself, not as a pattern.

    Returns:
        The text with ``[``, ``*``, and ``?`` each wrapped in a one-member bracket class.
    """
    for character, escaped in _LITERAL_ESCAPES:
        text = text.replace(character, escaped)
    return text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_selectors.py -q`
Expected: all PASS

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run --group dev ruff check src tests && uv run --group dev ruff format src tests && uv run --group dev ty check src
git add src/doc_lattice/link_selectors.py tests/test_link_selectors.py
git commit -m "feat(links): add the link_sources selector grammar"
```

---

### Task 2: `link_sources` config key and config provenance

**Files:**
- Modify: `src/doc_lattice/config.py` (`Config` fields at lines 49-54, `ProjectConfig` at lines 93-99, `load_config` return at line 159)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `validate_link_selector` from Task 1.
- Produces: `Config.link_sources: list[str]` (default `[]`), `ProjectConfig.config_path: Path | None` (default `None`, the file `load_config` read, or `None` under zero-config).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`)

```python
def test_link_sources_default_to_an_empty_list_with_no_config_file(tmp_path: Path):
    project = load_config(None, tmp_path)
    assert project.config.link_sources == []
    assert project.config_path is None


def test_link_sources_are_loaded_verbatim_and_the_config_path_is_recorded(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\nlink_sources:\n  - docs/**/*.md\n  - ARCHITECTURE.md\n",
        encoding="utf-8",
    )
    project = load_config(None, tmp_path)
    assert project.config.link_sources == ["docs/**/*.md", "ARCHITECTURE.md"]
    assert project.config_path == tmp_path / ".doc-lattice.yml"


def test_an_explicit_config_records_its_own_path(tmp_path: Path):
    explicit = tmp_path / "custom.yml"
    explicit.write_text("lattice_format: 2\n", encoding="utf-8")
    assert load_config(explicit, tmp_path).config_path == explicit


def test_link_sources_null_is_a_schema_error(tmp_path: Path):
    (tmp_path / ".doc-lattice.yml").write_text("lattice_format: 2\nlink_sources:\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="link_sources"):
        load_config(None, tmp_path)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("docs\\\\guide.md", "backslash"),
        ("/etc/passwd", "absolute"),
        ("docs/", "separator"),
        ("../up.md", "'..'"),
        ("notes[1.md", "unclosed"),
    ],
)
def test_a_malformed_link_source_is_a_config_error_naming_the_entry(tmp_path: Path, entry, reason):
    (tmp_path / ".doc-lattice.yml").write_text(
        f"lattice_format: 2\nlink_sources: ['{entry}']\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as info:
        load_config(None, tmp_path)
    assert "link_sources" in str(info.value)
    assert reason in str(info.value)


def test_an_escaping_link_source_is_not_rejected_at_load(tmp_path: Path):
    # Containment is judged after selection, where the escape becomes an exit-1 finding; a
    # load-time rejection here would be the exit-2 config error the design refuses.
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    (tmp_path / ".doc-lattice.yml").write_text(
        "lattice_format: 2\nlink_sources: [escape.md]\n", encoding="utf-8"
    )
    assert load_config(None, tmp_path).config.link_sources == ["escape.md"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_config.py -q -k "link_source or config_path or records_its_own"`
Expected: FAIL (`extra_forbidden` for `link_sources`; `AttributeError` for `config_path`)

- [ ] **Step 3: Implement**

In `src/doc_lattice/config.py`:

Add the import after the `.error_types` import:

```python
from .link_selectors import validate_link_selector
```

Add the field after `ignore_globs` in `Config`:

```python
    link_sources: list[str] = Field(default_factory=list)
```

Add a validator after `_validate_cache_key`:

```python
    @field_validator("link_sources")
    @classmethod
    def _validate_link_sources(cls, value: list[str]) -> list[str]:
        """Reject a link_sources entry the selector grammar cannot read.

        Lexical only, deliberately: nothing here is resolved or checked for existence, so a
        selector that names a symlink out of the project survives load and becomes an exit-1
        finding when the links command selects it, which is the contract the gate documents.
        """
        for entry in value:
            try:
                validate_link_selector(entry)
            except ValueError as exc:
                msg = f"link_sources entry {entry!r} {exc}"
                raise ValueError(msg) from exc
        return value
```

Change `ProjectConfig` to:

```python
@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """A loaded config plus the project root, the resolved docs roots, and the file it came from.

    ``config_path`` is the file ``load_config`` read, or None for a zero-config run. A diagnostic
    about a key the file lacks can then name the file, and one about an absent file can say so
    instead of calling a file that does not exist invalid.
    """

    config: Config
    project_root: Path
    resolved_roots: tuple[Path, ...]
    config_path: Path | None = None
```

Change the last line of `load_config` to:

```python
    return ProjectConfig(
        config=config, project_root=project_root, resolved_roots=roots, config_path=source
    )
```

Update the module docstring's first line to: `"""Load and validate .doc-lattice.yml, with project-root containment of docs_roots and lexical validation of link_sources."""` (wrap to 100 columns as two lines).

- [ ] **Step 4: Run the config suite**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_config.py tests/test_path_display_contract.py -q`
Expected: all PASS. (`entry` is a scoped path-bearing name in `config.py`; the validator interpolates it with `!r` inside a `ValueError` that pydantic re-renders, which is the same shape `_validate_cache_key` uses for `value!r`. If `tests/test_conventions.py` flags `entry` in the new f-string, wrap it as `format_path_for_display(entry)` instead.)

- [ ] **Step 5: Commit**

```bash
uv run --group dev ruff check src tests && uv run --group dev ruff format src tests && uv run --group dev ty check src
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_conventions.py -q
git add src/doc_lattice/config.py tests/test_config.py
git commit -m "feat(config): add the link_sources key and record the config path"
```

---

### Task 3: The link-check engine

**Files:**
- Create: `src/doc_lattice/link_check.py` (from a copy of `scripts/check_doc_links.py`)
- Create: `tests/test_link_check.py` (from a copy of `tests/test_check_doc_links.py`)
- Leave `scripts/check_doc_links.py` and `tests/test_check_doc_links.py` in place; Task 7 deletes them.

**Interfaces:**
- Produces: `LinkFinding(path: str, line: int | None, message: str)` (frozen, slots), `check_links(project_root: Path, sources: Sequence[Path]) -> list[LinkFinding]`, message constants `ESCAPING_SOURCE_MESSAGE`, `UNPARSEABLE_SOURCE_MESSAGE`, `HTML_ANCHOR_MESSAGE`, and the private helpers the tests import: `_PARSER`, `_links_in`, `_anchor_hrefs`, `_split_destination`.
- Consumes: `markdown_compat.full_heading_inventory`, `error_types.UnreadableDocError`, `path_utils.format_path_for_display`.

- [ ] **Step 1: Copy the script and its tests**

```bash
cp scripts/check_doc_links.py src/doc_lattice/link_check.py
cp tests/test_check_doc_links.py tests/test_link_check.py
```

- [ ] **Step 2: Rewrite the test module header**

Replace lines 1-19 of `tests/test_link_check.py` (everything through `_SCRIPT_PATH = ...`) with:

```python
"""Tests for the Markdown link gate engine."""

import errno
import os
import stat
from pathlib import Path

import pytest

import doc_lattice.link_check as link_check_module
from doc_lattice.error_types import UnreadableDocError
from doc_lattice.link_check import (
    _PARSER,
    _anchor_hrefs,
    _links_in,
    _split_destination,
    LinkFinding,
    check_links,
)
from doc_lattice.path_utils import format_path_for_display

# The old script's whole selection, spelled for the engine: the sorted root Markdown files.
# Kept as the fixture for the moved cases so every one of them still reads as it did.
def _root_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.resolve().glob("*.md") if path.is_file())


def _line(finding: LinkFinding) -> str:
    displayed = format_path_for_display(finding.path)
    if finding.line is None:
        return f"{displayed}: {finding.message}"
    return f"{displayed}:{finding.line}: {finding.message}"


def check_repository_links(root: Path) -> list[str]:
    """The old string form of every finding, so the moved cases assert what they always did."""
    return [_line(finding) for finding in check_links(root, _root_sources(root))]


_requires_permission_enforcement = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX filesystem that enforces modes"
)
```

Then delete these tests from the copied file: `test_maintained_documents_are_the_sorted_root_markdown_files`, `test_repository_maintained_documents_have_no_broken_links`, `test_pre_commit_runs_the_link_check`, `test_ci_code_quality_job_runs_the_link_check`. Delete the `from workflow_helpers import ...` line and the `run_path` import. Keep `test_staged_docs_are_not_link_sources_but_may_be_targets` (it now reads as "a target under docs/ may carry a dead link without being a source").

Then update wording: run `grep -n "maintained document" tests/test_link_check.py` and change every assertion on that phrase to `"link source"`.

- [ ] **Step 3: Add the new engine tests** (append to `tests/test_link_check.py`)

```python
def test_findings_are_typed_records(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[gone](MISSING.md)\n")

    findings = check_links(tmp_path, _root_sources(tmp_path))

    assert findings == [
        LinkFinding("README.md", 3, "link target 'MISSING.md' does not exist")
    ]


def test_check_links_sorts_a_copy_of_its_sources_without_mutating_them(tmp_path):
    _write(tmp_path, "B.md", "# B\n\n[gone](MISSING.md)\n")
    _write(tmp_path, "A.md", "# A\n\n[gone](MISSING.md)\n")
    sources = [tmp_path / "B.md", tmp_path / "A.md"]

    findings = check_links(tmp_path, sources)

    assert [finding.path for finding in findings] == ["A.md", "B.md"]
    assert sources == [tmp_path / "B.md", tmp_path / "A.md"]


def test_check_links_rechecks_containment_before_reading_a_source(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# Outside\n\n[gone](MISSING.md)\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)

    findings = check_links(tmp_path, [tmp_path / "escape.md"])

    assert findings == [LinkFinding("escape.md", None, link_check_module.ESCAPING_SOURCE_MESSAGE)]


def test_a_source_that_will_not_decode_is_reported_and_the_run_continues(tmp_path):
    (tmp_path / "A.md").write_bytes(b"\xff\xfe# not utf-8\n")
    _write(tmp_path, "B.md", "# B\n\n[gone](MISSING.md)\n")

    findings = check_links(tmp_path, _root_sources(tmp_path))

    assert [(finding.path, finding.line) for finding in findings] == [("A.md", None), ("B.md", 3)]
    assert findings[0].message == link_check_module.UNPARSEABLE_SOURCE_MESSAGE


@_requires_permission_enforcement
def test_an_unreadable_source_is_a_tool_error_not_a_finding(tmp_path):
    source = tmp_path / "README.md"
    _write(tmp_path, "README.md", "# Readme\n")
    source.chmod(0)
    try:
        with pytest.raises(UnreadableDocError) as info:
            check_links(tmp_path, [source])
    finally:
        source.chmod(0o644)
    assert info.value.source == source
    assert "README.md" in str(info.value)


@_requires_permission_enforcement
def test_an_unreadable_target_is_a_tool_error_not_a_finding(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[t](GUIDE.md#intro)\n")
    target = tmp_path / "GUIDE.md"
    _write(tmp_path, "GUIDE.md", "# Intro\n")
    target.chmod(0)
    try:
        with pytest.raises(UnreadableDocError) as info:
            check_links(tmp_path, _root_sources(tmp_path))
    finally:
        target.chmod(0o644)
    assert info.value.source == target


@_requires_permission_enforcement
def test_a_target_that_cannot_be_inspected_is_a_tool_error(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[t](locked/GUIDE.md)\n")
    _write(tmp_path, "locked/GUIDE.md", "# Guide\n")
    locked = tmp_path / "locked"
    locked.chmod(0)
    try:
        with pytest.raises(UnreadableDocError):
            check_links(tmp_path, _root_sources(tmp_path))
    finally:
        locked.chmod(0o755)


def test_a_parser_invariant_failure_propagates(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", "# Readme\n\n[t](GUIDE.md#intro)\n")
    _write(tmp_path, "GUIDE.md", "# Intro\n")

    def broken(_text: str):
        msg = "malformed heading token pair"
        raise RuntimeError(msg)

    monkeypatch.setattr(link_check_module, "full_heading_inventory", broken)
    with pytest.raises(RuntimeError, match="malformed"):
        check_links(tmp_path, _root_sources(tmp_path))


def test_same_line_findings_keep_links_before_raw_anchors(tmp_path):
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\n<a href="X.md">a</a> and [b](MISSING.md) and <a href="Y.md">c</a>\n',
    )

    findings = check_links(tmp_path, _root_sources(tmp_path))

    assert [finding.line for finding in findings] == [3, 3, 3]
    assert "MISSING.md" in findings[0].message
    assert findings[1].message == link_check_module.HTML_ANCHOR_MESSAGE
    assert findings[2].message == link_check_module.HTML_ANCHOR_MESSAGE


def test_the_target_inventory_is_the_engines_full_heading_inventory(tmp_path):
    # Setext, an indented ATX, and a heading inside a list item all resolve, and the
    # document-order dedup suffix is the one GitHub assigns.
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n[a](GUIDE.md#setext)\n[b](GUIDE.md#indented)\n[c](GUIDE.md#nested)\n"
        "[d](GUIDE.md#twice-1)\n",
    )
    _write(
        tmp_path,
        "GUIDE.md",
        "Setext\n======\n\n   ## Indented\n\n- ## Nested\n\n## Twice\n\n## Twice\n",
    )

    assert check_links(tmp_path, _root_sources(tmp_path)) == []
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_check.py -q`
Expected: FAIL at import (`check_links` and `LinkFinding` do not exist in the copied module yet)

- [ ] **Step 5: Turn the copied script into the engine module**

Edit `src/doc_lattice/link_check.py` as follows.

5a. Replace the shebang and module docstring (lines 1-32) with:

```python
"""The Markdown link gate: select configured sources, then verify every relative link and fragment.

The read-only filesystem boundary for the ``links`` command (AD-2). ``select_link_sources``
expands the ``link_sources`` selectors with a no-follow walk of its own, and ``check_links`` reads
the selected documents and every repository-contained target they link to. Both return data rather
than prose: findings are ``LinkFinding`` records and the ``path[:line]: message`` envelope belongs
to the command adapter.

Containment binds both ends: a source that leaves the project root through a symlink is reported
rather than read, and a target is judged the same way before it is opened. Selection expands a
selector lexically and never enters a symlinked directory, whether ``**`` reaches it or a fixed
segment names it, because following one would let a link to ``/`` turn ``**`` into a filesystem
walk. Every selector has to match at least one lexical path, so a mandatory gate can never pass
over zero files.

Absolute and external destinations are out of scope and skipped, as are image destinations.
Heading fragments are validated only against Markdown targets, against
``markdown_compat.full_heading_inventory``: every heading a GitHub render assigns an id to --
setext, ATX indented one to three spaces, and headings nested in a list item or a block quote --
where the addressable subset the lattice sees is column-zero ATX only. A link to one of the wider
forms renders and resolves on GitHub, so failing it here would fail a correct link; widening the
adapter instead would change which sections the engine sees, which is a cached-derivation change
this gate has no business forcing. Reading the engine's inventory rather than a private walk is
what keeps a fragment resolving to the same id the engine would assign wherever both see the same
heading.

Rendered inline heading text is the one form still out of reach: heading ids are slugged from raw
inline source on both sides, so ``## [Guide](target.md)`` yields ``guidetargetmd`` against
GitHub's ``guide``.

Destinations are read from Markdown link tokens. A destination written as a raw HTML anchor is
reported rather than resolved. Markdown-it normalizes a Markdown destination -- percent-encoding
separators and brackets, trimming surrounding whitespace -- and an attribute value arrives with
none of that done, so resolving one means owning the URL and HTML attribute semantics that
normalization otherwise supplies. That is a wider contract than this gate takes on; reporting the
anchor keeps the gap loud rather than leaving the gate green on a link form it does not model.

Failures fall into two classes. Content the gate cannot read as a document -- bytes that will not
decode, a character reference the parser refuses -- is a finding on that document and the run
continues. A filesystem the gate cannot inspect -- a resolve, stat, scan, or open that fails --
is a tool error: a gate that cannot see its inputs must not pass.
"""
```

5b. Replace the import block (old lines 34-44) with:

```python
import errno
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .error_types import ConfigError, UnreadableDocError
from .link_selectors import (
    RECURSIVE_SEGMENT,
    SELECTOR_SEPARATOR,
    segment_matches,
    validate_link_selector,
)
from .markdown_compat import full_heading_inventory
from .path_utils import format_path_for_display
```

(`ConfigError`, `os`, `RECURSIVE_SEGMENT`, `SELECTOR_SEPARATOR`, `segment_matches`, and `validate_link_selector` are consumed by Task 4; Ruff will flag them as unused until then, so add them in Task 4 instead if you prefer a green lint at this commit.)

5c. Replace the constants block from `_REPO_ROOT = ...` through `_C0_CONTROL_OR_SPACE = ...` (old lines 46-70) with the same block minus `_REPO_ROOT`, with the three message constants made public and reworded:

```python
_PARSER = MarkdownIt("commonmark")
_MARKDOWN_SUFFIX = ".md"
# (keep the _MARKDOWN_TARGET_SUFFIXES comment and definition exactly as they were)
# (keep _SINGLE_DOT_SEGMENTS and _DOUBLE_DOT_SEGMENTS and their comment exactly as they were)
HTML_ANCHOR_MESSAGE = (
    "raw HTML anchor carries a destination this check cannot resolve; write it as a Markdown link"
)
ESCAPING_SOURCE_MESSAGE = "link source leaves the project root through a symlink"
UNPARSEABLE_SOURCE_MESSAGE = "link source could not be parsed for destinations"
# (keep the _C0_CONTROL_OR_SPACE comment and definition exactly as they were)
# The errnos that answer "not here" rather than "cannot tell", the same pair the init adapter's
# ancestor walk uses. Everything else means the filesystem could not answer, which is a tool
# error rather than a finding.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})
```

5d. Add the finding record after the `Link` dataclass:

```python
@dataclass(frozen=True, slots=True)
class LinkFinding:
    """One thing the gate found wrong, located by source document and line.

    ``path`` is the source's project-relative POSIX spelling, raw and unescaped: the human
    renderer applies the display spelling and the GitHub renderer rejoins it to the project root.
    ``line`` is None for a finding about the document itself -- one that escapes the project
    root or will not parse -- since those are refused before any destination is read.
    """

    path: str
    line: int | None
    message: str
```

5e. Delete `maintained_documents`, `_heading_texts`, `_target_message`, `main`, and the `if __name__ == "__main__":` block.

5f. Replace `_heading_ids` with:

```python
def _heading_ids(document: Path, cache: dict[Path, frozenset[str]]) -> frozenset[str] | None:
    """Return the link-target GitHub heading ids of a Markdown document, memoized.

    The ids are the engine's own: ``full_heading_inventory`` reads every heading a GitHub render
    assigns an id to and allocates through the pinned document-order collision rule, so this is
    the one inventory rather than a parallel copy of it. The module docstring records why it is
    wider than the addressable subset.

    A target is read here rather than where the sources are, so it needs the same refusal the
    sources have: a document that will not decode as UTF-8, or that carries a character reference
    wider than the interpreter's integer-conversion limit, raises ``ValueError`` instead of
    answering. That is a finding on the link, not a tool error, and it is not memoized because
    nothing was learned. A read the filesystem refuses is the other class, and propagates.

    Args:
        document: The Markdown target whose heading ids are wanted.
        cache: Heading ids already read, keyed by target path.

    Returns:
        The target's link-target heading ids, or None when its content could not be read.

    Raises:
        UnreadableDocError: If the filesystem refused the read.
        RuntimeError: If the pinned parser returned a malformed heading token pair; that is a
            parser invariant failure, not bad content, and is deliberately not caught.
    """
    if document not in cache:
        try:
            text = document.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None
        except OSError as exc:
            msg = f"link target {format_path_for_display(document)} could not be read: {exc}"
            raise UnreadableDocError(msg, source=document) from exc
        try:
            records = full_heading_inventory(text)
        except ValueError:
            return None
        cache[document] = frozenset(record.github_id for record in records)
    return cache[document]
```

5g. Replace `_escapes_by_symlink` with these two functions (keep its docstring's reasoning in the first):

```python
def _resolved(path: Path) -> Path:
    """Resolve a path, turning a filesystem that will not answer into a tool error."""
    try:
        return path.resolve()
    except OSError as exc:
        msg = f"{format_path_for_display(path)} could not be resolved: {exc}"
        raise UnreadableDocError(msg, source=path) from exc


def _escapes_by_symlink(path: Path, root: Path) -> bool:
    """Report whether a project-shaped path leaves the project root once resolved.

    Both ends of a link are judged here. For a target, the lexical pass settles the destination
    as written and this settles where the filesystem actually sends it; both are needed, since a
    symlink is invisible to the first and the second alone would let ``..`` be walked out and
    back before anyone looked. For a source, selection is lexical, so this is the whole
    containment story.

    An in-project symlink stays legitimate, because only its resolved location is judged. One
    that leaves is refused rather than followed, and it is judged before the file is opened, so
    no outside file is read -- neither to answer a fragment nor to harvest link destinations the
    diagnostics would then quote back.

    Args:
        path: The candidate source or lexically contained target path.
        root: The project root, already resolved by the caller so that a checkout reached
            through a symlinked parent does not read as escaping.

    Returns:
        True when the resolved path lies outside the project root.
    """
    return not _resolved(path).is_relative_to(root)


def _stat_mode(path: Path) -> int | None:
    """Return a path's mode following symlinks, None when absent, or a tool error otherwise.

    ``Path.exists`` is not a stable predicate across the supported interpreters: 3.13 re-raises
    an ``OSError`` outside its ignored set while 3.14 answers False for every one, so a target in
    a directory this process cannot search would be "does not exist" on one and a traceback on
    the other. ``stat`` raises on both, and the decision is made here instead.
    """
    try:
        return path.stat().st_mode
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return None
        msg = f"{format_path_for_display(path)} could not be inspected: {exc}"
        raise UnreadableDocError(msg, source=path) from exc
```

5h. Replace `_link_message` with (only the target half changes; the docstring stays):

```python
def _link_message(
    link: Link,
    document: Path,
    root: Path,
    cache: dict[Path, frozenset[str]],
) -> str | None:
    """(keep the existing docstring, with ``repo_root`` renamed to ``root``)"""
    href = link.href
    parts = _split_destination(href)
    if parts is None or _is_out_of_scope(parts):
        return None
    # The plain-source view drops the fragment from validation but not the target from
    # existence checking: the file still has to be there for the view to render it.
    fragment = "" if _selects_plain_view(parts.query) else unquote(parts.fragment)
    if not parts.path:
        return _fragment_message(fragment, document, root, cache) if fragment else None
    target = _resolve_target(parts.path, document, root)
    if target is None:
        return f"link target {format_path_for_display(href)} does not resolve inside the repository"
    displayed = format_path_for_display(href)
    if _escapes_by_symlink(target, root):
        return f"link target {displayed} leaves the repository through a symlink"
    mode = _stat_mode(target)
    if mode is None:
        return f"link target {displayed} does not exist"
    renders_as_markdown = target.suffix.lower() in _MARKDOWN_TARGET_SUFFIXES
    if not fragment or not renders_as_markdown or not stat.S_ISREG(mode):
        return None
    return _fragment_message(fragment, target, root, cache)
```

Rename the `repo_root` parameter to `root` in `_fragment_message` and `_resolve_target` too, and update their bodies accordingly.

5i. Replace `check_repository_links` with:

```python
def check_links(project_root: Path, sources: Sequence[Path]) -> list[LinkFinding]:
    """Return one finding per unresolvable link, raw anchor, or unreadable source.

    The sources are normally what ``select_link_sources`` returned, but this upholds its own
    contract whatever the caller passed: the list is sorted (a copy, the input is untouched) and
    every source is containment-checked immediately before it is read.

    Args:
        project_root: The project root every source and target must stay inside.
        sources: Unresolved paths under ``project_root``, one per document to check.

    Returns:
        Findings in document order, sources sorted by path. Within a document, link findings
        come first, then raw HTML anchors, and the two are stable-sorted by line, so a link and an
        anchor on one line keep that relative order. A source that leaves the project root, or
        that will not decode or parse, carries no line, since all of those are refused before
        their destinations are read. An empty list means every relative destination and heading
        fragment resolves.

    Raises:
        UnreadableDocError: If the filesystem refused to resolve, inspect, or read a source or a
            link target.
        ValueError: If a source is not lexically under ``project_root``, which is a caller bug
            rather than a document defect.
    """
    root = project_root.resolve()
    cache: dict[Path, frozenset[str]] = {}
    findings: list[LinkFinding] = []
    for document in sorted(sources):
        relative = document.relative_to(root).as_posix()
        if _escapes_by_symlink(document, root):
            # Refused before the read, so an outside file is neither decoded nor quoted back
            # through a diagnostic. Reported rather than skipped, because a configured source
            # nobody checks is the silent green this gate exists to prevent.
            findings.append(LinkFinding(relative, None, ESCAPING_SOURCE_MESSAGE))
            continue
        try:
            text = document.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(LinkFinding(relative, None, UNPARSEABLE_SOURCE_MESSAGE))
            continue
        except OSError as exc:
            msg = f"link source {format_path_for_display(document)} could not be read: {exc}"
            raise UnreadableDocError(msg, source=document) from exc
        try:
            # One parse per document: both kinds of finding read the same token stream. A
            # character reference wider than the interpreter's integer-conversion limit makes
            # the parser raise ValueError rather than answer; reported and stepped over, so one
            # document's content cannot end the run and leave every later document unchecked.
            tokens = _PARSER.parse(text)
            links, anchors = _links_in(tokens), _anchor_lines_in(tokens)
        except ValueError:
            findings.append(LinkFinding(relative, None, UNPARSEABLE_SOURCE_MESSAGE))
            continue
        found: list[tuple[int, str]] = []
        for link in links:
            message = _link_message(link, document, root, cache)
            if message is not None:
                found.append((link.line, message))
        found.extend((line, HTML_ANCHOR_MESSAGE) for line in anchors)
        # Stable sort, so a link and an anchor reported on one line keep the order they were
        # collected in and the two kinds of finding interleave by line rather than by kind.
        found.sort(key=lambda entry: entry[0])
        findings.extend(LinkFinding(relative, line, message) for line, message in found)
    return findings
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_check.py -q`
Expected: all PASS. If `test_the_target_inventory_is_the_engines_full_heading_inventory` fails on the `#nested` id, check what `full_heading_inventory` records for a list-item heading and adjust the fixture rather than the engine; the inventory is the authority.

- [ ] **Step 7: Lint, type-check, conventions, commit**

```bash
uv run --group dev ruff check src tests && uv run --group dev ruff format src tests && uv run --group dev ty check src
uv run --group dev python scripts/check_typing_boundaries.py src
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_conventions.py tests/test_path_display_contract.py -q
git add src/doc_lattice/link_check.py tests/test_link_check.py
git commit -m "feat(links): move the link checker into the package as a typed engine"
```

---

### Task 4: Selector expansion (`select_link_sources`)

**Files:**
- Modify: `src/doc_lattice/link_check.py`
- Test: `tests/test_link_check.py`

**Interfaces:**
- Produces: `select_link_sources(project_root: Path, selectors: Sequence[str]) -> list[Path]`: sorted by project-relative POSIX string, lexically deduplicated, contained aliases deduplicated by resolved target (first in sorted order kept), escaping spellings retained. Raises `ConfigError` for a malformed selector, a selector matching nothing, or a directory the walk cannot scan; raises `UnreadableDocError` for a contained candidate that is not, or does not resolve to, a regular file.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_link_check.py`)

```python
from doc_lattice.error_types import ConfigError  # move to the import block at the top
from doc_lattice.link_check import select_link_sources  # add to the import block at the top


def _relative(root: Path, sources: list[Path]) -> list[str]:
    return [path.relative_to(root.resolve()).as_posix() for path in sources]


def test_a_recursive_selector_matches_at_every_depth_and_a_root_one_only_at_the_root(tmp_path):
    _write(tmp_path, "README.md", "# R\n")
    _write(tmp_path, "docs/a.md", "# A\n")
    _write(tmp_path, "docs/deep/b.md", "# B\n")
    _write(tmp_path, "docs/notes.txt", "x\n")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["docs/**/*.md"])) == [
        "docs/a.md",
        "docs/deep/b.md",
    ]
    assert _relative(tmp_path, select_link_sources(tmp_path, ["*.md"])) == ["README.md"]


def test_selection_is_sorted_and_lexically_deduplicated_across_selectors(tmp_path):
    _write(tmp_path, "b.md", "# b\n")
    _write(tmp_path, "a.md", "# a\n")
    _write(tmp_path, "docs/c.md", "# c\n")

    selected = select_link_sources(tmp_path, ["docs/**/*.md", "*.md", "b.md", "**/*.md"])

    assert _relative(tmp_path, selected) == ["a.md", "b.md", "docs/c.md"]


def test_every_selector_must_match_at_least_one_path(tmp_path):
    _write(tmp_path, "ARCHITECTURE.md", "# A\n")

    with pytest.raises(ConfigError, match=r"docs/\*\*/\*\.md"):
        select_link_sources(tmp_path, ["ARCHITECTURE.md", "docs/**/*.md"])


def test_a_directory_never_satisfies_a_selector(tmp_path):
    (tmp_path / "docs").mkdir()

    with pytest.raises(ConfigError, match="matches no file"):
        select_link_sources(tmp_path, ["docs"])


def test_a_malformed_selector_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="backslash"):
        select_link_sources(tmp_path, ["docs\\a.md"])


def test_matching_is_case_sensitive(tmp_path):
    _write(tmp_path, "README.MD", "# R\n")

    with pytest.raises(ConfigError):
        select_link_sources(tmp_path, ["*.md"])


def test_hidden_directories_get_no_special_treatment(tmp_path):
    _write(tmp_path, ".hidden/a.md", "# a\n")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["**/*.md"])) == [".hidden/a.md"]


def test_a_trailing_recursive_segment_matches_every_file_beneath(tmp_path):
    _write(tmp_path, "docs/a.md", "# a\n")
    _write(tmp_path, "docs/deep/b.txt", "b\n")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["docs/**"])) == [
        "docs/a.md",
        "docs/deep/b.txt",
    ]


def test_contained_aliases_are_deduplicated_by_resolved_target_keeping_the_first_sorted(tmp_path):
    _write(tmp_path, "real.md", "# r\n")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["*.md"])) == ["alias.md"]


def test_an_escaping_symlink_is_selected_and_then_reported_by_the_checker(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# o\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    _write(tmp_path, "ok.md", "# ok\n")

    selected = select_link_sources(tmp_path, ["*.md"])

    assert _relative(tmp_path, selected) == ["escape.md", "ok.md"]
    assert check_links(tmp_path, selected) == [
        LinkFinding("escape.md", None, link_check_module.ESCAPING_SOURCE_MESSAGE)
    ]


def test_a_symlinked_directory_is_never_entered(tmp_path):
    _write(tmp_path, "real/a.md", "# a\n")
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

    assert _relative(tmp_path, select_link_sources(tmp_path, ["**/*.md"])) == ["real/a.md"]
    with pytest.raises(ConfigError, match="matches no file"):
        select_link_sources(tmp_path, ["linked/*.md"])


def test_a_symlink_to_a_directory_matched_as_a_leaf_is_a_tool_error(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "linked.md").symlink_to(tmp_path / "real", target_is_directory=True)

    with pytest.raises(UnreadableDocError, match="not a regular file"):
        select_link_sources(tmp_path, ["*.md"])


def test_a_dangling_symlink_is_a_tool_error(tmp_path):
    (tmp_path / "gone.md").symlink_to(tmp_path / "nowhere.md")

    with pytest.raises(UnreadableDocError, match="gone.md"):
        select_link_sources(tmp_path, ["*.md"])


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are a POSIX shape")
def test_a_special_file_is_a_tool_error_without_being_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe.md")

    with pytest.raises(UnreadableDocError, match="not a regular file"):
        select_link_sources(tmp_path, ["*.md"])


@_requires_permission_enforcement
def test_an_unscannable_directory_is_a_config_error(tmp_path):
    _write(tmp_path, "docs/a.md", "# a\n")
    locked = tmp_path / "docs"
    locked.chmod(0)
    try:
        with pytest.raises(ConfigError, match="could not scan"):
            select_link_sources(tmp_path, ["docs/**/*.md"])
    finally:
        locked.chmod(0o755)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_check.py -q -k "select or selector or symlink or special or unscannable or hidden or recursive"`
Expected: FAIL with `ImportError: cannot import name 'select_link_sources'`

- [ ] **Step 3: Implement the walk** (append to `src/doc_lattice/link_check.py`; the imports were added in Task 3 step 5b)

```python
def select_link_sources(project_root: Path, selectors: Sequence[str]) -> list[Path]:
    """Expand the ``link_sources`` selectors into the documents the gate will check.

    Each selector is expanded lexically by a no-follow walk from the project root. The matches
    are unioned, sorted by project-relative POSIX spelling, and then judged in that order:
    a spelling that resolves outside the project root is kept, so ``check_links`` reports every
    bad configured source; a contained spelling has to be, or resolve to, a regular file, and
    aliases of one file are collapsed onto the first spelling in sorted order. YAML order,
    overlapping selectors, and filesystem order therefore cannot change what is returned.

    Args:
        project_root: The project root the selectors are relative to.
        selectors: The ``link_sources`` entries, already validated by config load or not.

    Returns:
        Unresolved paths under the resolved project root, sorted and deduplicated.

    Raises:
        ConfigError: If a selector is malformed, matches no lexical path, or the walk meets a
            directory it cannot scan.
        UnreadableDocError: If a contained match is a dangling symlink, a directory reached
            through a symlink, or a special file. None of those is opened: the classification is
            a ``stat``, because opening a FIFO or a device can block indefinitely.
    """
    root = project_root.resolve()
    matched: set[str] = set()
    for entry in selectors:
        try:
            segments = validate_link_selector(entry)
        except ValueError as exc:
            msg = f"link_sources entry {format_path_for_display(entry)} {exc}"
            raise ConfigError(msg) from exc
        found: set[str] = set()
        _walk(root, "", segments, 0, found)
        if not found:
            msg = (
                f"link_sources entry {format_path_for_display(entry)} matches no file under "
                f"the project root {format_path_for_display(root)}; the links command refuses "
                "to run over a selector that selects nothing"
            )
            raise ConfigError(msg)
        matched.update(found)
    seen: set[Path] = set()
    sources: list[Path] = []
    for relative in sorted(matched):
        candidate = root.joinpath(*relative.split(SELECTOR_SEPARATOR))
        resolved = _resolved(candidate)
        if not resolved.is_relative_to(root):
            sources.append(candidate)
            continue
        _require_regular_file(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        sources.append(candidate)
    return sources


def _require_regular_file(candidate: Path) -> None:
    """Refuse a contained match that is not, or does not resolve to, a regular file."""
    mode = _stat_mode(candidate)
    if mode is None:
        msg = f"link source {format_path_for_display(candidate)} is a symlink to nothing"
        raise UnreadableDocError(msg, source=candidate)
    if not stat.S_ISREG(mode):
        msg = f"link source {format_path_for_display(candidate)} is not a regular file"
        raise UnreadableDocError(msg, source=candidate)


def _scan(directory: Path) -> list[os.DirEntry[str]]:
    """List one directory, turning a scan the filesystem refuses into a config error."""
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError as exc:
        msg = f"link_sources selection could not scan {format_path_for_display(directory)}: {exc}"
        raise ConfigError(msg) from exc


def _is_directory(entry: os.DirEntry[str]) -> bool:
    """Report whether an entry is a directory in its own right, never through a symlink."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError as exc:
        msg = f"link_sources selection could not inspect {format_path_for_display(entry.path)}: {exc}"
        raise ConfigError(msg) from exc


def _join(prefix: str, name: str) -> str:
    return name if prefix == "" else f"{prefix}{SELECTOR_SEPARATOR}{name}"


def _walk(
    directory: Path, prefix: str, segments: tuple[str, ...], index: int, found: set[str]
) -> None:
    """Match ``segments[index:]`` beneath one directory, collecting project-relative spellings.

    A directory is a traversal node and never a match; only a non-directory entry can satisfy
    the last segment. ``**`` matches zero or more directories, so it is tried against the
    remaining segments here before descending with itself still current.
    """
    segment = segments[index]
    last = index == len(segments) - 1
    entries = _scan(directory)
    if segment == RECURSIVE_SEGMENT:
        if not last:
            _walk(directory, prefix, segments, index + 1, found)
        for entry in entries:
            if _is_directory(entry):
                _walk(Path(entry.path), _join(prefix, entry.name), segments, index, found)
            elif last:
                found.add(_join(prefix, entry.name))
        return
    for entry in entries:
        if not segment_matches(entry.name, segment):
            continue
        if last:
            if not _is_directory(entry):
                found.add(_join(prefix, entry.name))
        elif _is_directory(entry):
            _walk(Path(entry.path), _join(prefix, entry.name), segments, index + 1, found)
```

- [ ] **Step 4: Run the whole engine suite**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_check.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
uv run --group dev ruff check src tests && uv run --group dev ruff format src tests && uv run --group dev ty check src
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_conventions.py tests/test_path_display_contract.py -q
git add src/doc_lattice/link_check.py tests/test_link_check.py
git commit -m "feat(links): expand link_sources with a no-follow walk that fails closed"
```

---

### Task 5: CLI plumbing: format domain, exact stderr writer, annotation line

**Files:**
- Modify: `src/doc_lattice/constants.py` (after `ReportFormat`, line 114), `src/doc_lattice/cli/options.py`, `src/doc_lattice/cli/runtime.py` (after `write_stdout`, line 343), `src/doc_lattice/cli/github.py` (`Annotation`, `github_annotation`, `write_annotations`)
- Test: `tests/cli/test_github.py`, `tests/cli/test_runtime.py`

**Interfaces:**
- Produces: `constants.LinkReportFormat = Literal["human", "github"]`, `constants.VALID_LINK_REPORT_FORMATS`; `options.LinkFormatOpt`; `CliRuntime.write_stderr(text: str, *, newline: bool = True) -> None`; `Annotation.line: int | None = None` (declared after `severity`); `github_annotation(..., severity="error", line: int | None = None)` emitting `,line=N` right after the `file=` property only when `line` is not None.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_github.py`:

```python
def test_github_annotation_emits_a_line_only_when_given_one(tmp_path: Path):
    without = github_annotation(tmp_path / "doc.md", tmp_path, "title", "message")
    with_line = github_annotation(tmp_path / "doc.md", tmp_path, "title", "message", line=7)

    assert without == "::error file=doc.md,title=title::message"
    assert with_line == "::error file=doc.md,line=7,title=title::message"


def test_write_annotations_forwards_each_items_line(runtime: CliRuntime, tmp_path: Path):
    write_annotations(
        runtime,
        [
            Annotation(tmp_path / "a.md", "t", "m", line=3),
            Annotation(tmp_path / "b.md", "t", "m", "warning"),
        ],
    )

    assert _contents(runtime.stdout) == (
        "::error file=a.md,line=3,title=t::m\n::warning file=b.md,title=t::m\n"
    )
```

Append to `tests/cli/test_runtime.py` (the module already defines `_RefusingStream`, `_runtime`, and imports `CliConsole`, `replace`, `StringIO`):

```python
def test_write_stderr_writes_exact_bytes_without_rich(tmp_path: Path):
    stderr = StringIO()
    runtime = _runtime(StringIO(), stderr, tmp_path, no_color=True)

    runtime.write_stderr("'[bold]x[/bold].md':3: message")

    assert stderr.getvalue() == "'[bold]x[/bold].md':3: message\n"


def test_write_stderr_answers_a_departed_reader_in_place(tmp_path: Path):
    # The stderr half of AD-40: the console is silenced, nothing is raised, and the caller's
    # own exit path continues, so a human finding still earns exit 1 with nobody reading it.
    stream = _RefusingStream(BrokenPipeError(errno.EPIPE, "Broken pipe"))
    console = CliConsole(file=stream, stderr=True, no_color=True, color_system=None)
    runtime = replace(_runtime(StringIO(), StringIO(), tmp_path, no_color=True), stderr=console)

    runtime.write_stderr("a finding nobody will read")

    assert console.quiet is True
```

Append to `tests/test_constants.py` if that module exists, else skip this test:

```python
def test_link_report_formats_are_the_report_formats_minus_json():
    from doc_lattice.constants import VALID_LINK_REPORT_FORMATS, VALID_REPORT_FORMATS

    assert VALID_LINK_REPORT_FORMATS == VALID_REPORT_FORMATS - {"json"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/cli/test_github.py tests/cli/test_runtime.py -q -k "line or write_stderr"`
Expected: FAIL (`TypeError` for the `line` keyword, `AttributeError` for `write_stderr`)

- [ ] **Step 3: Implement**

`src/doc_lattice/constants.py`, after `VALID_REPORT_FORMATS`:

```python
# The link gate's output formats: the report formats minus json. Nothing asked for a JSON
# schema of link findings, and declaring one is a contract to own, so the command takes the
# annotating format its generated workflow runs and the human one its hook runs.
LinkReportFormat = Literal["human", "github"]
VALID_LINK_REPORT_FORMATS: frozenset[str] = frozenset(get_args(LinkReportFormat))
```

`src/doc_lattice/cli/options.py`, append:

```python
LinkFormatOpt = Annotated[str, typer.Option("--format", help="human or github.")]
```

`src/doc_lattice/cli/runtime.py`, after `write_stdout`:

```python
    def write_stderr(self, text: str, *, newline: bool = True) -> None:
        """Write exact text to the captured stderr stream, bypassing Rich.

        The stderr analogue of ``write_stdout``, for a diagnostic line that must reach the
        reader byte for byte: a link finding carries a filename, and a filename shaped like Rich
        markup must not become styling. The broken-pipe answer is the stderr half of AD-40,
        applied here directly because no Rich hook governs a raw stream write: the console is
        silenced and its descriptor neutralized, nothing is raised, and the caller's own exit
        code stands. Only a stdout that refuses a write reaches the silent 141.

        Args:
            text: Text to write without Rich rendering.
            newline: Whether to append one newline after ``text``.
        """
        try:
            self.stderr.file.write(text)
            if newline:
                self.stderr.file.write("\n")
            self.stderr.file.flush()
        except BrokenPipeError:
            apply_broken_pipe_policy(self.stderr)
```

`src/doc_lattice/cli/github.py`:

```python
@dataclass(frozen=True, slots=True)
class Annotation:
    """(keep the existing docstring, then add:)

    ``line`` is declared last and defaulted for the same reason ``severity`` is: every
    existing site constructs an annotation positionally through ``severity``, and a
    document-level link finding has no line to give. GitHub attaches a line-less annotation at
    line 1, which is the closest representation workflow commands allow.
    """

    path: Path
    title: str
    message: str
    severity: AnnotationSeverity = "error"
    line: int | None = None


def github_annotation(
    path: Path,
    root: Path,
    title: str,
    message: str,
    severity: AnnotationSeverity = "error",
    line: int | None = None,
) -> str:
    """(keep the existing docstring; add to Args:)
        line: The 1-based line to attach at, or None to omit the property.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    position = "" if line is None else f",line={line}"
    return (
        f"::{severity} file={escape_github_property(str(relative))}{position},"
        f"title={escape_github_property(title)}::{escape_github_message(message)}"
    )
```

In `write_annotations`, pass `line=item.line` after `item.severity`.

- [ ] **Step 4: Run the affected suites**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/cli/test_github.py tests/cli/test_runtime.py tests/cli/test_lint.py tests/cli/test_check.py tests/test_path_display_contract.py -q`
Expected: all PASS (existing annotation bytes unchanged)

- [ ] **Step 5: Commit**

```bash
uv run --group dev ruff check src tests && uv run --group dev ruff format src tests && uv run --group dev ty check src
git add src/doc_lattice/constants.py src/doc_lattice/cli/options.py src/doc_lattice/cli/runtime.py src/doc_lattice/cli/github.py tests/cli/test_github.py tests/cli/test_runtime.py tests/test_constants.py
git commit -m "feat(cli): exact stderr writer, optional annotation line, link format domain"
```

---

### Task 6: The `links` command adapter

**Files:**
- Create: `src/doc_lattice/cli/commands/links.py`
- Modify: `src/doc_lattice/cli/application.py` (import and `register_links(application)` after `register_init`)
- Test: `tests/cli/test_links.py`

**Interfaces:**
- Consumes: `select_link_sources`, `check_links`, `LinkFinding` (Tasks 3-4); `VALID_LINK_REPORT_FORMATS`, `LinkFormatOpt`, `write_stderr`, `Annotation(line=)` (Task 5); `ProjectConfig.config_path` (Task 2).
- Produces: `register_links(app)`, `ANNOTATION_TITLE = "doc-lattice links"`, `format_finding(finding: LinkFinding) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
"""CLI integration tests for the links command."""

import errno
import os
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from doc_lattice.cli import app
from doc_lattice.cli.application import create_app
from doc_lattice.cli.pipe_policy import PipeClosed
from doc_lattice.cli.runtime import CliConsole, CliRuntime
from doc_lattice.config import load_config
from doc_lattice.orchestrate import load_lattice

from .helpers import runner

_requires_permission_enforcement = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX filesystem that enforces modes"
)


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _config(root: Path, *selectors: str) -> None:
    listed = ", ".join(f"'{selector}'" for selector in selectors)
    _write(root, ".doc-lattice.yml", f"lattice_format: 2\nlink_sources: [{listed}]\n")


def _witness(root: Path) -> None:
    """One dead fragment and one dead relative path, in one source."""
    _config(root, "*.md")
    _write(root, "README.md", "# Readme\n\n[a](GUIDE.md#nope)\n\n[b](MISSING.md)\n")
    _write(root, "GUIDE.md", "# Guide\n")


class _RefusingStream(StringIO):
    def __init__(self, error: OSError) -> None:
        super().__init__()
        self._error = error

    def write(self, _text: str) -> int:
        raise self._error


def _runtime(stdout: Console, stderr: Console, cwd: Path) -> CliRuntime:
    return CliRuntime(
        stdout=stdout, stderr=stderr, cwd=cwd, load_config=load_config, load_lattice=load_lattice
    )


def test_links_fails_with_both_witness_messages_on_stderr(tmp_path: Path, monkeypatch):
    _witness(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == (
        "'README.md':3: fragment '#nope' matches no heading in 'GUIDE.md'\n"
        "'README.md':5: link target 'MISSING.md' does not exist\n"
    )


def test_links_is_silent_and_exits_0_when_clean(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md")
    _write(tmp_path, "README.md", "# Readme\n\n[g](GUIDE.md#guide)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")


def test_links_honors_a_configured_set_that_is_not_the_root(tmp_path: Path, monkeypatch):
    # The consumer-shaped fixture: sources live under spec/, and the root README carries a dead
    # link nobody asked the gate to check. The old hardcoded root selection is gone.
    _config(tmp_path, "spec/**/*.md")
    _write(tmp_path, "README.md", "# Readme\n\n[dead](MISSING.md)\n")
    _write(tmp_path, "spec/a.md", "# A\n\n[b](deep/b.md#b)\n")
    _write(tmp_path, "spec/deep/b.md", "# B\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["links"]).exit_code == 0

    _write(tmp_path, "spec/deep/b.md", "# Renamed\n")
    result = runner.invoke(app, ["links"])
    assert result.exit_code == 1
    assert result.stderr == "'spec/a.md':3: fragment '#b' matches no heading in 'spec/deep/b.md'\n"


def test_links_prints_a_markup_shaped_filename_literally(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md")
    _write(tmp_path, "[bold]red[/bold].md", "# X\n\n[m](MISSING.md)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert result.stderr.startswith("'[bold]red[/bold].md':3: ")
    assert "\x1b" not in result.stderr


def test_links_displays_a_control_byte_in_a_filename_as_its_escape(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md")
    _write(tmp_path, "esc\x1b.md", "# X\n\n[m](MISSING.md)\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 1
    assert "\x1b" not in result.stderr
    assert result.stderr.startswith("'esc\\x1b.md':3: ")


def test_links_exits_2_under_zero_config_naming_the_missing_file_and_key(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert result.stderr.startswith("error (CONFIG_ERROR): ")
    assert "no .doc-lattice.yml" in result.stderr
    assert "link_sources" in result.stderr


def test_links_exits_2_when_the_config_declares_no_link_sources(tmp_path: Path, monkeypatch):
    _write(tmp_path, ".doc-lattice.yml", "lattice_format: 2\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert ".doc-lattice.yml" in result.stderr
    assert "declares no link_sources" in result.stderr


def test_links_exits_2_naming_the_selector_that_matched_nothing(tmp_path: Path, monkeypatch):
    _config(tmp_path, "*.md", "docs/**/*.md")
    _write(tmp_path, "README.md", "# R\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links"])

    assert result.exit_code == 2
    assert "'docs/**/*.md'" in result.stderr
    assert "matches no file" in result.stderr


@_requires_permission_enforcement
def test_links_exits_2_on_a_directory_it_cannot_scan(tmp_path: Path, monkeypatch):
    _config(tmp_path, "docs/**/*.md")
    _write(tmp_path, "docs/a.md", "# a\n")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").chmod(0)
    try:
        result = runner.invoke(app, ["links"])
    finally:
        (tmp_path / "docs").chmod(0o755)

    assert result.exit_code == 2
    assert "could not scan" in result.stderr


def test_links_github_format_annotates_each_finding_on_stdout(tmp_path: Path, monkeypatch):
    _witness(tmp_path)
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# o\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["links", "--format", "github"])

    assert result.exit_code == 1
    assert result.stdout == (
        "::error file=README.md,line=3,title=doc-lattice links::"
        "fragment '#nope' matches no heading in 'GUIDE.md'\n"
        "::error file=README.md,line=5,title=doc-lattice links::"
        "link target 'MISSING.md' does not exist\n"
        "::error file=escape.md,title=doc-lattice links::"
        "link source leaves the project root through a symlink\n"
    )
    assert result.stderr == ""


def test_links_rejects_json_and_indent(tmp_path: Path, monkeypatch):
    _witness(tmp_path)
    monkeypatch.chdir(tmp_path)

    rejected = runner.invoke(app, ["links", "--format", "json"])
    assert rejected.exit_code == 2
    assert "must be one of: github, human" in rejected.stderr

    unknown = runner.invoke(app, ["links", "--indent", "2"])
    assert unknown.exit_code == 2
    assert "No such option" in unknown.stderr


def test_links_help_names_only_its_two_formats(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["links", "--help"])

    assert result.exit_code == 0
    assert "human or github." in result.stdout
    assert "json" not in result.stdout


def test_links_github_output_to_a_departed_reader_raises_pipe_closed(tmp_path: Path):
    _witness(tmp_path)
    stdout = CliConsole(
        file=_RefusingStream(BrokenPipeError(errno.EPIPE, "Broken pipe")),
        no_color=True,
        color_system=None,
    )
    stderr = CliConsole(file=StringIO(), stderr=True, no_color=True, color_system=None)
    application = create_app(runtime_factory=lambda *, no_color: _runtime(stdout, stderr, tmp_path))

    result = runner.invoke(application, ["links", "--format", "github"])

    assert isinstance(result.exception, PipeClosed)


def test_links_human_findings_to_a_departed_stderr_keep_exit_1(tmp_path: Path):
    _witness(tmp_path)
    stdout = CliConsole(file=StringIO(), no_color=True, color_system=None)
    stderr = CliConsole(
        file=_RefusingStream(BrokenPipeError(errno.EPIPE, "Broken pipe")),
        stderr=True,
        no_color=True,
        color_system=None,
    )
    application = create_app(runtime_factory=lambda *, no_color: _runtime(stdout, stderr, tmp_path))

    result = runner.invoke(application, ["links"])

    assert result.exit_code == 1
    assert stderr.quiet is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/cli/test_links.py -q`
Expected: FAIL (`No such command 'links'`)

- [ ] **Step 3: Write the adapter**

`src/doc_lattice/cli/commands/links.py`:

```python
"""Typer adapter for the Markdown link gate."""

from pathlib import Path

import typer

from ...config import DEFAULT_CONFIG_NAME, ProjectConfig
from ...constants import VALID_LINK_REPORT_FORMATS
from ...error_types import ConfigError
from ...link_check import LinkFinding, check_links, select_link_sources
from ...path_utils import format_path_for_display
from ..errors import EXIT_FINDING, exit_on_project_error
from ..github import Annotation, write_annotations
from ..options import ConfigOpt, LinkFormatOpt
from ..output import select_output
from ..runtime import CliRuntime, get_runtime

ANNOTATION_TITLE = "doc-lattice links"


def _require_link_sources(project: ProjectConfig) -> list[str]:
    """Return the configured selectors, refusing an empty set as a config error.

    Empty and omitted are one case, and both are refused rather than defaulted: the generated
    hook and workflow run this command unconditionally, and a default would let that mandatory
    gate pass over zero files. The message says which of the two shapes it met, because "your
    config lacks a key" and "you have no config" call for different edits.
    """
    if project.config.link_sources:
        return project.config.link_sources
    if project.config_path is None:
        msg = (
            f"no {DEFAULT_CONFIG_NAME} was found in "
            f"{format_path_for_display(project.project_root)}, and the links command requires "
            "a link_sources list naming the files to check; write one, for example "
            "link_sources: ['docs/**/*.md']"
        )
    else:
        msg = (
            f"config {format_path_for_display(project.config_path)} declares no link_sources, "
            "and the links command requires at least one selector; add link_sources: "
            "['docs/**/*.md'] or whichever files you want checked"
        )
    raise ConfigError(msg)


def format_finding(finding: LinkFinding) -> str:
    """Render one finding as the ``'path'[:line]: message`` line the hook prints.

    Args:
        finding: The finding to render.

    Returns:
        The line, with the path in the display spelling and no trailing newline.
    """
    if finding.line is None:
        return f"{format_path_for_display(finding.path)}: {finding.message}"
    return f"{format_path_for_display(finding.path)}:{finding.line}: {finding.message}"


def _annotations(project_root: Path, findings: list[LinkFinding]) -> list[Annotation]:
    return [
        Annotation(project_root / finding.path, ANNOTATION_TITLE, finding.message, line=finding.line)
        for finding in findings
    ]


def _write_findings(runtime: CliRuntime, findings: list[LinkFinding]) -> None:
    for finding in findings:
        runtime.write_stderr(format_finding(finding))


def register_links(app: typer.Typer) -> None:
    """Register the ``links`` command on an application.

    Args:
        app: Typer application receiving the command.
    """

    @app.command()
    def links(
        ctx: typer.Context,
        config: ConfigOpt = None,
        fmt: LinkFormatOpt = "human",
    ) -> None:
        """Validate relative links and heading fragments; exit 1 on a finding, 2 on tool error."""
        runtime = get_runtime(ctx)
        selection = select_output(runtime, fmt=fmt, valid=VALID_LINK_REPORT_FORMATS)
        with exit_on_project_error(runtime, github=selection.annotates):
            project = runtime.project(config)
            selectors = _require_link_sources(project)
            sources = select_link_sources(project.project_root, selectors)
            findings = check_links(project.project_root, sources)
        if selection.annotates:
            write_annotations(runtime, _annotations(project.project_root, findings))
        else:
            # Human findings go to stderr, the channel the script always used, and through the
            # exact writer so a filename shaped like Rich markup stays a filename.
            _write_findings(runtime, findings)
        raise typer.Exit(EXIT_FINDING if findings else 0)
```

`src/doc_lattice/cli/application.py`: add `from .commands.links import register_links` (alphabetically after `linear`) and `register_links(application)` after `register_init(application)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/cli/test_links.py tests/cli/test_contract.py -q`
Expected: all PASS. If a contract test enumerates registered commands against a fixed list, add `links` to that list only where the contract applies (it takes `--config`; it has no `json`/`--indent`; it loads no lattice).

- [ ] **Step 5: Commit**

```bash
uv run --group dev ruff check src tests && uv run --group dev ruff format src tests && uv run --group dev ty check src
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_conventions.py tests/test_path_display_contract.py -q
git add src/doc_lattice/cli/commands/links.py src/doc_lattice/cli/application.py tests/cli/test_links.py tests/cli/test_contract.py
git commit -m "feat(cli): add the links command"
```

---

### Task 7: Dogfood the shipped command and retire the script

**Files:**
- Create: `.doc-lattice.yml`, `tests/test_link_gate_wiring.py`
- Delete: `scripts/check_doc_links.py`, `tests/test_check_doc_links.py`
- Modify: `.pre-commit-config.yaml` (lines 44-51), `.github/workflows/ci.yml` (line 30), `pyproject.toml` (sdist exclude), `tests/test_package_metadata.py` (`_REPOSITORY_ONLY_TESTS`), `scripts/check_version_sync.py` (docstring of `maintained_documents`), `tests/test_check_version_sync.py` (delete the equivalence test), `src/doc_lattice/markdown_compat.py` (lines 339-341), `CLAUDE.md` (lines 47 and 156)

- [ ] **Step 1: Write the wiring tests**

`tests/test_link_gate_wiring.py`:

```python
"""This repository's own link gate runs through the shipped command, in the hook and in CI."""

from pathlib import Path

from workflow_helpers import _commands, _invocations, _load_workflow

from doc_lattice.cli import app

from cli.helpers import runner

_ROOT = Path(__file__).resolve().parents[1]


def _hook_invocations(hook_id: str) -> list[list[str]]:
    config = _load_workflow(_ROOT / ".pre-commit-config.yaml")
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"] if hook.get("id") == hook_id]
    assert len(hooks) == 1, f"expected one {hook_id} hook, found {len(hooks)}"
    hook = hooks[0]
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False
    return _invocations(hook["entry"])


def test_the_pre_commit_hook_runs_the_shipped_links_command():
    invocations = _hook_invocations("doc-lattice-links")

    assert [argv[-2:] for argv in invocations] == [["doc-lattice", "links"]]


def test_the_ci_code_quality_job_runs_the_shipped_links_command_with_annotations():
    # CI enumerates its checks directly and never invokes pre-commit, so the hook alone would
    # leave a renamed heading green on a pull request; annotations are the surface a reviewer
    # sees, so CI alone runs the github format.
    job = _load_workflow(_ROOT / ".github/workflows/ci.yml")["jobs"]["code-quality"]
    invocations = [
        argv for step in job["steps"] for argv in _invocations(_commands(step))
    ]

    assert [argv[-4:] for argv in invocations if "links" in argv] == [
        ["doc-lattice", "links", "--format", "github"]
    ]


def test_the_repository_is_clean_through_the_real_command(monkeypatch):
    # The committed config, its selector, the adapter, the renderer, and the engine composed:
    # exit 0 with nothing on either stream.
    monkeypatch.chdir(_ROOT)

    result = runner.invoke(app, ["links"])

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", "")
```

(If `from cli.helpers import runner` does not resolve under this repository's pytest path configuration, use `from typer.testing import CliRunner` and `runner = CliRunner()` instead; check how `tests/cli/test_lint.py` imports it.)

- [ ] **Step 2: Wire the repository**

Create `.doc-lattice.yml` at the repository root:

```yaml
# doc-lattice configuration. See https://github.com/Guardantix/doc-lattice
lattice_format: 2
docs_roots: []
link_sources:
  - "*.md"
```

In `.pre-commit-config.yaml`, replace the `check-doc-links` hook (keep its comment) with:

```yaml
      - id: doc-lattice-links
        name: doc-lattice links
        entry: uv run --locked --group dev doc-lattice links
        language: system
        always_run: true
        pass_filenames: false
```

In `.github/workflows/ci.yml`, replace `- run: uv run --no-sync python scripts/check_doc_links.py` with:

```yaml
      - run: uv run --no-sync doc-lattice links --format github
```

Delete the script and its old tests:

```bash
git rm scripts/check_doc_links.py tests/test_check_doc_links.py
```

In `pyproject.toml`, replace `"/tests/test_check_doc_links.py",` with `"/tests/test_link_gate_wiring.py",` placed alphabetically (after `test_extract_release_notes.py`). Make the identical replacement in `_REPOSITORY_ONLY_TESTS` in `tests/test_package_metadata.py`.

In `scripts/check_version_sync.py`, replace the `maintained_documents` docstring body with:

```python
    """Return the maintained documents: the sorted root Markdown files.

    Spelled here rather than imported, on purpose: this gate reads install pins and nothing
    else, and importing the link gate's selection would pull the Markdown parser into it. The
    repository's link sources are declared in ``.doc-lattice.yml`` and checked by
    ``doc-lattice links``; this selection is this script's own and is held only by its test.

    Args:
        repo_root: The repository root.

    Returns:
        Every root ``*.md`` file in sorted order. Nested directories are not sources.
    """
```

In `tests/test_check_version_sync.py`, delete `test_maintained_documents_matches_the_doc_links_definition`. Keep `_write_selection_fixture`; it is still used by the selection test above it.

In `src/doc_lattice/markdown_compat.py`, replace lines 339-341 of the `github_ids_for_texts` docstring (`for section identity; ``scripts/check_doc_links.py`` feeds it ... describes.`) with:

```
    for section identity, and ``full_heading_inventory`` allocates through the same slugger for
    the link gate in ``link_check``, whose inventory is deliberately wider than ``Heading``
    describes.
```

In `CLAUDE.md`, replace `uv run --group dev python scripts/check_doc_links.py` (line 47) with `uv run --group dev doc-lattice links`, and on line 156 replace `` `scripts/check_doc_links.py`, `` with `` `doc-lattice links`, ``.

- [ ] **Step 3: Run the wiring tests and everything the move touched**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_link_gate_wiring.py tests/test_package_metadata.py tests/test_check_version_sync.py tests/test_link_check.py -q`
Expected: all PASS

- [ ] **Step 4: Commit (the new hook runs on this commit)**

```bash
uv run --group dev ruff check src tests scripts && uv run --group dev ruff format src tests scripts
uv run --group dev python scripts/check_version_sync.py && uv run --group dev doc-lattice links
git add .doc-lattice.yml .pre-commit-config.yaml .github/workflows/ci.yml pyproject.toml \
  tests/test_package_metadata.py tests/test_link_gate_wiring.py scripts/check_version_sync.py \
  tests/test_check_version_sync.py src/doc_lattice/markdown_compat.py CLAUDE.md
git commit -m "chore: run this repository's link gate through doc-lattice links"
```

---

### Task 8: Generated adopter surfaces and the migration note

**Files:**
- Modify: `src/doc_lattice/scaffold.py`, `src/doc_lattice/cli/commands/init.py`, `CHANGELOG.md` (under `## [Unreleased]`)
- Test: `tests/test_scaffold.py`, `tests/cli/test_init.py`, `tests/test_readme_contract.py`, `README.md` (the mirrored config block only; Task 10 does the prose)

**Interfaces:**
- Produces: `scaffold.RootKind = Literal["file", "directory"]`, `scaffold.selector_for_root(root: str, kind: RootKind) -> str`, `render_config(docs_roots: tuple[str, ...], link_sources: tuple[str, ...], linear_team: str | None) -> str`, `build_scaffold(docs_roots, link_sources, linear_team, version, *, default_branch)`.
- Consumes: `link_selectors.escape_selector_literal`, `path_utils.safe_resolve`, init's `_walk_entry_mode`.

The commit for this task must include the CHANGELOG `### Migration` subsection, because the `check-migration-rule` hook fails on any generated-output diff without it.

- [ ] **Step 1: Write the failing scaffold tests** (append to `tests/test_scaffold.py`; import `selector_for_root` and `render_precommit` from `doc_lattice.scaffold`)

```python
@pytest.mark.parametrize(
    ("root", "kind", "selector"),
    [
        ("docs", "directory", "docs/**/*.md"),
        ("./docs/", "directory", "docs/**/*.md"),
        ("docs//sub/./x", "directory", "docs/sub/x/**/*.md"),
        (".", "directory", "**/*.md"),
        ("SPEC.md", "file", "SPEC.md"),
        ("notes [draft]", "directory", "notes [[]draft]/**/*.md"),
        ("what?.md", "file", "what[?].md"),
        ("missing", "directory", "missing/**/*.md"),
    ],
)
def test_selector_for_root_spells_a_literal_root_as_a_selector(root, kind, selector):
    assert selector_for_root(root, kind) == selector


def test_render_config_writes_link_sources_after_docs_roots():
    text = render_config(("docs",), ("docs/**/*.md",), None)
    assert "docs_roots:\n  - docs\nlink_sources:\n  - docs/**/*.md\n" in text
    assert _load(text).link_sources == ["docs/**/*.md"]


def test_render_config_quotes_a_selector_yaml_would_read_as_an_alias():
    assert _load(render_config((".",), ("**/*.md",), None)).link_sources == ["**/*.md"]


def test_generated_gates_run_links_as_an_always_run_hook_and_an_annotated_ci_step():
    scaffold = build_scaffold(("docs",), ("docs/**/*.md",), None, "0.3.0", default_branch="main")
    hooks = YAML(typ="safe").load(scaffold.precommit_text)[0]["hooks"]
    links_hook = [hook for hook in hooks if hook["id"] == "doc-lattice-links"]

    assert [hook["id"] for hook in hooks] == ["doc-lattice-check", "doc-lattice-lint", "doc-lattice-links"]
    assert links_hook == [
        {
            "id": "doc-lattice-links",
            "name": "doc-lattice links",
            "entry": "uvx --python 3.13 --from doc-lattice==0.3.0 doc-lattice links",
            "language": "system",
            "always_run": True,
            "pass_filenames": False,
        }
    ]
    assert "--from doc-lattice==0.3.0 doc-lattice links --format github\n" in scaffold.ci_text
    assert "rc_links=$?" in scaffold.ci_text
    assert '[ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ] && [ "$rc_links" -eq 0 ]' in scaffold.ci_text
```

Update every existing `render_config((...), None)` call in `tests/test_scaffold.py` to pass a selectors tuple as the second argument (`("docs/**/*.md",)` for the default; for the hypothesis and hostile-root cases pass `(selector_for_root(root, "directory"),)`), every `build_scaffold(("docs",), None, ...)` to `build_scaffold(("docs",), ("docs/**/*.md",), None, ...)`, and extend `test_generated_gates_run_check_and_lint` with `assert "doc-lattice links" in scaffold.precommit_text` and `in scaffold.ci_text`.

In `tests/test_readme_contract.py`, change the config-sample assertion to `render_config(_DEFAULT_DOCS_ROOTS, (selector_for_root("docs", "directory"),), None)` and import `selector_for_root`.

- [ ] **Step 2: Write the failing init tests**

In `tests/cli/test_init.py`, update `_shared_guidance` to insert after the lint hook (before the closing `"\n"`):

```python
        "      # always_run rather than files: \\.md$, because the break links catches is\n"
        "      # cross-document: renaming a heading in one file invalidates a link written in\n"
        "      # another, and the file that changed is not the file that ends up wrong.\n"
        "      - id: doc-lattice-links\n"
        "        name: doc-lattice links\n"
        f"        entry: uvx --python 3.13 --from doc-lattice=={version} "
        "doc-lattice links\n"
        "        language: system\n"
        "        always_run: true\n"
        "        pass_filenames: false\n"
```

and `_legacy_stdout` to insert after `"          rc_lint=$?\n"`:

```python
        f"          uvx --python 3.13 --from doc-lattice=={version} doc-lattice links "
        "--format github\n"
        "          rc_links=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ] && [ "$rc_links" -eq 0 ]\n'
```

(replacing the old final conjunction line). Then append:

```python
def _written_config(tmp_path: Path) -> str:
    return (tmp_path / ".doc-lattice.yml").read_text(encoding="utf-8")


def test_init_derives_link_sources_from_the_default_docs_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert "link_sources:\n  - docs/**/*.md\n" in _written_config(tmp_path)


def test_init_spells_an_existing_file_root_as_itself(tmp_path: Path, monkeypatch):
    (tmp_path / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "SPEC.md"]).exit_code == 0
    assert "link_sources:\n  - SPEC.md\n" in _written_config(tmp_path)


def test_init_uses_the_directory_form_for_a_root_that_does_not_exist_yet(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "./design/"]).exit_code == 0
    assert "link_sources:\n  - design/**/*.md\n" in _written_config(tmp_path)


def test_init_escapes_a_metacharacter_in_a_root(tmp_path: Path, monkeypatch):
    (tmp_path / "notes [draft]").mkdir()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "notes [draft]"]).exit_code == 0
    assert "link_sources:\n  - notes [[]draft]/**/*.md\n" in _written_config(tmp_path)


def test_init_derives_the_selector_from_a_symlinked_roots_resolved_path(tmp_path: Path, monkeypatch):
    # The checker never enters a symlinked directory, so a selector written over the link
    # would fail on every run; the resolved, contained path is what is written.
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--docs-root", "linked"]).exit_code == 0
    assert "link_sources:\n  - real/**/*.md\n" in _written_config(tmp_path)


def test_init_rejects_a_root_that_resolves_outside_the_project(tmp_path: Path, monkeypatch):
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (tmp_path / "away").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "away"])
    _assert_rejected_before_any_write(result, tmp_path)
    assert "resolves outside" in result.stderr


def test_init_rejects_a_root_with_a_backslash(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--docs-root", "docs\\guides"])
    _assert_rejected_before_any_write(result, tmp_path)
    assert "backslash" in result.stderr
```

(`_assert_rejected_before_any_write` expects only `.git` in the directory; if the symlink tests leave `away`/`linked` there, assert on `result.exit_code == 2` and the config's absence instead.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_scaffold.py tests/cli/test_init.py tests/test_readme_contract.py -q`
Expected: FAIL (`selector_for_root` missing; `render_config` arity)

- [ ] **Step 4: Implement the scaffold**

In `src/doc_lattice/scaffold.py`:

Add imports: `from typing import Literal` and `from .link_selectors import escape_selector_literal`.

Add after `PYTHON_PIN`:

```python
# What a docs root turned out to be when init looked, which decides the selector shape written
# for it. A nonexistent root is classified as a directory, because the default root is created
# after init and a directory is what a docs root usually is.
RootKind = Literal["file", "directory"]
_RECURSIVE_MARKDOWN = "**/*.md"


def selector_for_root(root: str, kind: RootKind) -> str:
    """Spell one docs root as the ``link_sources`` selector that covers it.

    The literal is normalized (a leading ``./``, trailing slashes, and interior ``//`` or
    ``/./`` removed) and then escaped, so a root containing ``*``, ``?``, or ``[`` names itself
    rather than becoming a pattern. The project root itself becomes every Markdown file beneath
    it.

    Args:
        root: The root as a project-relative POSIX path. The init adapter passes the resolved,
            contained path for a root that exists and the literal flag for one that does not.
        kind: Whether the root is a file or a directory.

    Returns:
        The file itself for a file root, or ``root/**/*.md`` for a directory root.
    """
    normalized = "/".join(part for part in root.split("/") if part not in ("", "."))
    if normalized == "":
        return _RECURSIVE_MARKDOWN
    literal = escape_selector_literal(normalized)
    return literal if kind == "file" else f"{literal}/{_RECURSIVE_MARKDOWN}"
```

Change `render_config` to:

```python
def render_config(
    docs_roots: tuple[str, ...], link_sources: tuple[str, ...], linear_team: str | None
) -> str:
    """(keep the docstring; add to Args:)
        link_sources: The selectors to write as the active link_sources list, already derived
            from the roots by the caller.
    """
    data: dict[str, int | list[str] | str] = {
        "lattice_format": LATTICE_FORMAT_VERSION,
        "docs_roots": list(docs_roots),
        "link_sources": list(link_sources),
    }
    # (rest unchanged)
```

Change `render_precommit` to emit, after the lint hook's `pass_filenames: false` line:

```python
        "      # always_run rather than files: \\.md$, because the break links catches is\n"
        "      # cross-document: renaming a heading in one file invalidates a link written in\n"
        "      # another, and the file that changed is not the file that ends up wrong.\n"
        "      - id: doc-lattice-links\n"
        "        name: doc-lattice links\n"
        f"        entry: {_invocation(version, 'links')}\n"
        "        language: system\n"
        "        always_run: true\n"
        "        pass_filenames: false\n"
```

and update its docstring to "check, lint, and links".

Change `render_ci`: add `links_cmd = _invocation(version, "links --format github")` and emit after `rc_lint=$?`:

```python
        f"          {links_cmd}\n"
        "          rc_links=$?\n"
        '          [ "$rc_check" -eq 0 ] && [ "$rc_lint" -eq 0 ] && [ "$rc_links" -eq 0 ]\n'
```

(replacing the old final line). Update its docstring: "All three commands run in one shell step ... the final test fails the step if any command failed. ``links`` runs with ``--format github`` because annotations on the pull-request diff are the surface a reviewer sees; ``check`` and ``lint`` keep their plain invocations."

Change `build_scaffold` to take `link_sources: tuple[str, ...]` as its second positional parameter and pass it to `render_config`.

- [ ] **Step 5: Implement the init side**

In `src/doc_lattice/cli/commands/init.py`:

Add imports: `from ...path_utils import format_path_for_display, safe_resolve` (extend the existing line) and `from ...scaffold import RootKind, build_scaffold, render_ci, render_gitignore, render_precommit, selector_for_root`.

In `_validate_init_flags`, inside the `for root in docs_roots:` loop before the absolute/`..` check, add:

```python
        if "\\" in root:
            msg = (
                f"--docs-root {format_path_for_display(root)} contains a backslash, which the "
                "link_sources selector grammar cannot express; spell the path with '/'"
            )
            raise ValidationError(msg)
```

Add a new function after `_validate_init_flags`:

```python
def _link_selectors(docs_roots: tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    """Derive the ``link_sources`` selector for each docs root, from what the root is.

    A root that exists is classified by stat and spelled from its resolved, contained
    project-relative path rather than from the flag: the link gate never enters a symlinked
    directory, so a selector written over the link would fail on every run. A root that does not
    exist yet takes the directory form of its literal spelling.

    Args:
        docs_roots: The validated ``--docs-root`` values.
        cwd: The invocation directory the config is written into.

    Returns:
        One selector per root, in root order.

    Raises:
        ValidationError: If an existing root resolves outside the invocation directory.
        InitPersistenceError: If a root can be neither confirmed nor ruled out.
    """
    project_root = cwd.resolve()
    selectors: list[str] = []
    for root in docs_roots:
        candidate = cwd / root
        mode = _walk_entry_mode(candidate)
        if mode is None:
            selectors.append(selector_for_root(root, "directory"))
            continue
        try:
            resolved = safe_resolve(candidate, project_root)
        except ValueError as exc:
            msg = (
                f"--docs-root {format_path_for_display(root)} resolves outside the project "
                f"directory {format_path_for_display(cwd)}; a link_sources selector cannot "
                "reach it"
            )
            raise ValidationError(msg) from exc
        kind: RootKind = "file" if stat.S_ISREG(mode) else "directory"
        selectors.append(selector_for_root(resolved.relative_to(project_root).as_posix(), kind))
    return tuple(selectors)
```

In the `init` command body's ordinary branch, after `_validate_init_flags(roots, linear_team)`:

```python
                selectors = _link_selectors(roots, runtime.cwd)
                branch, branch_source = _resolve_default_branch(default_branch, runtime.cwd)
                scaffold = build_scaffold(
                    roots, selectors, linear_team, __version__, default_branch=branch
                )
```

`root` and `cwd` are display-guarded names in this module; both interpolations above are wrapped.

- [ ] **Step 6: Update the README config mirror and write the CHANGELOG**

In `README.md`, in the yaml fence under `## Configuration`, insert after `  - docs`:

```yaml
link_sources:
  - docs/**/*.md
```

In `CHANGELOG.md`, replace the `## [Unreleased]` section header block so it reads (keeping the existing `### Changed` entry about `init` naming the Git precondition, placed under the new `### Changed`):

```markdown
## [Unreleased]

### Added

- `doc-lattice links`, the Markdown link gate, shipped as a command. It validates every relative
  link destination and heading fragment in a configured source set, exits 1 on a finding with one
  `'path':line: message` line per finding on stderr, and annotates the pull-request diff under
  `--format github`. The source set is the new `link_sources` config key, a list of
  project-relative POSIX selectors that fails closed: an omitted or empty key, or any selector
  that matches nothing, is a config error rather than a green run. README.md owns the contract
  and ARCHITECTURE.md's AD-45 owns the decisions behind it.

### Changed

- The pre-commit block and the GitHub Actions workflow `init` prints now run `links` beside
  `check` and `lint`: the hook with `always_run` because the break it catches is cross-document,
  and the workflow with `--format github`. The generated config now writes `link_sources`, derived
  from the docs roots.
- The migration guard now snapshots the generated config, so a change to what `init` writes is
  authorized the same way as a change to what it prints.
- (the existing `init` Git-precondition entry, unchanged)

### Migration

Existing installations keep working untouched: `link_sources` is optional for every command but
`links`, and nothing runs `links` until you install the new blocks. To adopt the link gate:

1. Upgrade first. Bump the pinned version in your hook entries and workflow, or upgrade the
   installed tool, before touching the config: the config parser is strict, and a `link_sources`
   key is a schema error to every release before this one.
2. Add `link_sources` to `.doc-lattice.yml`, for example `link_sources: ['docs/**/*.md']`, or
   run `init --print-only` from the new release and copy the key the generated config shows.
   Every selector must match at least one file, or `links` exits 2.
3. Then replace the pre-commit block and the workflow with the ones `init --print-only` prints.
   Copying the blocks before adding the key would make every gated commit exit 2.
```

- [ ] **Step 7: Run the affected suites, the migration guard, and the README contract**

Run:
```bash
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_scaffold.py tests/cli/test_init.py tests/test_readme_contract.py tests/test_check_migration_rule.py tests/test_path_display_contract.py tests/test_conventions.py -q
uv run --group dev python scripts/check_migration_rule.py
uv run --group dev python scripts/check_version_sync.py
uv run --group dev doc-lattice links
```
Expected: all PASS; the migration guard passes because `### Migration` is present under Unreleased (the baseline stays at 7.0.0).

- [ ] **Step 8: Commit**

```bash
uv run --group dev ruff check src tests scripts && uv run --group dev ruff format src tests scripts && uv run --group dev ty check src scripts
git add src/doc_lattice/scaffold.py src/doc_lattice/cli/commands/init.py tests/test_scaffold.py tests/cli/test_init.py tests/test_readme_contract.py README.md CHANGELOG.md
git commit -m "feat(init): generate the links gate and link_sources in every adopter surface"
```

---

### Task 9: Enroll the generated config in the migration guard

**Files:**
- Modify: `scripts/check_migration_rule.py` (import at line 77, `compute_surfaces` at lines 240-261, module docstring "What is covered"), `RELEASING.md` (step 4, lines 51-56)
- Test: `tests/test_check_migration_rule.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_check_migration_rule.py`)

```python
def test_compute_surfaces_snapshots_four_generated_config_shapes():
    surfaces = compute_surfaces(_MANAGED_CI)

    assert surfaces["init.config[default]"].count("link_sources:\n  - docs/**/*.md\n") == 1
    assert "  - SPEC.md\n" in surfaces["init.config[file-root]"]
    assert "  - notes [[]draft]/**/*.md\n" in surfaces["init.config[metacharacter-root]"]
    assert "  - design/**/*.md\n  - lore/**/*.md\n" in surfaces["init.config[multiple-roots]"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_migration_rule.py -q -k config_shapes`
Expected: FAIL with `KeyError: 'init.config[default]'`

- [ ] **Step 3: Implement**

In `scripts/check_migration_rule.py`, change the import to:

```python
from doc_lattice.scaffold import (
    RootKind,
    render_ci,
    render_config,
    render_gitignore,
    render_precommit,
    selector_for_root,
)
```

Add after `CI_BRANCHES`:

```python
# The representative docs-root matrix for the generated config, named by shape. The selector
# derivation has branches for a file root, a metacharacter in a root, and several roots, and one
# snapshot per branch is what makes a change to any of them visible.
CONFIG_ROOTS: dict[str, tuple[tuple[str, RootKind], ...]] = {
    "default": (("docs", "directory"),),
    "file-root": (("SPEC.md", "file"),),
    "metacharacter-root": (("notes [draft]", "directory"),),
    "multiple-roots": (("design", "directory"), ("lore", "directory")),
}
```

In `compute_surfaces`, after the `init.precommit` entry:

```python
    for name, roots in CONFIG_ROOTS.items():
        docs_roots = tuple(root for root, _ in roots)
        selectors = tuple(selector_for_root(root, kind) for root, kind in roots)
        surfaces[f"init.config[{name}]"] = render_config(docs_roots, selectors, None)
```

In the module docstring's "What is covered" section, change "The ``init`` blocks come from the renderers" to "The ``init`` config and blocks come from the renderers", and add a "Config coverage" paragraph after "Branch coverage": "``render_config`` takes the derived ``link_sources`` selectors, so its output is a family too. The snapshot holds one shape per derivation branch: the default root, a file root, a root carrying a selector metacharacter, and several roots. It is an approximation in the same sense the branch matrix is."

In `RELEASING.md` step 4, change "the `.gitignore`, pre-commit, and workflow blocks `init` prints" to "the config `init` writes and the `.gitignore`, pre-commit, and workflow blocks it prints".

- [ ] **Step 4: Run the guard and its tests**

Run:
```bash
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_check_migration_rule.py -q
uv run --group dev python scripts/check_migration_rule.py
uv run --group dev doc-lattice links
```
Expected: tests PASS; the guard passes (new keys diff against the baseline and the Migration subsection authorizes it).

- [ ] **Step 5: Commit**

```bash
uv run --group dev ruff check scripts tests && uv run --group dev ruff format scripts tests && uv run --group dev ty check scripts
git add scripts/check_migration_rule.py tests/test_check_migration_rule.py RELEASING.md
git commit -m "chore(release): snapshot the generated config in the migration guard"
```

---

### Task 10: Documents

**Files:**
- Modify: `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`
- Verify with: `scripts/check_doc_links.py` is gone, so use `uv run --group dev doc-lattice links`, plus `scripts/check_version_sync.py`, `scripts/check_migration_rule.py`, `git diff --check`, `tests/test_readme_contract.py`.

- [ ] **Step 1: README.md**

1a. Commands table (line 249 area): add after the `lint` row:

```markdown
| `links [--format human\|github]` | Validate every relative link destination and heading fragment in the `link_sources` files. | 1 on a finding, 2 on tool error |
```

1b. Replace "`check` and `lint` gate by default, exiting 1 when they find drift or an authority inversion." with "`check`, `lint`, and `links` gate by default, exiting 1 when they find drift, an authority inversion, or a dead link."

1c. Replace "The lattice-loading commands `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear` accept `--config PATH`" with "The lattice-loading commands `check`, `lint`, `impact`, `reconcile`, `graph`, and `linear`, and the link gate `links`, accept `--config PATH`".

1d. Insert a `### links` subsection immediately before `### reconcile` (line 420):

```markdown
### links

`links` is the Markdown link gate, and it is deliberately not part of `check`. `check` is the
lattice edge gate and its green is honest precisely because it makes no claim about Markdown
links; folding link coverage into it would make that green a lie the first time it was wrong.

Over every file `link_sources` selects, `links` reads each Markdown link destination and resolves
the relative ones against the project root: the target must exist inside the project, and a
`#fragment` on a Markdown target must name a heading GitHub assigns that id to. That inventory is
wider than the addressable subset the lattice tracks, on purpose: setext headings, ATX headings
indented one to three spaces, and headings nested in a list item or a block quote all render and
resolve on GitHub, so failing a link to one would fail a correct link. Absolute and external
destinations, image destinations, and the `?plain=1` source view's line fragments are out of
scope and skipped.

Two gaps are declared rather than closed. A destination written as a raw HTML anchor is reported,
not resolved, because an attribute value arrives without the normalization markdown-it applies to
a Markdown destination; write it as a Markdown link. And a heading whose text is itself an inline
link slugs from raw source on both sides, so `## [Guide](target.md)` answers to `#guidetargetmd`
here where GitHub renders `#guide`.

Containment binds both ends. A selected file that leaves the project root through a symlink is
reported rather than read, and a file that will not decode as UTF-8 or that the parser refuses is
reported and stepped over, so one document cannot end the run. A filesystem the gate cannot
inspect, resolve, scan, or open is a tool error: a gate that cannot see its inputs must not pass.

Human findings go to stderr, one `'path':line: message` per finding in document order with no
line for a finding about the document itself, and nothing is printed on success. `--format
github` writes one annotation per finding to stdout instead, at the finding's line, so a dead link
shows on the pull-request diff. The generated workflow runs that form. Exit 1 on any finding, 2
when `link_sources` is missing or empty, a selector matches nothing, or the filesystem refuses the
gate, and 0 otherwise.
```

1e. In `## Configuration`, after the paragraph beginning "Discovered document symlinks are resolved separately." insert:

```markdown
`link_sources` is the file set `links` gates, and it is independent of `docs_roots`: the lattice
corpus is the files you track, and the link gate may cover files you do not, or fewer. It is a
list of project-relative selectors in a POSIX grammar that reads the same on every platform. `/`
is the only separator and a backslash is a config error; `*`, `?`, and `[...]` classes match
within one segment, case-sensitively, and never cross `/`; `**` is accepted only as a whole
segment and matches zero or more directories. `docs/**/*.md`, `ARCHITECTURE.md`, and `*.md` are
all selectors. Expansion never enters a symlinked directory, whether `**` reaches it or a segment
names it, and a symlinked file is selected by its spelling and judged for containment afterward.
Matches are unioned across selectors, sorted by their project-relative spelling, and deduplicated
by resolved target, so YAML order and overlapping selectors cannot change the output.

The key fails closed. It has no default and is not derived from `docs_roots`; `ignore_globs` does
not apply to it, since that key is anchored to each docs root and a selector already says what it
wants. With the key omitted or empty, or with any selector that matches no file, `links` exits 2
rather than reporting a clean run over nothing. The generated config writes a selector per docs
root, `docs/**/*.md` for the default, which therefore fails closed until that directory holds at
least one Markdown file.
```

1f. In "Ordinary offline setup" (line 820 area): change "pre-commit hooks, and a GitHub Actions workflow that run `doc-lattice check` (drift) and `doc-lattice lint` (authority ladder) as your gates" to "pre-commit hooks, and a GitHub Actions workflow that run `doc-lattice check` (drift), `doc-lattice lint` (authority ladder), and `doc-lattice links` (dead links) as your gates"; change "so those two hooks stay inert" to "so those three hooks stay inert"; change "they run only `check` and `lint`" to "they run only `check`, `lint`, and `links`".

1g. In "Enabling the gates": "It adds two hook definitions" becomes "It adds three hook definitions"; "Both hooks run on it and pass" becomes "All three hooks run on it and pass"; replace the paragraph beginning "Confirm activation with a commit that stages at least one Markdown file." with:

```markdown
Confirm activation with any commit. The `links` hook carries `always_run: true`, because the break
it catches is cross-document and the file that changed is not the file that ends up wrong, so it
runs on every commit and reports itself as passed or failed either way. The `check` and `lint`
entries carry `files: \.md$`, so a commit staging no Markdown file reports those two as
`Skipped`; that is still a working gate, and the `links` line beside them is the proof.
```

1h. In "Upgrading": "two `entry:` lines. The block carries generated structure beyond those two commands" becomes "three `entry:` lines. The block carries generated structure beyond those three commands".

1i. Exit codes table: the `1` row becomes "Coherent finding: lattice drift, an authority inversion, a dead link or fragment, or a Linear gate failure."

1j. Project structure: under `src/doc_lattice/` add a line after `markdown_compat.py`:

```
│   ├── link_check.py           # the links gate: selector expansion and link resolution (read-only I/O)
```

and reword the `src/doc_lattice/` comment to `# the engine: a pure graph/report core, the link gate, behind a thin impure shell`.

- [ ] **Step 2: ARCHITECTURE.md**

2a. System Overview (lines 10-13): replace the diagram with:

```
    config -> discovery -> frontmatter parse -> loader.build_lattice
        -> { check, impact, reconcile, graph, lint, linear }
    config -> link_check.select_link_sources -> link_check.check_links -> links
```

and add after "`init` is separate and never loads the lattice.": "`links` is the other lattice-independent command: it reads `link_sources` from the same config and runs the read-only link gate in `link_check.py`."

2b. AD-2 Decision: after the sentence ending "own load-path filesystem work." add: "`link_check.py` owns the read-only filesystem work of the `links` gate: selector expansion by a no-follow walk, containment of both ends of a link, and the source and target reads. It is the one boundary that never feeds the lattice."

AD-2 Consequences: replace "Every command's logic is unit-tested with no I/O;" with "Every lattice command's logic is unit-tested with no I/O, and the link gate's engine, which is a read-only boundary by its nature, is tested against temporary trees;".

2c. Append AD-45 at the end of the file:

```markdown
### AD-45: The link gate is its own command over its own source set, reading the engine's inventory

**Date:** 2026-09-02
**Status:** Accepted
**Context:** `scripts/check_doc_links.py` was a complete Markdown link gate that reached nobody:
excluded from the wheel, hardcoded to this repository's root, and callable only by being this
repository. colinear ran `check` and `lint` green over hundreds of dead fragment links, which is
correct behavior for both and the whole problem. GTX-477 productizes it.
**Decision:** Ship it as `doc-lattice links`, a command of its own over a `link_sources` config
key, with the engine in `link_check.py` reading `markdown_compat.full_heading_inventory`.

**Its own verb, never folded into `check`.** `check` is the lattice edge gate, and its green is
honest precisely because it claims nothing about Markdown links. Folding link coverage in would
make that green a lie the first time a link class the gate does not model went dead, and would
put an exit-1 finding class under a command whose adopters gate a different thing.

**An independent `link_sources` key, with no `docs_roots` fallback and no `ignore_globs`.**
`docs_roots` is the tracked lattice corpus, and a consumer may gate links in files it does not
track (colinear's root carries several) or in fewer; reusing it conflates two corpora and yields
an honest-looking green over the wrong files. `ignore_globs` is anchored to each docs root, so
applying it here would re-couple the corpora silently. A selector already says exactly what it
wants. The key fails closed per selector: omitted, empty, or any selector matching nothing is
exit 2, because the generated hook and workflow run the command unconditionally and a fallback
would let a mandatory gate pass over zero files.

**One heading inventory.** The script built its own link-target inventory, wider than the
addressable subset because a link to a setext or nested heading resolves on GitHub. v7 shipped
`full_heading_inventory`, the same width described in the same terms. The gate now collects that
inventory's `github_id` values and its private walk is deleted, so there is one implementation of
"the ids GitHub allocates" rather than two that agree by discipline. Addressability is unchanged:
the wider inventory is read, never allocated from, so no cached derivation moves.

**Selection is a no-follow walk of the module's own, not `Path.glob`.** `Path.glob` orders
results unspecifiedly, matches case by platform, stops following symlinks only while expanding
`**`, and suppresses scanning errors. The walk is POSIX on every platform, case-sensitive by code
point, never enters a symlinked directory (a link to `/` would otherwise turn `**` into a
filesystem walk), reports a directory it cannot scan as exit 2, and orders by sorting the union
of lexical matches before judging them, so YAML order and filesystem order cannot change output.

**Containment after selection, so an escaping source is a finding.** `docs_roots` resolution
rejects an escaping entry at load and discovery skips one with a warning; both are wrong here,
where the requirement is that a configured file nobody checks is reported. Selection keeps the
unresolved spelling, and `check_links` rechecks containment immediately before each read. What
the filesystem refuses to resolve, inspect, scan, or open is a tool error rather than a finding,
because a gate that cannot see its inputs must not pass; what a document's content refuses
(undecodable bytes, a parser-rejected reference) stays a finding and the run continues.

**Human findings on stderr; annotations on stdout.** The script always wrote findings to stderr,
and the hook contract is preserved through an exact stderr writer that bypasses Rich, so a
filename shaped like markup stays a filename. A departed stderr reader is answered under the
stderr half of AD-40 and the semantic exit code stands; only a truncated stdout result, the
annotation stream, reaches the silent 141. `--format github` annotates each finding at its line,
which is the form the generated workflow runs.

**Consequences:** Both generated adopter surfaces run the command, the hook with `always_run`
and the workflow with `--format github`, and the generated config writes `link_sources` derived
from the docs roots by the `init` adapter, from the resolved path for a root that exists so a
symlinked root cannot be written as a selector the walk would never enter. Those are
migration-rule surfaces, and the generated config is now enrolled in the guard with them.
This repository's own gate runs through the shipped command over `link_sources: ["*.md"]`, the
first prerequisite of GTX-168. `scripts/check_doc_links.py` is deleted. JSON output was declined:
nobody asked for a schema of link findings, and one is a contract to own.
```

- [ ] **Step 3: CLAUDE.md**

Replace the two enforced-rules bullets that begin "- `scripts/check_doc_links.py` resolves every relative Markdown link" and "- That link-target inventory is deliberately separate from doc-lattice's section identity." with:

```markdown
- This repository's Markdown links are gated by the shipped command: `doc-lattice links` runs in
  the pre-commit hook and the CI code-quality job over the `link_sources` in `.doc-lattice.yml`,
  which selects the root `*.md` files. A target may be any repository-contained relative path,
  `docs/` staging included; absolute and external destinations are out of scope. Write
  destinations as Markdown links: a raw HTML anchor is reported rather than resolved, for the
  reason `link_check.py` records. Fragments resolve against the engine's full heading
  inventory, so renaming a heading or moving a file fails the hook and CI rather than breaking a
  deep link silently. README.md owns the command contract and AD-45 owns the decisions.
- That inventory is deliberately wider than the addressable subset: `link_check.py` reads
  `markdown_compat.full_heading_inventory`, which covers every heading form GitHub assigns an id
  to, while addressing stays column-zero ATX. Keep the separation: accepting a valid deep link by
  widening `extract_headings` instead would change which sections the engine sees, which is a
  cached-derivation change costing a `CACHE_VERSION` bump and an edit to README.md's
  addressable-subset paragraph and AD-13. Use `github_ids_for_texts` or the inventory for GitHub
  heading ids: `github_slug` is a base slug with no deduplication, and `anchor_ids` answers a
  different question, doc-lattice's explicit `{#anchor}` identity. Rendered inline heading text
  is out of reach on both sides, since ids are slugged from raw inline source.
```

Confirm the two command-list edits from Task 7 are present (`uv run --group dev doc-lattice links` in the contributor commands and in the Markdown-only verification sentence).

- [ ] **Step 4: Verify the documents**

Run:
```bash
uv run --group dev doc-lattice links
uv run --group dev python scripts/check_version_sync.py
uv run --group dev python scripts/check_migration_rule.py
git diff --check
env -u FORCE_COLOR uv run --group dev python -m pytest tests/test_readme_contract.py tests/test_conventions.py -q
grep -c '—' README.md ARCHITECTURE.md CLAUDE.md
```
Expected: links clean, both scripts pass, no whitespace errors, tests PASS, no em dashes.

- [ ] **Step 5: Commit**

```bash
git add README.md ARCHITECTURE.md CLAUDE.md
git commit -m "docs: document the links command, link_sources, and AD-45"
```

---

### Task 11: Full verification

- [ ] **Step 1: Run the complete handoff set**

```bash
env -u FORCE_COLOR uv run --group dev python -m pytest -n auto --dist loadfile -q
uv run --group dev ruff check src tests scripts
uv run --group dev ruff format --check src tests scripts
uv run --group dev ty check src scripts
uv run --group dev python scripts/check_typing_boundaries.py src
uv run --group dev python scripts/check_version_sync.py
uv run --group dev doc-lattice links
uv run --group dev python scripts/check_migration_rule.py
git diff --check
```
Expected: pytest green with coverage at or above 80 percent; every gate passes.

- [ ] **Step 2: Run the 3.13 leg of the suite**

```bash
UV_PYTHON=3.13 env -u FORCE_COLOR uv run --group dev python -m pytest -n auto --dist loadfile -q
```
Expected: green.

- [ ] **Step 3: Record the run for Linear and report**

```bash
colinear review record-test --issue GTX-477 --command 'uv run --group dev python -m pytest -n auto --dist loadfile' --passed <N> --failed 0
```

Then report the branch state, the deliberate "maintained document" to "link source" wording change, and that the Linear acceptance bullet about regenerating the baseline needs correcting to the guard's rule at handoff.
