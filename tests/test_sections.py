"""Tests for section extraction."""

from game_lattice.sections import build_toc, section_span, section_text

DOC = """# Top {#top}
intro

## Accent {#accent}
accent body

### Nested {#nested}
nested body

## Other {#other}
other body
"""


def test_build_toc_extracts_levels_and_anchors():
    toc = build_toc(DOC)
    assert [(h.level, h.anchor, h.line) for h in toc] == [
        (1, "top", 1),
        (2, "accent", 4),
        (3, "nested", 7),
        (2, "other", 10),
    ]


def test_build_toc_anchorless_heading():
    toc = build_toc("## Plain Heading\nbody\n")
    assert toc[0].anchor is None
    assert toc[0].text == "Plain Heading"


def test_section_span_stops_at_same_or_higher_level():
    toc = build_toc(DOC)
    total = len(DOC.splitlines())
    # "accent" (index 1) spans through its nested child until "## Other" at line 10.
    assert section_span(toc, 1, total) == (4, 9)
    # "nested" (index 2) spans until "## Other".
    assert section_span(toc, 2, total) == (7, 9)
    # "other" (index 3) spans to EOF.
    assert section_span(toc, 3, total) == (10, total)


def test_section_text_strips_anchor_from_heading_line():
    toc = build_toc(DOC)
    text = section_text(DOC, section_span(toc, 1, len(DOC.splitlines())))
    assert text.startswith("## Accent\n")
    assert "{#accent}" not in text
    assert "nested body" in text  # nested content is part of the parent span


def test_build_toc_ignores_headings_in_code_fence():
    body = (
        "# Real {#real}\n\n"
        "```\n"
        "# fake heading\n"
        "## {#fakeanchor} not real\n"
        "```\n\n"
        "## After {#after}\n"
    )
    toc = build_toc(body)
    assert [h.anchor for h in toc] == ["real", "after"]


def test_build_toc_handles_tilde_fence_with_info_string():
    body = "# Real {#real}\n\n~~~python\n# x = 1  {#nope}\n~~~\n\n## After {#after}\n"
    toc = build_toc(body)
    assert [h.anchor for h in toc] == ["real", "after"]


def test_build_toc_ignores_exotic_line_separator():
    # A form feed must not split a non-heading line into a phantom heading/anchor.
    body = "intro text\x0c# Notes {#palette}\n\n## Real {#real}\n"
    toc = build_toc(body)
    assert [h.anchor for h in toc] == ["real"]
