"""Section span and text utilities over the versioned Markdown adapter.

Section-span semantics are adapted from gx-linear-skills' binding_slicer: a section
spans from its heading line through the line before the next heading of equal or higher
level, or to end of file. Heading extraction and slug generation live in
``markdown_compat`` so their upstream compatibility boundary remains explicit.
"""

from .hashing import normalize_newlines
from .markdown_compat import (
    Heading,
    SluggedHeading,
    anchor_ids,
    extract_headings,
    github_slug,
    strip_heading_anchor,
)

__all__ = [
    "Heading",
    "ancestor_chains",
    "anchor_ids",
    "build_toc",
    "github_slug",
    "section_spans",
    "section_text",
    "split_body_lines",
]


def split_body_lines(body: str) -> list[str]:
    """Split ``body`` into lines on ``\n`` only, matching the hashing model.

    Unlike ``str.splitlines``, this does not treat form feed, vertical tab, NEL, or the
    Unicode line/paragraph separators as line breaks, so an exotic separator inside
    content cannot spawn a phantom heading or anchor. Line endings are normalized first
    and a single trailing blank (from a final newline) is dropped, so the result matches
    ``str.splitlines`` for ordinary text.

    Args:
        body: Markdown document text.

    Returns:
        The lines of ``body``.
    """
    lines = normalize_newlines(body).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def build_toc(body: str) -> list[Heading]:
    """Return supported headings through the pinned compatibility adapter.

    Args:
        body: Markdown document text.

    Returns:
        Top-level, column-zero ATX headings outside CommonMark fenced code blocks.
    """
    return extract_headings(body)


def section_spans(headings: list[Heading], total_lines: int) -> list[tuple[int, int]]:
    """Return inclusive line ranges for every heading in one pass.

    Args:
        headings: The document TOC from ``build_toc``.
        total_lines: Total line count of the document.

    Returns:
        A list of ``(start, end)`` spans positionally aligned with ``headings``. Each
        section runs from its heading through the line before the next heading of equal
        or higher level, or to ``total_lines``.
    """
    end_lines = [total_lines] * len(headings)
    stack: list[tuple[int, int]] = []
    for idx, heading in enumerate(headings):
        while stack and stack[-1][1] >= heading.level:
            previous_idx, _ = stack.pop()
            end_lines[previous_idx] = heading.line - 1
        stack.append((idx, heading.level))
    return [(heading.line, end_line) for heading, end_line in zip(headings, end_lines, strict=True)]


def ancestor_chains(full: list[SluggedHeading], toc: list[Heading]) -> list[tuple[str, ...]]:
    """Return each addressable heading's enclosing heading chain, outermost first.

    Derived by heading *level* over every heading form GitHub assigns an id to, not by span
    containment over the addressable subset. A parent written as a setext heading, as an ATX
    heading indented one to three spaces, or nested in a list item or a block quote owns no
    ``TargetId`` and so can never appear in ``model.Lattice.ancestors``; it is still the parent
    a reader sees, and the drift hash needs the chain the reader sees. It is also not merely
    missing from the span-based chain: a non-addressable heading does not terminate an
    addressable section's span either, so span ancestry attributes a section nested under it to
    the preceding addressable heading instead.

    The two inventories are merged by line rather than either taken alone, for the reason
    ``loader._collision_members_by_line`` already merges them: the full parse misses a heading
    the restricted addressable scanner still addresses, such as a column-zero ``#`` line inside
    an HTML comment. The full inventory wins a line both see, matching that precedence.

    Every ancestor renders as normalized ATX -- ``"#" * level``, a space, then the heading text
    with any explicit ``{#anchor}`` marker stripped -- whatever form it was written in. One
    spelling for every form means converting a heading between forms at the same level, or
    adding or removing a marker on it, does not restale its descendants.

    Args:
        full: The full GitHub heading inventory, from ``full_heading_inventory``.
        toc: The addressable ATX subset from ``build_toc``, in document order.

    Returns:
        One chain per heading in ``toc``, positionally aligned with it, outermost first and
        empty for a heading with no enclosing heading.
    """
    outline: dict[int, tuple[int, str]] = {}
    for slugged in full:
        outline[slugged.line] = (slugged.level, slugged.text)
    for heading in toc:
        outline.setdefault(heading.line, (heading.level, heading.text))

    addressable_lines = {heading.line for heading in toc}
    chains: dict[int, tuple[str, ...]] = {}
    stack: list[tuple[int, str]] = []
    for line in sorted(outline):
        level, text = outline[line]
        while stack and stack[-1][0] >= level:
            stack.pop()
        if line in addressable_lines:
            chains[line] = tuple(_render_ancestor(*entry) for entry in stack)
        stack.append((level, text))
    return [chains[heading.line] for heading in toc]


def _render_ancestor(level: int, text: str) -> str:
    """Render one ancestor heading as normalized ATX, marker stripped.

    ``strip_heading_anchor`` applies to parsed inline text as readily as to a raw source line:
    its pattern anchors on end of string, and the parser has already removed any ATX closing
    sequence.
    """
    marker = "#" * level
    return f"{marker} {strip_heading_anchor(text)}".rstrip()


def section_text(body: str, span: tuple[int, int]) -> str:
    """Return section text with the heading's explicit anchor marker removed.

    Args:
        body: Markdown document text.
        span: Inclusive 1-indexed ``(start, end)`` line range.

    Returns:
        The joined lines of the span, with the anchor marker stripped from the first
        heading line.
    """
    lines = split_body_lines(body)
    start, end = span
    chunk = lines[start - 1 : end]
    if chunk:
        chunk[0] = strip_heading_anchor(chunk[0])
    return "\n".join(chunk)
