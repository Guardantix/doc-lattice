"""Pure authored-marker taint analysis for one CI shell run body."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

TAINT_REFUSAL_REASON = "authored marker flow reaches an execution sink"


@dataclass(frozen=True, slots=True)
class LiteralTransfer:
    """Authored literal bytes transferred into one content stream."""

    text: str


@dataclass(frozen=True, slots=True)
class VariableRef:
    """Reference one shell variable's joined authored definitions."""

    name: str


@dataclass(frozen=True, slots=True)
class StreamRef:
    """Reference one stream scope's aggregated stdout."""

    scope_id: int


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Reference one normalized static resource's content."""

    key: str


@dataclass(frozen=True, slots=True)
class Choice:
    """Mutually exclusive in-word or control-flow alternatives."""

    parts: tuple[ContentExpr, ...]


@dataclass(frozen=True, slots=True)
class Concat:
    """Ordered adjacent fragments in one byte stream."""

    parts: tuple[ContentExpr, ...]


@dataclass(frozen=True, slots=True)
class OutsideGap:
    """An external-content boundary with epsilon and opaque-barrier alternatives."""


ContentExpr: TypeAlias = (  # noqa: UP040
    LiteralTransfer | VariableRef | StreamRef | ResourceRef | Choice | Concat | OutsideGap
)


def concat(*parts: ContentExpr) -> ContentExpr:
    """Join adjacent content expressions, removing authored epsilon literals."""
    flattened: list[ContentExpr] = []
    for part in parts:
        if isinstance(part, Concat):
            flattened.extend(part.parts)
        elif not isinstance(part, LiteralTransfer) or part.text:
            flattened.append(part)

    if not flattened:
        return LiteralTransfer("")
    if len(flattened) == 1:
        return flattened[0]
    return Concat(tuple(flattened))


def choice(*parts: ContentExpr) -> ContentExpr:
    """Join mutually exclusive content expressions while retaining epsilon alternatives."""
    flattened: list[ContentExpr] = []
    for part in parts:
        if isinstance(part, Choice):
            flattened.extend(part.parts)
        else:
            flattened.append(part)

    if len(flattened) == 1:
        return flattened[0]
    return Choice(tuple(flattened))


_DFA_STATE_COUNT = 11
_DFA_START = 0
_SEPARATORS = frozenset("-_.")
_DOC_SEPARATOR_STATE = 3
_LATTICE_START_STATE = 4
_DFA_FINAL_STATE = 10
_DFA_EXPECTED_CHARACTERS = ("", "o", "c", "", "", "a", "t", "t", "i", "c", "e")


def _ascii_lower(character: str) -> str:
    if "A" <= character <= "Z":
        return chr(ord(character) + (ord("a") - ord("A")))
    return character


def _dfa_step(state: int, character: str) -> tuple[int, bool]:
    character = _ascii_lower(character)
    restart_state = 1 if character == "d" else 0
    accepted = False
    if state == 0:
        next_state = restart_state
    elif state == _DOC_SEPARATOR_STATE:
        next_state = 4 if character in _SEPARATORS else restart_state
    elif state == _LATTICE_START_STATE:
        if character in _SEPARATORS:
            next_state = 4
        elif character == "l":
            next_state = 5
        else:
            next_state = restart_state
    else:
        expected = _DFA_EXPECTED_CHARACTERS[state]
        if character == expected:
            accepted = state == _DFA_FINAL_STATE
            next_state = 0 if accepted else state + 1
        else:
            next_state = restart_state
    return next_state, accepted


@dataclass(frozen=True, slots=True)
class _DfaTransfer:
    entries: tuple[tuple[int, bool], ...]

    @classmethod
    def identity(cls) -> _DfaTransfer:
        """Return the transfer that preserves every DFA state."""
        return cls(tuple((state, False) for state in range(_DFA_STATE_COUNT)))

    @classmethod
    def barrier(cls) -> _DfaTransfer:
        """Return the transfer that discards partial authored marker state."""
        return cls(tuple((0, False) for _ in range(_DFA_STATE_COUNT)))

    @classmethod
    def literal(cls, text: str) -> _DfaTransfer:
        """Return the transfer induced by authored literal text."""
        entries: list[tuple[int, bool]] = []
        for start_state in range(_DFA_STATE_COUNT):
            state = start_state
            accepted = False
            for character in text:
                state, step_accepted = _dfa_step(state, character)
                accepted = accepted or step_accepted
            entries.append((state, accepted))
        return cls(tuple(entries))

    def compose(self, following: _DfaTransfer) -> _DfaTransfer:
        """Return this transfer followed by another transfer."""
        return _DfaTransfer(
            tuple(
                (
                    following.entries[intermediate][0],
                    accepted_before or following.entries[intermediate][1],
                )
                for intermediate, accepted_before in self.entries
            )
        )


@dataclass(frozen=True, slots=True)
class _TransferSummary:
    full: _DfaTransfer
    stripped: _DfaTransfer
    newline_only: bool

    @classmethod
    def literal(cls, text: str) -> _TransferSummary:
        """Return a summary for authored literal text."""
        return cls(
            full=_DfaTransfer.literal(text),
            stripped=_DfaTransfer.literal(text.rstrip("\n")),
            newline_only=all(character == "\n" for character in text),
        )

    @classmethod
    def barrier(cls) -> _TransferSummary:
        """Return a summary for opaque external content."""
        transfer = _DfaTransfer.barrier()
        return cls(full=transfer, stripped=transfer, newline_only=False)

    def compose(self, following: _TransferSummary) -> _TransferSummary:
        """Return this summary followed by another summary."""
        stripped = (
            self.stripped if following.newline_only else self.full.compose(following.stripped)
        )
        return _TransferSummary(
            full=self.full.compose(following.full),
            stripped=stripped,
            newline_only=self.newline_only and following.newline_only,
        )


_ContentValue: TypeAlias = frozenset[_TransferSummary]  # noqa: UP040
_EPSILON = _TransferSummary.literal("")
_OUTSIDE_VALUE = frozenset({_EPSILON, _TransferSummary.barrier()})


def _join_values(*values: _ContentValue) -> _ContentValue:
    return frozenset(summary for value in values for summary in value)


def _compose_values(left: _ContentValue, right: _ContentValue) -> _ContentValue:
    return frozenset(before.compose(after) for before in left for after in right)


def _strip_trailing_newlines(value: _ContentValue) -> _ContentValue:
    return frozenset(
        _TransferSummary(
            full=alternative.stripped,
            stripped=alternative.stripped,
            newline_only=alternative.newline_only,
        )
        for alternative in value
    )


def _marker_capable(value: _ContentValue) -> bool:
    return any(alternative.full.entries[_DFA_START][1] for alternative in value)


def _evaluate_closed(expression: ContentExpr) -> _ContentValue:
    if isinstance(expression, LiteralTransfer):
        return frozenset({_TransferSummary.literal(expression.text)})
    if isinstance(expression, OutsideGap):
        return _OUTSIDE_VALUE
    if isinstance(expression, Choice):
        return _join_values(*(_evaluate_closed(part) for part in expression.parts))
    if isinstance(expression, Concat):
        value = frozenset({_EPSILON})
        for part in expression.parts:
            value = _compose_values(value, _evaluate_closed(part))
        return value
    raise ValueError("closed content expression contains an unresolved reference")
