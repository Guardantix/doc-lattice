"""Versioned Markdown heading and GitHub-slug compatibility adapter.

The supported Markdown subset is top-level, column-zero ATX headings plus CommonMark
backtick and tilde fences. ``markdown-it-py==4.2.0`` owns heading and fence recognition;
the local state adapter builds only the source maps those rules require. Generated data preserves
``github-slugger@2.0.0`` lowercase and strip behavior under JavaScript Unicode 17.0.
"""

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_block import fence as parse_fence
from markdown_it.rules_block import heading as parse_heading
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.token import Token
from markdown_it.utils import EnvType

from ._github_slugger_data import (
    CASE_IGNORABLE_PATTERN,
    CASED_PATTERN,
    JAVASCRIPT_UNICODE_VERSION,
    LOWERCASE_PATCH_TRANSLATION,
    SLUG_STRIP_PATTERN,
)
from .hashing import normalize_newlines

MARKDOWN_COMPAT_VERSION = "markdown-it-py==4.2.0"
SLUG_COMPAT_VERSION = "github-slugger@2.0.0"
SLUG_UNICODE_VERSION = JAVASCRIPT_UNICODE_VERSION

_ANCHOR_RE = re.compile(r"(?:^|\s+)\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}(?:\s*$|\s+(?=#+\s*$))")
_CASED_RE = re.compile(CASED_PATTERN)
_CASE_IGNORABLE_RE = re.compile(CASE_IGNORABLE_PATTERN)
_SLUG_STRIP_RE = re.compile(SLUG_STRIP_PATTERN)
_HEADING_TOKEN_COUNT = 3
_GREEK_CAPITAL_SIGMA = "\u03a3"
_GREEK_SMALL_SIGMA = "\u03c3"
_GREEK_FINAL_SIGMA = "\u03c2"


@dataclass(frozen=True, slots=True)
class Heading:
    """One supported ATX heading with a 1-based source line."""

    level: int
    text: str
    anchor: str | None
    line: int


class _SourceMapState(StateBlock):
    """Minimal line-map state for markdown-it-py's pinned block rules.

    ``StateBlock`` scans every source character to support every CommonMark container.
    Doc-lattice supports only top-level ATX headings and fences, so this adapter builds
    the same fields by scanning line starts and indentation only. Recognition stays in
    markdown-it-py's unmodified ``heading`` and ``fence`` rules.
    """

    def __init__(self, src: str, md: MarkdownIt, env: EnvType, tokens: list[Token]) -> None:
        self.src = src
        self.md = md
        self.env = env
        self.tokens = tokens
        self.bMarks: list[int] = []
        self.eMarks: list[int] = []
        self.tShift: list[int] = []
        self.sCount: list[int] = []
        self.bsCount: list[int] = []
        self.blkIndent = 0
        self.line = 0
        self.lineMax = 0
        self.tight = False
        self.ddIndent = -1
        self.listIndent = -1
        self.parentType = "root"
        self.level = 0
        self.result = ""

        source_lines = src.split("\n")
        if source_lines and source_lines[-1] == "":
            source_lines.pop()
        start = 0
        for source_line in source_lines:
            indent = 0
            expanded_indent = 0
            for character in source_line:
                if character not in (" ", "\t"):
                    break
                indent += 1
                if character == "\t":
                    expanded_indent += 4 - expanded_indent % 4
                else:
                    expanded_indent += 1
            end = start + len(source_line)
            self.bMarks.append(start)
            self.eMarks.append(end)
            self.tShift.append(indent)
            self.sCount.append(expanded_indent)
            self.bsCount.append(0)
            start = end + 1

        length = len(src)
        self.bMarks.append(length)
        self.eMarks.append(length)
        self.tShift.append(0)
        self.sCount.append(0)
        self.bsCount.append(0)
        self.lineMax = len(self.bMarks) - 1
        self._code_enabled = True


_PARSER = MarkdownIt("commonmark")


