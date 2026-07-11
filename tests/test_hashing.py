"""Tests for hashing."""

from hypothesis import given
from hypothesis import strategies as st

from doc_lattice.hashing import canonicalize, content_hash


def test_canonicalize_strips_trailing_ws_and_blank_edges():
    assert canonicalize("\n\n  hi  \nthere \n\n") == "  hi\nthere"


def test_content_hash_is_32_hex_chars():
    h = content_hash("anything")
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)


def test_crlf_and_final_newline_do_not_change_hash():
    base = "# Title\n\nbody line\n"
    assert content_hash(base) == content_hash("# Title\r\n\r\nbody line")
    assert content_hash(base) == content_hash("# Title\n\nbody line\n\n\n")


def test_substantive_change_changes_hash_examples():
    assert content_hash("accent: blue") != content_hash("accent: red")
    assert content_hash("a\nb") != content_hash("a\nb\nc")


def test_lone_cr_line_endings_do_not_change_hash():
    # Lone CR (classic-Mac) endings normalize to LF just like CRLF.
    assert content_hash("a\nb\nc") == content_hash("a\rb\rc")
    assert canonicalize("a\rb") == "a\nb"


def test_canonicalize_empty_and_all_blank_collapse_to_empty():
    # The trim loops advance start all the way to end on empty or all-blank input.
    assert canonicalize("") == ""
    assert canonicalize("\n\n\n") == ""
    assert canonicalize("   \n\t\n  ") == ""


def test_blank_inputs_all_share_one_hash():
    h = content_hash("")
    assert content_hash("\n\n") == h
    assert content_hash("   \n  ") == h
    assert len(h) == 32


def test_canonicalize_keeps_internal_blank_lines():
    # Edge blanks trimmed, interior run of blanks preserved verbatim.
    assert canonicalize("\n\na\n\n\nb\n\n") == "a\n\n\nb"


def test_content_hash_known_value_is_stable():
    # Golden digest pins the on-disk wire format of stored ``seen`` values; a change to the
    # algorithm, canonicalization, or 32-char truncation breaks this.
    expected = "452f0d378efb40e81b013395da6bc577"  # pragma: allowlist secret
    assert content_hash("# Title\n\nbody line\n") == expected


@given(st.text())
def test_canonicalize_is_idempotent(text: str):
    once = canonicalize(text)
    assert canonicalize(once) == once


@given(st.text())
def test_trailing_whitespace_invariant(text: str):
    # Appending trailing spaces to each already-canonical line must not change the hash.
    canonical = canonicalize(text)
    noisy = "\n".join(line + "   " for line in canonical.split("\n"))
    assert content_hash(text) == content_hash(noisy)
