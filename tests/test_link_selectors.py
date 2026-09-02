"""Tests for the link_sources selector grammar."""

from fnmatch import fnmatchcase

import pytest

from doc_lattice.link_selectors import (
    escape_selector_literal,
    segment_matches,
    validate_link_selector,
)


@pytest.mark.parametrize(
    ("entry", "segments"),
    [
        ("*.md", ("*.md",)),
        ("ARCHITECTURE.md", ("ARCHITECTURE.md",)),
        ("docs/**/*.md", ("docs", "**", "*.md")),
        ("**", ("**",)),
        ("notes [draft]/*.md", ("notes [draft]", "*.md")),
        ("[]]x.md", ("[]]x.md",)),
        ("[!]]x.md", ("[!]]x.md",)),
        ("a]b.md", ("a]b.md",)),
    ],
)
def test_valid_selectors_split_into_segments(entry, segments):
    assert validate_link_selector(entry) == segments


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("", "is empty"),
        ("\x1bdocs/*.md", "control character"),
        ("docs\\guide.md", "backslash"),
        ("/etc/*.md", "is absolute"),
        ("C:docs/*.md", "is absolute"),
        ("docs/", "ends in a separator"),
        ("docs//guide.md", "empty segment"),
        ("./guide.md", "'.' or '..' segment"),
        ("../guide.md", "'.' or '..' segment"),
        ("docs/a**/*.md", r"'\*\*' inside a segment"),
        ("notes[1.md", "unclosed"),
        ("[!x.md", "unclosed"),
    ],
)
def test_invalid_selectors_name_the_defect(entry, reason):
    with pytest.raises(ValueError, match=reason):
        validate_link_selector(entry)


def test_segment_matching_is_case_sensitive():
    assert segment_matches("README.md", "*.md")
    assert not segment_matches("README.MD", "*.md")
    assert not segment_matches("readme.md", "README.md")


def test_segment_matching_carries_fnmatch_classes():
    assert segment_matches("x.md", "[!d]*.md")
    assert not segment_matches("docs.md", "[!d]*.md")
    assert segment_matches("b.md", "[a-c].md")
    assert segment_matches("[.md", "[[].md")


@pytest.mark.parametrize("text", ["notes [draft]", "*.md", "??", "a[b*c?d", "plain.md"])
def test_an_escaped_literal_matches_only_itself(text):
    escaped = escape_selector_literal(text)
    assert fnmatchcase(text, escaped)
    assert validate_link_selector(escaped) == (escaped,)
    assert not fnmatchcase(text + "x", escaped)


def test_escape_spells_each_metacharacter_as_a_class():
    assert escape_selector_literal("a[b*c?d") == "a[[]b[*]c[?]d"
