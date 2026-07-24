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


@dataclass(frozen=True, slots=True)
class TaintLimits:
    """Deterministic caps for one taint pass."""

    max_alternatives: int = 256
    max_expression_nodes: int = 100_000
    max_table_entries: int = 10_000
    max_edges: int = 50_000
    max_fixed_point_updates: int = 100_000
    max_brace_expansions: int = 256
    max_brace_depth: int = 16


class _TaintLimitExceeded(RuntimeError):
    """A deterministic taint bound prevented certification."""


@dataclass(frozen=True, slots=True)
class _FlowWrite:
    key: str | int
    expression: ContentExpr
    append: bool = False
    strip_trailing_newlines: bool = False


@dataclass(frozen=True, slots=True)
class _FlowDefinitions:
    variable_writes: tuple[_FlowWrite, ...] = ()
    resource_writes: tuple[_FlowWrite, ...] = ()
    stream_writes: tuple[_FlowWrite, ...] = ()


def concat(*parts: ContentExpr) -> ContentExpr:
    """Flatten concatenation and discard authored epsilon fragments."""
    flattened: list[ContentExpr] = []

    def append_part(part: ContentExpr) -> None:
        if isinstance(part, Concat):
            for nested_part in part.parts:
                append_part(nested_part)
        elif not isinstance(part, LiteralTransfer) or part.text:
            flattened.append(part)

    for part in parts:
        append_part(part)

    if not flattened:
        return LiteralTransfer("")
    if len(flattened) == 1:
        return flattened[0]
    return Concat(tuple(flattened))


def choice(*parts: ContentExpr) -> ContentExpr:
    """Flatten choices while retaining epsilon as a real alternative."""
    flattened: list[ContentExpr] = []

    def append_part(part: ContentExpr) -> None:
        if isinstance(part, Choice):
            for nested_part in part.parts:
                append_part(nested_part)
        else:
            flattened.append(part)

    for part in parts:
        append_part(part)

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
    """Deterministic exit and acceptance result for every possible entry state."""

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
    """Raw and trailing-newline-stripped DFA effects for one alternative."""

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


def _expression_nodes(expression: ContentExpr) -> int:
    if isinstance(expression, Choice | Concat):
        return 1 + sum(_expression_nodes(part) for part in expression.parts)
    return 1


def _expression_edges(expression: ContentExpr) -> int:
    if isinstance(expression, VariableRef | ResourceRef | StreamRef):
        return 1
    if isinstance(expression, Choice | Concat):
        return sum(_expression_edges(part) for part in expression.parts)
    return 0


def _cap_value(value: _ContentValue, limits: TaintLimits) -> _ContentValue:
    if len(value) > limits.max_alternatives:
        raise _TaintLimitExceeded("shell taint alternative limit exceeded")
    return value


def _definition_counts(definitions: _FlowDefinitions) -> tuple[int, int, int]:
    writes = definitions.variable_writes + definitions.resource_writes + definitions.stream_writes
    nodes = sum(_expression_nodes(write.expression) for write in writes)
    edges = len(writes) + sum(_expression_edges(write.expression) for write in writes)
    entries = len(
        {
            *(("variable", write.key) for write in definitions.variable_writes),
            *(("resource", write.key) for write in definitions.resource_writes),
            *(("stream", write.key) for write in definitions.stream_writes),
        }
    )
    return nodes, edges, entries


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


def _evaluate_with_tables(
    expression: ContentExpr,
    variables: dict[str | int, _ContentValue],
    resources: dict[str | int, _ContentValue],
    streams: dict[str | int, _ContentValue],
    limits: TaintLimits,
) -> _ContentValue:
    if isinstance(expression, LiteralTransfer):
        value = frozenset({_TransferSummary.literal(expression.text)})
    elif isinstance(expression, OutsideGap):
        value = _OUTSIDE_VALUE
    elif isinstance(expression, VariableRef):
        value = variables.get(expression.name, _OUTSIDE_VALUE)
    elif isinstance(expression, ResourceRef):
        value = resources.get(expression.key, _OUTSIDE_VALUE)
    elif isinstance(expression, StreamRef):
        value = streams.get(expression.scope_id, _OUTSIDE_VALUE)
    elif isinstance(expression, Choice):
        value = _join_values(
            *(
                _evaluate_with_tables(part, variables, resources, streams, limits)
                for part in expression.parts
            )
        )
    else:
        value = frozenset({_EPSILON})
        for part in expression.parts:
            value = _cap_value(
                _compose_values(
                    value,
                    _evaluate_with_tables(part, variables, resources, streams, limits),
                ),
                limits,
            )
    return _cap_value(value, limits)


@dataclass(frozen=True, slots=True)
class _SolvedFlow:
    variables: dict[str | int, _ContentValue]
    resources: dict[str | int, _ContentValue]
    streams: dict[str | int, _ContentValue]
    limits: TaintLimits

    def evaluate(self, expression: ContentExpr) -> _ContentValue:
        """Evaluate one expression against the solved typed tables."""
        return _evaluate_with_tables(
            expression,
            self.variables,
            self.resources,
            self.streams,
            self.limits,
        )


def _solve_flow_definitions(
    definitions: _FlowDefinitions,
    *,
    limits: TaintLimits = TaintLimits(),  # noqa: B008
) -> _SolvedFlow:
    nodes, edges, entries = _definition_counts(definitions)
    if nodes > limits.max_expression_nodes:
        raise _TaintLimitExceeded("shell taint expression node limit exceeded")
    if edges > limits.max_edges:
        raise _TaintLimitExceeded("shell taint edge limit exceeded")
    if entries > limits.max_table_entries:
        raise _TaintLimitExceeded("shell taint table entry limit exceeded")

    variables: dict[str | int, _ContentValue] = {}
    resources: dict[str | int, _ContentValue] = {}
    streams: dict[str | int, _ContentValue] = {}
    writes = (
        *(("variable", write) for write in definitions.variable_writes),
        *(("resource", write) for write in definitions.resource_writes),
        *(("stream", write) for write in definitions.stream_writes),
    )
    updates = 0

    changed = True
    while changed:
        changed = False
        for kind, write in writes:
            table = {
                "variable": variables,
                "resource": resources,
                "stream": streams,
            }[kind]
            value = _evaluate_with_tables(write.expression, variables, resources, streams, limits)
            if write.strip_trailing_newlines:
                value = _cap_value(_strip_trailing_newlines(value), limits)
            prior = table.get(write.key, frozenset())
            if write.append:
                base = prior or _OUTSIDE_VALUE
                value = _cap_value(_compose_values(base, value), limits)
            widened = _cap_value(_join_values(prior, value), limits)
            if widened == prior:
                continue
            table[write.key] = widened
            updates += 1
            if updates > limits.max_fixed_point_updates:
                raise _TaintLimitExceeded("shell taint fixed-point update limit exceeded")
            changed = True

    return _SolvedFlow(variables, resources, streams, limits)
