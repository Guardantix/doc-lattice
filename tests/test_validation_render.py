"""Tests for the shared pydantic validation diagnostic renderer."""

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from doc_lattice.validation_render import format_validation_error


class _Nested(BaseModel):
    """A stand-in for a model reached through a field of the root."""

    model_config = ConfigDict(strict=True, extra="forbid")

    ref: str = "r"


class _Sample(BaseModel):
    """A stand-in model exercising every rendering branch in one place."""

    model_config = ConfigDict(strict=True, extra="forbid")

    name: str = "x"
    tags: list[str] = []
    edges: list[_Nested] = []
    single: _Nested | None = None

    @model_validator(mode="after")
    def _name_is_not_reserved(self) -> "_Sample":
        if self.name == "reserved":
            msg = "name 'reserved' is taken; pick another"
            raise ValueError(msg)
        return self


def _render(
    raw: object,
    extra_note: Callable[[tuple[int | str, ...]], str | None] | None = None,
) -> str:
    try:
        _Sample.model_validate(raw)
    except ValidationError as exc:
        return format_validation_error(
            exc,
            header="invalid sample:",
            model=_Sample,
            root_label="<sample>",
            extra_note=extra_note,
        )
    raise AssertionError("input was expected to fail validation")


def test_header_leads_and_each_error_gets_its_own_indented_line():
    message = _render({"name": 1, "bogus": 2})

    lines = message.splitlines()
    assert lines[0] == "invalid sample:"
    assert len(lines) == 3
    assert all(line.startswith("  ") for line in lines[1:])


def test_pydantic_url_and_echoed_input_are_dropped():
    message = _render({"name": 1})

    assert "pydantic.dev" not in message
    assert "input_value" not in message
    assert "[type=" not in message


def test_value_error_boilerplate_prefix_is_stripped():
    message = _render({"name": "reserved"})

    assert message == "invalid sample:\n  <sample>: name 'reserved' is taken; pick another"


def test_empty_location_renders_the_caller_s_root_label():
    # A non-mapping input reports no location, exactly as a whole-model validator does.
    message = _render([1, 2])

    assert message.splitlines()[1].startswith("  <sample>: ")


def test_list_index_is_part_of_the_location():
    message = _render({"tags": ["ok", 5]})

    assert "  tags.1: " in message


def test_forbidden_key_lists_the_models_accepted_keys():
    message = _render({"bogus": 1})

    assert "(accepted keys: edges, name, single, tags)" in message


def test_forbidden_key_inside_a_list_of_models_lists_that_models_keys():
    # The root's fields are invalid exactly where the user is editing, so offering them would
    # send them from one error straight into the next.
    message = _render({"edges": [{"ref": "a", "bogus": 1}]})

    assert "  edges.0.bogus: " in message
    assert "(accepted keys: ref)" in message


def test_forbidden_key_inside_a_direct_nested_model_lists_that_models_keys():
    message = _render({"single": {"ref": "a", "bogus": 1}})

    assert "(accepted keys: ref)" in message


def test_forbidden_key_under_an_unresolvable_path_offers_no_key_list():
    # Better to say nothing than to name the wrong model's fields. A dict-valued field has no
    # single model behind it, so the walk gives up rather than guessing.
    class _Opaque(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")

        payload: dict[str, "_Leaf"] = {}

    class _Leaf(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")

        value: int = 0

    _Opaque.model_rebuild()

    with pytest.raises(ValidationError) as exc:
        _Opaque.model_validate({"payload": {"k": {"value": 1, "bogus": 2}}})

    message = format_validation_error(
        exc.value,
        header="invalid:",
        model=_Opaque,
        root_label="<root>",
    )

    assert "  payload.k.bogus: Extra inputs are not permitted" in message
    assert "accepted keys" not in message
    assert "()" not in message  # no empty parenthetical left behind


def test_extra_note_precedes_the_accepted_keys_for_the_key_it_matches():
    def note(location: tuple[int | str, ...]) -> str | None:
        return "gone since 2.0." if location == ("bogus",) else None

    message = _render({"bogus": 1}, note)

    assert "(gone since 2.0. accepted keys: edges, name, single, tags)" in message


def test_extra_note_returning_none_adds_nothing():
    def note(location: tuple[int | str, ...]) -> str | None:
        del location
        return None

    assert _render({"bogus": 1}, note) == _render({"bogus": 1})


def test_assertion_error_boilerplate_prefix_is_stripped():
    # No model in the project uses a bare `assert` in a validator today. The prefix map covers
    # it anyway so the next one that does cannot silently reintroduce pydantic boilerplate.
    class _Asserting(BaseModel):
        value: int = 0

        @model_validator(mode="after")
        def _value_is_small(self) -> "_Asserting":
            assert self.value < 10, "value must stay under 10"
            return self

    with pytest.raises(ValidationError) as exc:
        _Asserting.model_validate({"value": 11})

    message = format_validation_error(
        exc.value,
        header="invalid:",
        model=_Asserting,
        root_label="<root>",
    )

    assert "Assertion failed," not in message
    assert "value must stay under 10" in message
