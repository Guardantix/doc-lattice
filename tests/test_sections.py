"""Tests for section extraction."""

import pytest

import doc_lattice.sections as sections_module
from doc_lattice.sections import (
    Heading,
    anchor_ids,
    build_toc,
    github_slug,
    section_span,
    section_spans,
    section_text,
    split_body_lines,
)

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


@pytest.mark.parametrize(("body", "level"), [("#", 1), ("##   ", 2)])
def test_build_toc_accepts_empty_atx_heading(body, level):
    assert build_toc(body) == [Heading(level=level, text="", anchor=None, line=1)]


def test_build_toc_rejects_spaceless_heading_text():
    assert build_toc("#not head") == []


def test_section_span_stops_at_same_or_higher_level():
    toc = build_toc(DOC)
    total = len(DOC.splitlines())
    # "accent" (index 1) spans through its nested child until "## Other" at line 10.
    assert section_span(toc, 1, total) == (4, 9)
    # "nested" (index 2) spans until "## Other".
    assert section_span(toc, 2, total) == (7, 9)
    # "other" (index 3) spans to EOF.
    assert section_span(toc, 3, total) == (10, total)


def test_section_span_does_not_compute_all_spans(monkeypatch):
    toc = build_toc(DOC)
    total = len(DOC.splitlines())

    def reject_batch_call(_headings, _total_lines):
        pytest.fail("section_span must not delegate a single lookup to section_spans")

    monkeypatch.setattr(sections_module, "section_spans", reject_batch_call)

    assert section_span(toc, 1, total) == (4, 9)


def test_section_spans_matches_individual_section_span_results():
    toc = build_toc(DOC)
    total = len(DOC.splitlines())

    expected = [section_span(toc, i, total) for i in range(len(toc))]
    assert section_spans(toc, total) == expected


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


def test_split_body_lines_uses_shared_newline_normalizer(monkeypatch):
    calls: list[str] = []

    def normalize_newlines(body: str) -> str:
        calls.append(body)
        return "normalized\nlines"

    monkeypatch.setattr(sections_module, "normalize_newlines", normalize_newlines)

    assert split_body_lines("raw body") == ["normalized", "lines"]
    assert calls == ["raw body"]


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


def test_build_toc_ignores_nontrailing_anchor_marker():
    toc = build_toc("## Use `{#id}` in examples\nbody line\n")

    assert toc[0].anchor is None
    assert toc[0].text == "Use `{#id}` in examples"


def test_build_toc_does_not_accept_unspaced_hashes_after_anchor_marker():
    toc = build_toc("## Example {#id}##\nbody line\n")

    assert toc[0].anchor is None


def test_section_text_preserves_nontrailing_anchor_marker():
    body = "## Use `{#id}` in examples\nbody line\n"

    assert section_text(body, (1, 2)) == "## Use `{#id}` in examples\nbody line"


def test_build_toc_strips_atx_closing_sequence_from_text():
    # A CommonMark closing '#' run (preceded by whitespace) is not heading content, so it is
    # dropped from Heading.text; a '#' inside the content is kept.
    toc = build_toc("# Title #\n## C# guide ##\n")
    assert [h.text for h in toc] == ["Title", "C# guide"]


def test_anchor_ids_matches_github_for_atx_closing_sequence():
    # GitHub renders '## Save format ##' with anchor 'save-format' (closing '##' discarded);
    # without stripping the closing run the trailing space would slug to 'save-format-'.
    toc = build_toc("## Save format ##\nx\n")
    assert anchor_ids(toc) == ["save-format"]


def test_build_toc_keeps_marker_when_closing_sequence_present():
    # The closing '##' is stripped, but an explicit marker before it survives.
    toc = build_toc("## Accent {#accent} ##\nx\n")
    assert toc[0].anchor == "accent"
    assert toc[0].text == "Accent {#accent}"


def test_section_text_strips_marker_before_atx_closing_sequence():
    body = "## Accent {#accent} ##\nx\n"

    assert section_text(body, (1, 2)) == "## Accent ##\nx"


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Slot table", "slot-table"),
        ("3.2 Slot table", "32-slot-table"),  # '.' stripped, '3' and '2' join
        ("5.7 Capability", "57-capability"),
        ("Hello, World!", "hello-world"),  # punctuation stripped
        ("A  B", "a--b"),  # runs are NOT collapsed; each space becomes one hyphen
        ("well-known term", "well-known-term"),  # existing hyphens preserved
        ("snake_case name", "snake_case-name"),  # underscores preserved
        ("Fast⚡Mode", "fastmode"),  # emoji/symbol stripped, no adjacent space
        ("Overview", "overview"),
    ],
)
def test_github_slug_matches_github_rules(text, slug):
    assert github_slug(text) == slug


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        # Category No (superscript / vulgar fraction / circled digit): github-slugger strips
        # these; a hand-rolled `\w`-based class wrongly keeps them. Values observed from the
        # real github-slugger@2.0.0 package (see task-1-fix-report.md).
        ("x²", "x"),  # SUPERSCRIPT TWO
        ("½ cup", "-cup"),  # VULGAR FRACTION ONE HALF
        ("① step one", "-step-one"),  # CIRCLED DIGIT ONE
        # Category Mn (nonspacing combining marks): github-slugger keeps these; a hand-rolled
        # class wrongly strips them.
        ("é", "é"),  # e + COMBINING ACUTE ACCENT, unchanged
        # An emoji (So, stripped) directly followed by VARIATION SELECTOR-16 (Mn, kept): only
        # the emoji is removed, the selector survives.
        ("\U0001f44d️", "️"),
        # Category Pc other than underscore (connector punctuation): github-slugger keeps
        # these; a hand-rolled class wrongly strips them.
        ("under‿score", "under‿score"),  # UNDERTIE
        ("under⁀score", "under⁀score"),  # CHARACTER TIE
        # Category Lm (modifier letter): github-slugger keeps these.
        ("aʼb", "aʼb"),  # noqa: RUF001 -- MODIFIER LETTER APOSTROPHE, intentional
    ],
)
def test_github_slug_divergent_unicode_categories(text, slug):
    assert github_slug(text) == slug


def test_anchor_ids_uses_marker_when_present_else_slug():
    toc = build_toc("# Intro {#custom}\n\n## Slot table\nx\n")
    assert anchor_ids(toc) == ["custom", "slot-table"]


def test_anchor_ids_dedupes_repeated_slugs_in_document_order():
    toc = build_toc("## Notes\n\n## Notes\n\n## Notes\n")
    assert anchor_ids(toc) == ["notes", "notes-1", "notes-2"]


def test_anchor_ids_marker_heading_reserves_its_github_slug():
    # GitHub slugs '## Notes {#n}' from its literal, marker-included text to 'notes-n' and
    # reserves it; a later '## Notes n' then collides and becomes 'notes-n-1'. Reserving the
    # marker heading's slug keeps doc-lattice byte-parity with GitHub in this mixed case.
    toc = build_toc("## Notes {#n}\n\n## Notes n\nx\n")
    assert anchor_ids(toc) == ["n", "notes-n-1"]


def test_anchor_ids_empty_toc_is_empty():
    assert anchor_ids([]) == []
