"""Golden tests for the versioned Markdown compatibility adapter."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from doc_lattice.markdown_compat import (
    SLUG_UNICODE_VERSION,
    anchor_ids,
    extract_headings,
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
