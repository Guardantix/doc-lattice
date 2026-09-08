"""Golden tests for the versioned Markdown compatibility adapter."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from doc_lattice.frontmatter_parser import parse_document
from doc_lattice.markdown_compat import (
    SLUG_UNICODE_VERSION,
    addressable_explicit_markers,
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
    body = (
        "Overview\n--------\n\ntext\n\n# Overview\n\n   #### Indented\n\n"
        "> ## Quoted\n\n- ### Nested\n"
    )

    inventory = full_heading_inventory(body)

    # Level comes from the token tag, the only field that spells a setext heading's level.
    assert [(h.text, h.level, h.line, h.github_id) for h in inventory] == [
        ("Overview", 2, 1, "overview"),
        ("Overview", 1, 6, "overview-1"),
        ("Indented", 4, 8, "indented"),
        ("Quoted", 2, 10, "quoted"),
        ("Nested", 3, 12, "nested"),
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


def test_a_marker_on_a_rendered_addressable_heading_is_returned():
    assert addressable_explicit_markers("## Fingerprints {#fingerprints}\n") == {"fingerprints"}


def test_the_accessor_returns_bare_marker_values_in_a_frozenset():
    result = addressable_explicit_markers("## Notes {#n}\n")

    assert isinstance(result, frozenset)
    # No leading "#": the fragment's marker character belongs to the link, not to the value.
    assert result == {"n"}


def test_marker_case_survives_because_nothing_here_slugs():
    assert addressable_explicit_markers("## Notes {#MixedCase}\n") == {"MixedCase"}


def test_a_marker_before_an_atx_closing_sequence_is_returned():
    assert addressable_explicit_markers("## Notes {#n} ##\n") == {"n"}


def test_a_document_with_no_marked_heading_yields_nothing():
    assert addressable_explicit_markers("# Plain\n\ntext\n") == frozenset()


def test_a_malformed_marker_is_ordinary_heading_text():
    # `Heading.anchor` already owns the marker grammar; a value that fails it never becomes a
    # marker anywhere in the engine, so it must not become one here either.
    assert addressable_explicit_markers("## Notes {#-bad}\n") == frozenset()
    assert addressable_explicit_markers("## {#early} Notes\n") == frozenset()


def test_repeated_eligible_markers_collapse_to_one_value():
    # A set, not a sequence: this accessor reports membership and performs no uniqueness
    # validation, which is the loader's job and reaches a different diagnostic.
    assert addressable_explicit_markers("## First {#same}\n\n## Second {#same}\n") == {"same"}


def test_a_marker_only_the_restricted_scanner_sees_inside_a_comment_is_not_returned():
    body = "<!--\n## Hidden {#hidden}\n-->\n"

    # The restricted scanner is not container-aware, so it reads the commented heading as one.
    assert [(h.line, h.anchor) for h in extract_headings(body)] == [(2, "hidden")]
    assert full_heading_inventory(body) == []
    assert addressable_explicit_markers(body) == frozenset()

    # The tension this accessor is specified to accept, pinned so it stays visible: the engine
    # does address `hidden`, so a lattice ref to it resolves while this reports no marker at all.
    # A consumer checking marker preservation is therefore blind to a commented heading losing
    # its marker. That is the issue's stated contract ("rendered, addressable"), not an oversight
    # here, but it is the consumer's problem to know about before relying on this set.
    assert anchor_ids(extract_headings(body)) == ["hidden"]


def test_a_marker_in_a_raw_html_block_the_render_swallows_is_not_returned():
    # The tight form matters: a blank line would end the HTML block and make the heading
    # genuinely rendered, which is a different document rather than a weaker version of this one.
    body = "<div>\n## Boxed {#boxed}\n</div>\n"

    assert [(h.line, h.anchor) for h in extract_headings(body)] == [(2, "boxed")]
    assert full_heading_inventory(body) == []
    assert addressable_explicit_markers(body) == frozenset()


def test_a_blank_line_ends_the_html_block_and_the_marker_comes_back():
    # The paired positive for the swallowed case above, which the tight form's comment names but
    # nothing pinned: one blank line closes the raw HTML block, so the same heading is genuinely
    # rendered and its marker qualifies. Without this, an over-exclusion on the rendered side
    # would still pass every container test here.
    assert addressable_explicit_markers("<div>\n\n## Boxed {#boxed}\n</div>\n") == {"boxed"}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_the_source_position_join_survives_every_line_ending(newline: str):
    # The join is only sound because both scanners read one normalized text. A lone CR is the
    # sharp case: the restricted scanner's line map splits on "\n" alone while the pinned
    # parser normalizes CR itself, so dropping the shared normalization would leave the two
    # disagreeing about which line a heading sits on and the intersection would come back empty.
    body = newline.join(["## A {#a}", "", "## B {#b}", ""])

    assert addressable_explicit_markers(body) == {"a", "b"}


def test_the_visible_occurrence_supplies_the_marker_and_the_hidden_one_does_not():
    body = "## Shared {#visible}\n\n<!--\n## Shared {#hidden}\n-->\n"

    assert addressable_explicit_markers(body) == {"visible"}


def test_identical_visible_and_hidden_headings_cannot_validate_each_other():
    # The negative witness for the source-position join. Three leading spaces keep the visible
    # heading out of the addressable subset while leaving it rendered, so the two scanners see
    # disjoint occurrences of the same text: an intersection joined on text alone would admit
    # `shared` even though neither occurrence is both rendered and addressable.
    body = "   ## Shared {#shared}\n\n<!--\n## Shared {#shared}\n-->\n"

    assert [h.line for h in extract_headings(body)] == [4]
    assert [h.line for h in full_heading_inventory(body)] == [1]
    assert addressable_explicit_markers(body) == frozenset()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("Shared {#shared}\n----------------\n", id="setext"),
        pytest.param("   ## Shared {#shared}\n", id="indented_three_spaces"),
        pytest.param("> ## Shared {#shared}\n", id="block_quoted"),
        pytest.param("- ## Shared {#shared}\n", id="list_nested"),
    ],
)
def test_a_marker_outside_the_addressable_subset_is_not_returned(body: str):
    assert full_heading_inventory(body), "the form must still be a rendered heading"
    assert addressable_explicit_markers(body) == frozenset()


def test_a_marker_inside_a_fence_is_not_returned():
    assert addressable_explicit_markers("```\n## Fenced {#fenced}\n```\n") == frozenset()


def test_removing_a_marker_whose_value_equals_the_heading_slug_empties_the_result():
    # The marker-free heading still allocates the GitHub id `fingerprints`, so asking link
    # resolution whether `#fingerprints` resolves answers yes for both bodies. Only this
    # accessor witnesses that the marker itself is gone, which is why a consumer's
    # marker-preservation check cannot be reduced to a link-resolution check.
    marked = "## Fingerprints {#fingerprints}\n"
    unmarked = "## Fingerprints\n"

    assert addressable_explicit_markers(marked) == {"fingerprints"}
    assert addressable_explicit_markers(unmarked) == frozenset()
    assert [record.github_id for record in full_heading_inventory(unmarked)] == ["fingerprints"]
