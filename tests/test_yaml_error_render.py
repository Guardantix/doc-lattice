"""Tests for the YAML load-failure display spelling."""

import pytest
from ruamel.yaml.error import YAMLError

from doc_lattice.yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader
from doc_lattice.yaml_error_render import format_yaml_error_for_display

# Every code point AD-35 refuses in a frontmatter value, which is the same range
# `text_utils.is_control_char` answers for and the same one AD-34's spelling escapes in a path.
# This module walks it exhaustively rather than sampling it: the renderer is one expression over
# a whole message, so there is no per-code-point branch to sample and no reason to leave a gap.
_CONTROL_CODES = (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0))


def _carries_a_control(text: str) -> bool:
    """Whether any C0, DEL, or C1 code point survives into rendered text."""
    return any(ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F for char in text)


@pytest.mark.parametrize("code", _CONTROL_CODES, ids=[f"{code:#04x}" for code in _CONTROL_CODES])
def test_no_control_code_point_survives_the_spelling(code: int):
    # The renderer's whole job. `ruamel` builds the message, so a document reaches it through
    # whatever that message echoes back, and every code point in the range has to be neutralized
    # rather than the ESC the vector was reported for.
    rendered = format_yaml_error_for_display(YAMLError(f"found duplicate key {chr(code)} here"))

    assert not _carries_a_control(rendered)


@pytest.mark.parametrize("code", _CONTROL_CODES, ids=[f"{code:#04x}" for code in _CONTROL_CODES])
def test_the_spelling_is_the_repr_of_the_message(code: int):
    # Pinned as a relation to the exception rather than as a literal, because CPython names some
    # of these escapes (`\n`) and hex-spells the rest (`\x1b`), and the contract is AD-34's
    # "the active supported interpreter's repr", not a byte table this suite would have to own.
    exc = YAMLError(f"boom{chr(code)}tail")

    assert format_yaml_error_for_display(exc) == repr(str(exc))


def test_the_spelling_is_injective_over_an_escape_and_its_literal_text():
    # Injectivity is what AD-34 rests on and what a deleting cleaner (`strip_control_chars`)
    # cannot give: it would render these two identically, and a diagnostic that maps two
    # different documents onto one string is not one a reader can act on.
    real = format_yaml_error_for_display(YAMLError(f"value {chr(0x1B)}[31m"))
    literal = format_yaml_error_for_display(YAMLError("value \\x1b[31m"))

    assert real != literal


def test_a_line_break_in_the_message_is_escaped_rather_than_kept():
    # The recorded cost, and the reason the preserve-line-breaks branch was rejected: once
    # `ruamel` has joined its context, marks, and problem into one string, a break it wrote and
    # a break decoded from an echoed value are the same character. Spelling all of them is what
    # stops a document forging a diagnostic line, which is AD-35's rule for a frontmatter value.
    rendered = format_yaml_error_for_display(YAMLError("while parsing\n  in <unicode string>"))

    assert "\n" not in rendered
    assert "\\n" in rendered


def test_a_backslash_in_the_message_cannot_forge_an_escape():
    # A document echoed back can spell `\x1b` as four ordinary characters. Doubling the
    # backslash is what keeps that distinct from the code point itself.
    rendered = format_yaml_error_for_display(YAMLError("value \\x1b"))

    assert "\\\\x1b" in rendered


def test_printable_non_ascii_in_the_message_survives_verbatim():
    # The rule is about bytes a terminal acts on, not about unfamiliar text. A path renders
    # `café` unchanged under the same spelling, and a YAML message naming a key has to as well.
    assert "café" in format_yaml_error_for_display(YAMLError("found duplicate key café"))


@pytest.mark.parametrize(
    "error", [error_type("k") for error_type in YAML_LOAD_ERRORS], ids=lambda e: type(e).__name__
)
def test_every_member_of_the_caught_family_renders_through_the_same_spelling(error: Exception):
    # The family is not one type: a constructor building a tagged scalar raises the builtin its
    # type rejected, and an `!!omap` duplicate arrives as an AssertionError. All five render
    # through `str`, so none needs a case of its own. `KeyError` is the one that surprises,
    # since `str(KeyError("k"))` is already quoted and comes back nested.
    assert format_yaml_error_for_display(error) == repr(str(error))


def test_a_real_duplicate_key_error_renders_free_of_the_bytes_it_echoes():
    # The vector end to end at the renderer's own level: `ruamel` echoes the duplicate key and
    # both of its values, so the control character reaches this module inside a message no rule
    # of this project built. The value half is the one AD-35 could not reach, because the load
    # aborts before validation runs.
    loader = SafeYamlLoader(parser="pure")
    with pytest.raises(YAML_LOAD_ERRORS) as caught:
        loader.load('k: "v\\u001b[31mA"\nk: "v\\u001b[31mB"\n')

    rendered = format_yaml_error_for_display(caught.value)

    assert _carries_a_control(str(caught.value))
    assert not _carries_a_control(rendered)
    assert rendered == repr(str(caught.value))
