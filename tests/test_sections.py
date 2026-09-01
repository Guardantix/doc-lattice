"""Tests for section extraction."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import doc_lattice.sections as sections_module
from doc_lattice.frontmatter_parser import parse_document
from doc_lattice.hashing import content_hash
from doc_lattice.markdown_compat import full_heading_inventory
from doc_lattice.sections import (
    Heading,
    ancestor_chains,
    anchor_ids,
    build_toc,
    github_slug,
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


def test_single_section_span_helper_is_not_exported_or_present():
    assert "section_span" not in sections_module.__all__
    assert not hasattr(sections_module, "section_span")


def test_section_text_strips_anchor_from_heading_line():
    toc = build_toc(DOC)
    spans = section_spans(toc, len(DOC.splitlines()))
    text = section_text(DOC, spans[1])
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
    spans = section_spans(toc, len(DOC.splitlines()))
    text = section_text(DOC, spans[1])
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
        # github-slugger@2.0.0. Verified codepoint-for-codepoint over Unicode scalar values;
        # repeat that parity check when updating the ported table.
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


def test_the_drift_hash_of_a_section_is_the_same_under_both_spellings():
    body = "# H1\n\n## Two {#two}\ntext\n"
    _fm, fence_body = parse_document(f"---\nid: a\n---\n{body}", Path("a.md"))
    _cm, comment_body = parse_document(f"<!-- doc-lattice\nid: a\n-->\n{body}", Path("b.md"))

    assert content_hash(section_text(fence_body, (3, 4))) == content_hash(
        section_text(comment_body, (3, 4))
    )


def _chains(body: str) -> list[tuple[str, ...]]:
    return ancestor_chains(full_heading_inventory(body), build_toc(body))


@pytest.mark.parametrize(
    ("form", "body"),
    [
        pytest.param("setext", "Parent\n------\n\n### Child\nbody\n", id="setext"),
        pytest.param("indented", " ## Parent\n\n### Child\nbody\n", id="indented-one-space"),
        pytest.param("indented", "   ## Parent\n\n### Child\nbody\n", id="indented-three"),
        pytest.param("list-item", "- ## Parent\n\n### Child\nbody\n", id="list-item"),
        pytest.param("quote", "> ## Parent\n\n### Child\nbody\n", id="block-quote"),
    ],
)
def test_a_non_addressable_parent_still_supplies_the_ancestor_chain(form: str, body: str):
    toc = build_toc(body)

    # Addressability is unchanged: only the child is addressed.
    assert [heading.text for heading in toc] == ["Child"], form
    assert _chains(body) == [("## Parent",)], form


def test_every_ancestor_form_renders_as_the_same_normalized_atx():
    atx = "## Product A\n\n### Setup\nrun it\n"
    setext = "Product A\n---------\n\n### Setup\nrun it\n"
    quoted = "> ## Product A\n\n### Setup\nrun it\n"
    marked = "## Product A {#pa}\n\n### Setup\nrun it\n"

    # Only the ATX spellings put the parent in the TOC as well, so compare the child's chain,
    # which is the last entry in every spelling.
    chains = [_chains(body)[-1] for body in (atx, setext, quoted, marked)]

    assert chains == [("## Product A",)] * 4


def test_the_chain_of_a_top_level_heading_is_empty():
    assert _chains("# Only\nbody\n") == [()]


def test_a_deeper_ancestor_is_popped_by_a_shallower_sibling():
    body = "# Top\n\n## A\n\n### Deep\n\n## B\n\n### Other\n"

    assert _chains(body) == [(), ("# Top",), ("# Top", "## A"), ("# Top",), ("# Top", "## B")]


def test_a_heading_only_the_addressable_scanner_sees_still_anchors_the_chain():
    # A column-zero '#' inside an HTML comment is inert to the full CommonMark parse but is
    # still addressed by extract_headings, so the merged outline must keep it.
    body = "<!--\n# Hidden\n-->\n\n## Child\nbody\n"

    assert [heading.text for heading in build_toc(body)] == ["Hidden", "Child"]
    assert _chains(body) == [(), ("# Hidden",)]


def _chains_reference(
    outline: list[tuple[int, int, str]], toc_lines: set[int]
) -> list[tuple[str, ...]]:
    """Quadratic reference: an ancestor is the nearest preceding heading of each lower level."""
    result: list[tuple[str, ...]] = []
    for index, (line, level, _text) in enumerate(outline):
        if line not in toc_lines:
            continue
        chain: list[str] = []
        deepest = level
        for _previous_line, previous_level, previous_text in reversed(outline[:index]):
            if previous_level < deepest:
                chain.append(f"{'#' * previous_level} {previous_text}")
                deepest = previous_level
        result.append(tuple(reversed(chain)))
    return result


@given(st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=40))
def test_ancestor_chains_matches_the_nearest_shallower_heading_reference(levels: list[int]):
    forms = ["atx", "setext", "quote", "indent"]
    parts: list[str] = []
    outline: list[tuple[int, int, str]] = []
    toc_lines: set[int] = set()
    line = 1
    for index, level in enumerate(levels):
        text = f"H{index}"
        form = forms[index % len(forms)]
        # Setext only reaches levels 1 and 2, so a deeper level falls back to plain ATX.
        if form == "setext" and level <= 2:
            parts.append(f"{text}\n{('=' if level == 1 else '-') * 5}\n\n")
            outline.append((line, level, text))
            line += 3
            continue
        if form == "quote":
            parts.append(f"> {'#' * level} {text}\n\n")
        elif form == "indent":
            parts.append(f"  {'#' * level} {text}\n\n")
        else:
            parts.append(f"{'#' * level} {text}\n\n")
            toc_lines.add(line)
        outline.append((line, level, text))
        line += 2
    body = "".join(parts)

    assert _chains(body) == _chains_reference(outline, toc_lines)
