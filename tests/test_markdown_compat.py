"""Golden tests for the versioned Markdown compatibility adapter."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from doc_lattice.frontmatter_parser import parse_document
from doc_lattice.markdown_compat import (
    SLUG_UNICODE_VERSION,
    anchor_ids,
    code_block_line_spans,
    collision_components,
    extract_headings,
    full_heading_inventory,
    github_heading_ids,
    github_ids_for_texts,
    github_slug,
    strip_heading_anchor,
)
from doc_lattice.sections import section_spans, split_body_lines

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "markdown_compatibility.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_extract_headings_matches_golden_fixture(case: dict[str, object]) -> None:
    headings = extract_headings(str(case["body"]))
    assert [asdict(heading) for heading in headings] == case["headings"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_anchor_ids_and_spans_match_golden_fixture(case: dict[str, object]) -> None:
    body = str(case["body"])
    headings = extract_headings(body)

    assert anchor_ids(headings) == case["anchor_ids"]
    spans = section_spans(headings, len(split_body_lines(body)))
    assert [list(span) for span in spans] == case["spans"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_github_heading_ids_match_golden_fixture(case: dict[str, object]) -> None:
    headings = extract_headings(str(case["body"]))

    assert github_heading_ids(headings) == case["github_ids"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_github_ids_for_texts_matches_github_heading_ids(case: dict[str, object]) -> None:
    # The two heading inventories share this one collision implementation: section identity
    # reaches it through Heading.text, and the link gate feeds it raw texts from a wider
    # grammar Heading does not describe. Pinning the delegation is what keeps a heading both
    # inventories see resolving to the same id rather than to two independently drifting ones.
    headings = extract_headings(str(case["body"]))

    ids = github_ids_for_texts(heading.text for heading in headings)
    assert ids == github_heading_ids(headings)


def test_github_ids_for_texts_dedupes_across_heading_forms_in_document_order() -> None:
    # The gate's inventory mixes forms extract_headings never yields together -- a setext
    # 'Overview' ahead of '# Overview' takes the base slug and moves the ATX heading to
    # 'overview-1'. The rule reads document order over raw text and knows nothing of form.
    assert github_ids_for_texts(["Overview", "Overview", "Overview"]) == [
        "overview",
        "overview-1",
        "overview-2",
    ]


def test_github_ids_for_texts_empty_is_empty() -> None:
    assert github_ids_for_texts([]) == []


def test_github_heading_ids_do_not_substitute_explicit_markers() -> None:
    # GitHub has no {#id} syntax, so it slugs the literal marker-bearing heading. anchor_ids
    # substitutes doc-lattice's explicit identity instead; the two namespaces must not converge.
    headings = extract_headings("## Notes {#n}\n")

    assert github_heading_ids(headings) == ["notes-n"]
    assert anchor_ids(headings) == ["n"]


def test_github_heading_ids_dedupe_repeated_headings_in_document_order() -> None:
    # CHANGELOG.md carries repeated '### Fixed' headings today, so a bare github_slug -- which
    # documents itself as being without deduplication -- would collapse them onto one id.
    headings = extract_headings("### Fixed\n\n### Fixed\n\n### Fixed\n")

    assert github_heading_ids(headings) == ["fixed", "fixed-1", "fixed-2"]
    assert [github_slug(heading.text) for heading in headings] == ["fixed", "fixed", "fixed"]


def test_github_heading_ids_empty_headings_is_empty() -> None:
    assert github_heading_ids([]) == []


def test_strip_heading_anchor_preserves_atx_closing_sequence() -> None:
    assert strip_heading_anchor("## Accent {#accent} ##") == "## Accent ##"


def test_slug_lowercase_uses_pinned_javascript_unicode_data() -> None:
    assert SLUG_UNICODE_VERSION == "17.0"
    assert github_slug("\ua7cb") == "\u0264"
    assert github_slug("\ua7dc") == "\u019b"
    assert github_slug("\u039f\u03a3") == "\u03bf\u03c2"
    assert github_slug("\ua7cb\u03a3") == "\u0264\u03c2"
    assert github_slug("\u1c89\u03a3") == "\u03c2"
    assert github_slug("A\u03a3\u1ad0A") == "a\u03c3a"


def test_code_block_line_spans_covers_fenced_and_indented_blocks():
    body = "# H\n\n```\n<!-- doc-lattice\n```\n\ntext\n\n    indented\n"

    spans = code_block_line_spans(body)

    assert (3, 5) in spans
    assert any(start <= 9 <= end for start, end in spans)
    assert not any(start <= 7 <= end for start, end in spans)


_RENDER_PARSER = MarkdownIt("commonmark")


@pytest.mark.parametrize(
    ("name", "source", "expected_first_token"),
    [
        ("accepted", "<!-- doc-lattice\nid: a\n-->\n# H\n", "html_block"),
        ("accepted_empty_body", "<!-- doc-lattice\n-->\n# H\n", "html_block"),
        ("refused_bom", "﻿<!-- doc-lattice\nid: a\n-->\n# H\n", "paragraph_open"),
        ("refused_indent_four", "    <!-- doc-lattice\nid: a\n-->\n# H\n", "code_block"),
    ],
)
def test_every_envelope_byte_form_renders_as_its_pinned_block(
    name: str, source: str, expected_first_token: str
):
    tokens = _RENDER_PARSER.parse(source)

    assert tokens[0].type == expected_first_token, name


def test_the_comment_envelope_never_perturbs_heading_extraction_or_spans():
    body = "# H1\n\n## Two\ntext\n"
    fenced = f"---\nid: a\n---\n{body}"
    commented = f"<!-- doc-lattice\nid: a\n-->\n{body}"

    _fence_meta, fence_body = parse_document(fenced, Path("a.md"))
    _comment_meta, comment_body = parse_document(commented, Path("b.md"))

    assert fence_body == comment_body == body
    assert extract_headings(fence_body) == extract_headings(comment_body)
    assert section_spans(extract_headings(fence_body), len(split_body_lines(fence_body))) == (
        section_spans(extract_headings(comment_body), len(split_body_lines(comment_body)))
    )


def test_the_full_inventory_sees_every_heading_form_github_assigns_an_id_to():
    body = "Overview\n--------\n\ntext\n\n# Overview\n\n> ## Quoted\n\n- ### Nested\n"

    inventory = full_heading_inventory(body)

    assert [(h.text, h.line, h.github_id) for h in inventory] == [
        ("Overview", 1, "overview"),
        ("Overview", 6, "overview-1"),
        ("Quoted", 8, "quoted"),
        ("Nested", 10, "nested"),
    ]


def test_the_inventory_ids_agree_with_the_shared_slugger():
    body = "# Notes\n\n# Notes\n\n# Notes-1\n"

    inventory = full_heading_inventory(body)

    assert [h.github_id for h in inventory] == github_ids_for_texts(h.text for h in inventory)


def _components(body: str) -> list[list[str]]:
    return [
        [f"{h.text}@{h.line}" for h in component]
        for component in collision_components(full_heading_inventory(body))
    ]


def test_chained_dedup_suffixes_pull_every_shifted_heading_into_one_component():
    body = "# Notes\n\n# Notes\n\n# Notes-1\n\n# Notes-1-1\n"

    assert _components(body) == [["Notes@1", "Notes@3", "Notes-1@5", "Notes-1-1@7"]]


def test_a_heading_a_probe_never_reached_stays_out_of_the_component():
    body = "# Notes\n\n# Other\n\n# Notes\n"

    assert _components(body) == [["Notes@1", "Notes@5"]]


def test_probe_completeness_pulls_in_a_heading_only_a_probe_touches():
    # "Other" renamed to "Notes-1": the third heading's base request is still only `notes`, and
    # its final id shifts from `notes-1` to `notes-2`, so a rule reading requests alone would
    # call this clean while a rename of the middle heading silently rebinds the third.
    body = "# Notes\n\n# Notes-1\n\n# Notes\n"

    assert _components(body) == [["Notes@1", "Notes-1@3", "Notes@5"]]


def test_a_cross_inventory_collision_is_one_component():
    body = "Overview\n--------\n\ntext\n\n# Overview\n"

    assert _components(body) == [["Overview@1", "Overview@6"]]


def test_a_document_with_no_repeated_slug_has_no_components():
    assert _components("# One\n\n# Two\n\n# Three\n") == []
