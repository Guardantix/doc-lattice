"""Heading-TOC and anchored-section extraction.

Section-span semantics are adapted from gx-linear-skills' binding_slicer: a section
spans from its heading line through the line before the next heading of equal or higher
level, or to end of file.
"""

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ANCHOR_RE = re.compile(r"\s*\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}\s*")
_FENCE_RE = re.compile(r"^ {0,3}(?P<ticks>`{3,}|~{3,})(?P<info>.*)$")


@dataclass(frozen=True, slots=True)
class Heading:
    """One markdown heading. ``line`` is 1-indexed. ``text`` keeps the anchor marker."""

    level: int
    text: str
    anchor: str | None
    line: int


def split_body_lines(body: str) -> list[str]:
    """Split ``body`` into lines on ``\\n`` only, matching the hashing model.

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
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def build_toc(body: str) -> list[Heading]:
    """Return all ATX headings in ``body`` in document order.

    Headings inside fenced code blocks (delimited by ``` or ~~~) are ignored, so a
    ``#``-prefixed comment or a ``{#id}`` token inside a code sample is not mistaken for
    a heading or anchor.

    Args:
        body: Markdown document text.

    Returns:
        A list of Heading, each with its level, text, optional ``{#anchor}`` id, and
        1-indexed line number.
    """
    headings: list[Heading] = []
    open_fence: str | None = None
    for i, line in enumerate(split_body_lines(body), start=1):
        fence_match = _FENCE_RE.match(line)
        if open_fence is None:
            if fence_match is not None:
                open_fence = fence_match.group("ticks")
                continue
        else:
            # CommonMark closing-fence rule: a fence closes only on the same fence
            # character (backtick or tilde), a run at least as long as the opener, and
            # nothing after it. A shorter run or a trailing info string keeps the block
            # open, so those lines stay code content and never register as headings.
            is_closing_fence = (
                fence_match is not None
                and fence_match.group("ticks")[0] == open_fence[0]
                and len(fence_match.group("ticks")) >= len(open_fence)
                and not fence_match.group("info").strip()
            )
            if is_closing_fence:
                open_fence = None
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        raw_text = match.group(2)
        anchor_match = _ANCHOR_RE.search(raw_text)
        anchor = anchor_match.group(1) if anchor_match else None
        headings.append(Heading(level=level, text=raw_text, anchor=anchor, line=i))
    return headings


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
    for nxt in headings[idx + 1 :]:
        if nxt.level <= head.level:
            end = nxt.line - 1
            break
    return (head.line, end)


def section_text(body: str, span: tuple[int, int]) -> str:
    """Return the text of a section span with the heading's ``{#anchor}`` marker removed.

    Args:
        body: Markdown document text.
        span: Inclusive 1-indexed ``(start, end)`` line range.

    Returns:
        The joined lines of the span, with the anchor marker stripped from the first
        (heading) line.
    """
    lines = split_body_lines(body)
    start, end = span
    chunk = lines[start - 1 : end]
    if chunk:
        chunk[0] = _ANCHOR_RE.sub(" ", chunk[0]).rstrip()
    return "\n".join(chunk)
