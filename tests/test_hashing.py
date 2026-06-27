"""Tests for hashing."""

from hypothesis import given
from hypothesis import strategies as st

from game_lattice.hashing import canonicalize, content_hash


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


@given(st.text())
def test_canonicalize_is_idempotent(text: str):
    once = canonicalize(text)
    assert canonicalize(once) == once


@given(st.text())
def test_trailing_whitespace_invariant(text: str):
    # Appending trailing spaces to each line must not change the hash.
    noisy = "\n".join(line + "   " for line in text.split("\n"))
    assert content_hash(text) == content_hash(noisy)
