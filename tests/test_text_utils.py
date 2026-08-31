"""Tests for text_utils."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from doc_lattice.text_utils import (
    first_control_index,
    is_control_char,
    safe_heading_label,
    strip_control_chars,
)


def test_strips_escape_and_controls():
    assert strip_control_chars("a\x1b[31mb\x07c\x7f") == "a[31mbc"


def test_strips_c1_controls():
    # 0x9B (CSI), 0x85 (NEL), and the C1 boundaries 0x80/0x9F all drive 8-bit terminals.
    assert strip_control_chars("a\x9bb\x85c\x80d\x9fe") == "abcde"


def test_keeps_ordinary_text():
    assert strip_control_chars("PC-228 Done") == "PC-228 Done"


def test_keeps_non_ascii_and_nbsp_boundary():
    # 0xA0 (NBSP) sits one above C1_CONTROL_MAX (0x9F) and must survive;
    # accented letters, CJK, and emoji are ordinary printables, not controls.
    assert strip_control_chars("café 日本 \U0001f3ae") == "café 日本 \U0001f3ae"
    assert strip_control_chars("a\xa0b") == "a\xa0b"


@given(st.text())
def test_output_has_no_control_bytes(text: str):
    cleaned = strip_control_chars(text)
    assert all(
        ord(ch) >= 0x20 and ord(ch) != 0x7F and not (0x80 <= ord(ch) <= 0x9F) for ch in cleaned
    )


@given(st.text())
def test_is_idempotent(text: str):
    once = strip_control_chars(text)
    assert strip_control_chars(once) == once


@given(st.text(alphabet=st.characters(exclude_categories=["Cc"])))
def test_preserves_control_free_text(text: str):
    # 'Cc' == U+0000-001F, U+007F-009F: the full set strip_control_chars removes,
    # so any control-free string must come back unchanged.
    assert strip_control_chars(text) == text


@pytest.mark.parametrize("code", [0x00, 0x09, 0x0A, 0x0D, 0x1B, 0x1F, 0x7F, 0x80, 0x85, 0x9B, 0x9F])
def test_is_control_char_covers_c0_delete_and_c1(code: int):
    assert is_control_char(chr(code))


@pytest.mark.parametrize("code", [0x20, 0x41, 0x7E, 0xA0, 0xE9, 0x2028, 0xFEFF, 0x1F3AE])
def test_is_control_char_leaves_printables_and_the_range_neighbors_alone(code: int):
    # 0x20 and 0x7E bracket printable ASCII, 0xA0 sits one above C1_CONTROL_MAX, and the last
    # three are ordinary text: a widened predicate would take these first.
    assert not is_control_char(chr(code))


def test_first_control_index_finds_the_earliest_control():
    assert first_control_index("ab\x1bc\x07d") == 2


def test_first_control_index_is_none_for_control_free_text():
    assert first_control_index("café 日本 \U0001f3ae") is None
    assert first_control_index("") is None


@given(st.text())
def test_first_control_index_agrees_with_strip_control_chars(text: str):
    # The two helpers answer the same question in different shapes, so a change to one that
    # did not move the other would show up here rather than as a diagnostic that names a
    # position nothing was removed at.
    assert (first_control_index(text) is None) == (strip_control_chars(text) == text)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\x1b]0;title\x07Setup", "]0;titleSetup"),
        ("\x9bmSetup", "mSetup"),
        ("Setup\x7f", "Setup"),
        ("Se\x00tup", "Setup"),
        ("Ordinary Heading", "Ordinary Heading"),
        ("Ünïcode ok", "Ünïcode ok"),
    ],
)
def test_safe_heading_label_strips_every_control_range(raw: str, expected: str):
    assert safe_heading_label(raw) == expected
