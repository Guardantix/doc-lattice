"""Tests for section extraction."""

import pytest

from game_lattice.sections import build_toc, section_span, section_text, split_body_lines

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


def test_split_body_lines_normalizes_crlf_and_lone_cr():
    assert split_body_lines("a\r\nb\rc\n") == ["a", "b", "c"]


def test_split_body_lines_drops_only_one_trailing_blank():
    assert split_body_lines("a\n") == ["a"]
    assert split_body_lines("a\n\n") == ["a", ""]


def test_split_body_lines_empty_body_is_empty_list():
    assert split_body_lines("") == []


def test_section_text_retains_inner_anchor_markers():
    toc = build_toc(DOC)
    text = section_text(DOC, section_span(toc, 1, len(DOC.splitlines())))
    # Only the heading (first) line is de-anchored; inner anchors stay verbatim.
    assert text.startswith("## Accent\n")
    assert "{#accent}" not in text
    assert "{#nested}" in text  # nested heading's anchor must be preserved


def test_build_toc_mismatched_fence_char_does_not_close():
    body = "# Real {#real}\n```\n~~~\n## Hidden {#hidden}\n```\n\n## After {#after}\n"
    toc = build_toc(body)
    assert [h.anchor for h in toc] == ["real", "after"]


def test_build_toc_closing_fence_with_trailing_text_keeps_block_open():
    body = "# Real {#real}\n```\n``` still-open\n## Hidden {#hidden}\n```\n\n## After {#after}\n"
    toc = build_toc(body)
    assert [h.anchor for h in toc] == ["real", "after"]


def test_build_toc_unclosed_fence_hides_headings_to_eof():
    body = "# Real {#real}\n```\n## Hidden {#hidden}\n"
    toc = build_toc(body)
    assert [h.anchor for h in toc] == ["real"]


def test_build_toc_rejects_too_deep_and_spaceless_headings():
    body = "####### TooDeep {#deep}\n#NoSpace {#nospace}\n###### Six {#six}\n"
    toc = build_toc(body)
    # only the valid level-6 heading registers
    assert [(h.level, h.anchor) for h in toc] == [(6, "six")]


def test_build_toc_empty_body_returns_no_headings():
    assert build_toc("") == []


def test_section_text_empty_or_inverted_span_returns_empty_string():
    # start > end yields no lines and must not raise.
    assert section_text(DOC, (5, 4)) == ""


@pytest.mark.parametrize("heading", ["## A {#_lead}", "## A {#-lead}", "## A {# spaced}"])
def test_build_toc_rejects_invalid_anchor_ids(heading):
    toc = build_toc(heading + "\n")
    assert toc[0].anchor is None  # heading still parsed, but no valid anchor


def test_build_toc_heading_text_retains_anchor_marker():
    toc = build_toc("## Accent {#accent}\nbody\n")
    assert toc[0].text == "Accent {#accent}"