def extract_headings(body: str) -> list[Heading]:
    """Extract the supported top-level ATX headings from Markdown.

    Args:
        body: Markdown document text.

    Returns:
        Headings in document order with raw inline content, trailing explicit anchor,
        and exact 1-based source line.

    Raises:
        RuntimeError: If the pinned parser returns a malformed heading token pair.
    """
    normalized = normalize_newlines(body).replace("\0", "\ufffd")
    tokens: list[Token] = []
    state = _SourceMapState(normalized, _PARSER, {}, tokens)
    headings: list[Heading] = []
    line = 0
    while line < state.lineMax:
        position = state.bMarks[line] + state.tShift[line]
        if position >= state.eMarks[line]:
            line += 1
            continue
        marker = state.src[position]
        if marker in ("`", "~") and parse_fence(state, line, state.lineMax, False):
            line = state.line
            tokens.clear()
            continue
        if marker != "#" or not parse_heading(state, line, state.lineMax, False):
            line += 1
            continue
        if (
            len(tokens) != _HEADING_TOKEN_COUNT
            or tokens[0].type != "heading_open"
            or tokens[1].type != "inline"
        ):
            msg = f"{MARKDOWN_COMPAT_VERSION} returned a malformed heading token pair"
            raise RuntimeError(msg)
        if state.tShift[line] == 0:
            text = tokens[1].content
            anchor_match = _ANCHOR_RE.search(text)
            headings.append(
                Heading(
                    level=len(tokens[0].markup),
                    text=text,
                    anchor=anchor_match.group(1) if anchor_match else None,
                    line=line + 1,
                )
            )
        line = state.line
        tokens.clear()
    return headings


def _is_final_sigma(text: str, index: int) -> bool:
    for position in range(index - 1, -1, -1):
        character = text[position]
        if _CASE_IGNORABLE_RE.fullmatch(character):
            continue
        if not _CASED_RE.fullmatch(character):
            return False
        break
    else:
        return False

    for character in text[index + 1 :]:
        if _CASE_IGNORABLE_RE.fullmatch(character):
            continue
        return _CASED_RE.fullmatch(character) is None
    return True


def _lower_with_pinned_unicode(text: str) -> str:
    lowercase: list[str] = []
    for index, character in enumerate(text):
        if character == _GREEK_CAPITAL_SIGMA:
            lowercase.append(
                _GREEK_FINAL_SIGMA if _is_final_sigma(text, index) else _GREEK_SMALL_SIGMA
            )
            continue
        lowercase.append(character.lower().translate(LOWERCASE_PATCH_TRANSLATION))
    return "".join(lowercase)


def github_slug(text: str) -> str:
    """Return a github-slugger 2.0.0 base slug without deduplication.

    Args:
        text: Raw heading content.

    Returns:
        The JavaScript Unicode 17 lowercased, stripped, and space-replaced compatible slug.
    """
    lowercase = text.lower() if text.isascii() else _lower_with_pinned_unicode(text)
    return _SLUG_STRIP_RE.sub("", lowercase).replace(" ", "-")


class _Slugger:
    """Document-order slug deduplicator matching github-slugger 2.0.0."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        """Return the next unique slug for heading content."""
        base = github_slug(text)
        result = base
        while result in self._seen:
            self._seen[base] += 1
            result = f"{base}-{self._seen[base]}"
        self._seen[result] = 0
        return result


def anchor_ids(headings: list[Heading]) -> list[str]:
    """Return one explicit or generated addressable id per heading.

    Args:
        headings: Supported headings in document order.

    Returns:
        Addressable ids positionally aligned with ``headings``.
    """
    slugger = _Slugger()
    ids: list[str] = []
    for heading in headings:
        unique = slugger.slug(heading.text)
        ids.append(heading.anchor if heading.anchor is not None else unique)
    return ids


def strip_heading_anchor(text: str) -> str:
    """Remove a valid trailing explicit anchor from one raw heading line.

    Args:
        text: Raw source line containing the section heading.

    Returns:
        The line with its trailing marker removed and ATX closing sequence retained.
    """
    return _ANCHOR_RE.sub(" ", text).rstrip()
