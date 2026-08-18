"""Render pydantic validation failures as the diagnostic lines this project owns.

Both load boundaries validate user-authored YAML against a strict pydantic model: the config
file in ``config.py`` and a document's frontmatter in ``frontmatter_parser.py``. Neither hands
``str(ValidationError)`` to the user, because that renderer emits a ``pydantic.dev`` URL, echoes
the offending input back, and prefixes domain-authored sentences with its own boilerplate. This
module owns the replacement so the two boundaries cannot drift apart.
"""

from collections.abc import Callable

from pydantic import BaseModel, ValidationError

# pydantic prefixes a validator's message with one of these literals when the validator signals
# failure by raising: ``ValueError`` becomes ``value_error``, a bare ``assert`` becomes
# ``assertion_error``. A validator raising ``PydanticCustomError`` gets neither, which is why the
# strip is keyed on the error type rather than attempted on every message.
_BOILERPLATE_PREFIXES = {
    "value_error": "Value error, ",
    "assertion_error": "Assertion failed, ",
}


def format_validation_error(
    exc: ValidationError,
    *,
    header: str,
    model: type[BaseModel],
    root_label: str,
    extra_note: Callable[[tuple[int | str, ...]], str | None] | None = None,
) -> str:
    """Render one pydantic failure as a header plus one diagnostic line per error.

    The message is built from ``exc.errors()`` rather than ``str(exc)`` so the user contract is
    owned here: pydantic's ``url`` and ``input`` fields are dropped, its raise-path boilerplate
    prefix is stripped so a domain-authored sentence reads as written, and its human ``msg`` is
    otherwise kept. A key rejected by ``extra="forbid"`` additionally lists the keys the model
    does accept, derived from ``model.model_fields`` so a future field cannot make the
    diagnostic stale.

    Args:
        exc: The validation error raised by the caller's ``model_validate``.
        header: The first line, naming what failed and the file it came from.
        model: The model that rejected the input, whose fields are the accepted-key list.
        root_label: Rendered in place of a field path when pydantic reports no location.
        extra_note: Optional per-key migration note for a forbidden key, given that key's
            location. Returning None adds nothing. This keeps a caller's one-off migration
            sentence out of the general renderer.

    Returns:
        A multi-line message: the header, then one indented line per validation error.
    """
    lines = [header]
    for error in exc.errors(include_url=False, include_input=False):
        location = _format_location(error["loc"], root_label)
        detail = error["msg"]
        prefix = _BOILERPLATE_PREFIXES.get(error["type"])
        if prefix is not None:
            detail = detail.removeprefix(prefix)
        if error["type"] == "extra_forbidden":
            detail = f"{detail} ({_accepted_key_help(error['loc'], model, extra_note)})"
        lines.append(f"  {location}: {detail}")
    return "\n".join(lines)


def _format_location(location: tuple[int | str, ...], root_label: str) -> str:
    """Render one pydantic error location, marking a whole-input error explicitly.

    pydantic reports an empty location for an error about the input as a whole rather than about
    one field: a validator that runs on the whole model, and also an input that is not a mapping
    at all, which both boundaries can reach because a YAML file may hold a list or a scalar.
    Naming a field there would invent one the user never wrote.

    Args:
        location: The ``loc`` tuple from one pydantic error.
        root_label: Rendered when the location is empty.

    Returns:
        A dotted field path, list indices included, or ``root_label`` for a whole-input error.
    """
    return ".".join(str(part) for part in location) if location else root_label


def _accepted_key_help(
    location: tuple[int | str, ...],
    model: type[BaseModel],
    extra_note: Callable[[tuple[int | str, ...]], str | None] | None,
) -> str:
    """Build the parenthesized help that follows a forbidden-key message.

    Args:
        location: The ``loc`` tuple from one ``extra_forbidden`` error.
        model: The model that rejected the key.
        extra_note: Optional migration note for this key, or None.

    Returns:
        The sorted accepted-key list, preceded by the caller's migration note when it supplies
        one for this key.
    """
    help_text = f"accepted keys: {', '.join(sorted(model.model_fields))}"
    note = extra_note(location) if extra_note is not None else None
    return f"{note} {help_text}" if note is not None else help_text
