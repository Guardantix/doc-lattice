"""Tests for the selector grammar the link source keys share."""

import sys
from fnmatch import fnmatchcase

import pytest

from doc_lattice.link_selectors import (
    escape_selector_literal,
    segment_matches,
    selector_matches_path,
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


def _matches(entry: str, path: str) -> bool:
    return selector_matches_path(validate_link_selector(entry), path)


@pytest.mark.parametrize(
    ("entry", "path"),
    [
        # A selector is anchored at the project root and matches a whole spelling.
        ("*.md", "README.md"),
        ("ARCHITECTURE.md", "ARCHITECTURE.md"),
        ("docs/a.md", "docs/a.md"),
        # `**` matches zero directories, which is the case a naive one-or-more reading loses.
        ("docs/**/*.md", "docs/a.md"),
        ("docs/**/*.md", "docs/deep/b.md"),
        ("docs/**/*.md", "docs/deep/deeper/c.md"),
        # A terminal `**` covers the directory's own files as well as everything beneath.
        ("docs/**", "docs/a.md"),
        ("docs/**", "docs/deep/b.txt"),
        ("**", "a.md"),
        ("**", "docs/deep/b.md"),
        # Adjacent recursive segments are valid grammar and mean what one `**` means.
        ("a/**/**/**/b.md", "a/b.md"),
        ("a/**/**/**/b.md", "a/x/y/z/b.md"),
        # Classes and wildcards still stop at a separator.
        ("docs/[ab].md", "docs/a.md"),
    ],
)
def test_a_selector_matches_the_spellings_the_walk_would_have_found(entry, path):
    assert _matches(entry, path)


@pytest.mark.parametrize(
    ("entry", "path"),
    [
        # Anchoring: a root selector never reaches into a directory, and a nested one never
        # floats free of the prefix it names.
        ("*.md", "docs/c.md"),
        ("docs/*.md", "a.md"),
        ("docs/*.md", "other/a.md"),
        ("docs/**/*.md", "a.md"),
        # A wildcard never crosses a separator, whatever the depth.
        ("*", "docs/a.md"),
        ("docs/*.md", "docs/deep/b.md"),
        # A terminal `**` still requires the prefix it trails, and a directory is not a match.
        ("docs/**", "notes/a.md"),
        ("docs/**", "docs"),
        # Matching is case-sensitive by code point, exactly as the walk is.
        ("*.md", "README.MD"),
        # The last segment must match the file, not a directory on the way to it.
        ("docs/deep", "docs/deep/b.md"),
        # Suffix and prefix are both part of the match; nothing is anchored loosely.
        ("a/**/b.md", "a/x/c.md"),
    ],
)
def test_a_selector_matches_nothing_the_walk_would_have_skipped(entry, path):
    assert not _matches(entry, path)


def test_a_path_deeper_than_the_recursion_limit_is_matched():
    """The ceiling the walk keeps its own stack to avoid must not come back from this side.

    A compatibility declaration is matched against every retained spelling on a mandatory gate,
    so a repository deeper than the interpreter's limit would end the run in a traceback rather
    than a verdict if matching recursed.
    """
    depth = sys.getrecursionlimit() + 50
    path = "/".join(["d"] * depth) + "/deep.md"

    assert _matches("**/*.md", path)
    assert _matches("d/**/deep.md", path)
    assert not _matches("d/**/other.md", path)
