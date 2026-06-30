"""Tests for text_utils."""

from hypothesis import given
from hypothesis import strategies as st

from game_lattice.text_utils import strip_control_chars


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
