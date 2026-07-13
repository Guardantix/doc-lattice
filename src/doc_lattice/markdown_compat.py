"""Versioned Markdown heading and GitHub-slug compatibility adapter.

The supported Markdown subset is top-level, column-zero ATX headings plus CommonMark
backtick and tilde fences. ``markdown-it-py==4.2.0`` owns heading and fence recognition;
the line-skip fallback only avoids building tokens for content doc-lattice does not use.
"""

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_block.state_block import StateBlock

from ._github_slugger_data import SLUG_STRIP_PATTERN
from .hashing import normalize_newlines

MARKDOWN_COMPAT_VERSION = "markdown-it-py==4.2.0"
SLUG_COMPAT_VERSION = "github-slugger@2.0.0"

_ANCHOR_RE = re.compile(r"(?:^|\s+)\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}(?:\s*$|\s+(?=#+\s*$))")
_SLUG_STRIP_RE = re.compile(SLUG_STRIP_PATTERN)


@dataclass(frozen=True, slots=True)
class Heading:
    """One supported ATX heading with a 1-based source line."""

    level: int
    text: str
    anchor: str | None
    line: int


def _skip_line(state: StateBlock, start_line: int, _end_line: int, silent: bool) -> bool:
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
    """Extract the supported top-level ATX headings from Markdown.

    Args:
        body: Markdown document text.

    Returns:
        Headings in document order with raw inline content, trailing explicit anchor,
        and exact 1-based source line.

    Raises:
        RuntimeError: If the pinned parser returns a malformed heading token pair.
    """
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


def github_slug(text: str) -> str:
    """Return a github-slugger 2.0.0 base slug without deduplication.

    Args:
        text: Raw heading content.

    Returns:
        The lowercased, stripped, and space-replaced upstream-compatible slug.
    """
    return _SLUG_STRIP_RE.sub("", text.lower()).replace(" ", "-")


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
