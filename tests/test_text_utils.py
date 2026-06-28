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
