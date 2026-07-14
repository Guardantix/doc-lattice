"""Section span and text utilities over the versioned Markdown adapter.

Section-span semantics are adapted from gx-linear-skills' binding_slicer: a section
spans from its heading line through the line before the next heading of equal or higher
level, or to end of file. Heading extraction and slug generation live in
``markdown_compat`` so their upstream compatibility boundary remains explicit.
"""

from .hashing import normalize_newlines
from .markdown_compat import (
    Heading,
    anchor_ids,
    extract_headings,
    github_slug,
    strip_heading_anchor,
)

__all__ = [
    "Heading",
    "anchor_ids",
    "build_toc",
    "github_slug",
    "section_span",
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


def section_span(headings: list[Heading], idx: int, total_lines: int) -> tuple[int, int]:
    """Return the inclusive 1-indexed line range for ``headings[idx]``.

    Args:
        headings: The document TOC from ``build_toc``.
        idx: Index into ``headings`` of the section of interest.
        total_lines: Total line count of the document.

    Returns:
        ``(start, end)`` from the heading line through the line before the next heading
        of equal or higher level, or to ``total_lines``.
    """
    head = headings[idx]
    end = total_lines
    start_idx = idx + 1
    if start_idx < 0:
        start_idx += len(headings)
    for next_idx in range(start_idx, len(headings)):
        nxt = headings[next_idx]
        if nxt.level <= head.level:
            end = nxt.line - 1
            break
    return (head.line, end)


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
