"""Pure authored-marker taint analysis for one CI shell run body."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import TypeAlias

from doc_lattice.error_types import ProjectError

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


class _TaintLimitExceeded(ProjectError):
    """A deterministic taint bound prevented certification."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SHELL_TAINT_LIMIT_EXCEEDED")


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


@dataclass(frozen=True, slots=True)
class _ArgPort:
    """One shell argv port and its authored content transfer."""

    literal: str
    content: ContentExpr
    dynamic: bool = False
    process_resource_id: int | None = None


@dataclass(frozen=True, slots=True)
class _AssignmentEvidence:
    """One shell variable assignment from the parsed run body."""

    name: str
    content: ContentExpr
    append: bool = False


@dataclass(frozen=True, slots=True)
class _ExecutableEvidence:
    """The resolved executable port for one command."""

    argv_index: int | None
    name: str | None
    literal: str | None
    external_lookup: bool = False
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class StaticResourceTarget:
    """A statically named filesystem resource."""

    key: str


@dataclass(frozen=True, slots=True)
class DynamicResourceTarget:
    """A resource whose name cannot be determined statically."""


@dataclass(frozen=True, slots=True)
class ContentTarget:
    """Authored content directly connected to a descriptor."""

    content: ContentExpr


@dataclass(frozen=True, slots=True)
class ProcessResourceTarget:
    """A process-substitution resource connected to a descriptor."""

    resource_id: int


@dataclass(frozen=True, slots=True)
class DescriptorTarget:
    """A duplicated file descriptor target."""

    descriptor: int


@dataclass(frozen=True, slots=True)
class NullTarget:
    """The shell null device target."""


RedirectionTarget: TypeAlias = (  # noqa: UP040
    StaticResourceTarget
    | DynamicResourceTarget
    | ContentTarget
    | ProcessResourceTarget
    | DescriptorTarget
    | NullTarget
)


@dataclass(frozen=True, slots=True)
class _RedirectionEvent:
    """One ordered descriptor mutation."""

    ordinal: int
    operator: str
    descriptor: int | None
    target: RedirectionTarget


@dataclass(frozen=True, slots=True)
class CommandOutput:
    """Output from one command within a structured stream scope."""

    command_id: int


@dataclass(frozen=True, slots=True)
class SequenceOutput:
    """Outputs concatenated in execution order."""

    parts: tuple[OutputExpr, ...]


@dataclass(frozen=True, slots=True)
class ChoiceOutput:
    """Mutually exclusive output alternatives."""

    parts: tuple[OutputExpr, ...]


@dataclass(frozen=True, slots=True)
class RepeatOutput:
    """One output repeated an arbitrary number of times."""

    part: OutputExpr


OutputExpr: TypeAlias = CommandOutput | SequenceOutput | ChoiceOutput | RepeatOutput  # noqa: UP040


@dataclass(frozen=True, slots=True)
class _CommandEvidence:
    """Typed evidence for one parsed shell command."""

    command_id: int
    output_scope_id: int
    container_scope_id: int
    argv: tuple[_ArgPort, ...]
    assignments: tuple[_AssignmentEvidence, ...]
    redirections: tuple[_RedirectionEvent, ...]
    executable: _ExecutableEvidence


@dataclass(frozen=True, slots=True)
class _StreamScopeEvidence:
    """A structured shell stream scope."""

    scope_id: int
    kind: str
    parent_scope_id: int | None
    parent_command_id: int | None
    output: OutputExpr
    redirections: tuple[_RedirectionEvent, ...] = ()
    loop_bindings: tuple[_AssignmentEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class _PipeEvidence:
    """A producer scope piped to a consumer command."""

    producer_scope_id: int
    consumer_command_id: int


@dataclass(frozen=True, slots=True)
class _ProcessResourceEvidence:
    """A process-substitution resource and its producer stream."""

    resource_id: int
    scope_id: int
    direction: str


@dataclass(frozen=True, slots=True)
class _ShellTaintEvidence:
    """Typed shell execution evidence for one CI run body."""

    commands: tuple[_CommandEvidence, ...] = ()
    scopes: tuple[_StreamScopeEvidence, ...] = ()
    pipes: tuple[_PipeEvidence, ...] = ()
    process_resources: tuple[_ProcessResourceEvidence, ...] = ()


def normalize_static_resource(literal: str, *, dynamic: bool) -> str | None:
    """Return a lexical static resource key without resolving filesystem state."""
    if dynamic or not literal:
        return None
    path = PurePosixPath(literal.replace("\\", "/"))
    if ".." in path.parts:
        return None
    absolute = path.is_absolute()
    parts = tuple(part for part in path.parts if part not in ("", ".", "/") and part.strip("/"))
    if not parts:
        return "/" if absolute else "."
    prefix = "/" if absolute else ""
    return prefix + "/".join(parts)


_SHELL_HEADS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "rbash", "rzsh", "rksh"})
_SHELL_LONG_OPTIONS_WITH_VALUE = frozenset({"--rcfile", "--init-file", "--emulate"})
_SHELL_EAGER_STOPS = frozenset({"--help", "--version", "--dump-strings", "--dump-po-strings"})
_INPUT_REDIRECTION_OPERATORS = frozenset({"<", "<<", "<<-", "<<<", "<&", "<>"})
_OUTPUT_REDIRECTION_OPERATORS = frozenset({">", ">|", ">>", ">&", "<>", "&>", "&>>"})
_APPEND_REDIRECTION_OPERATORS = frozenset({">>", "&>>"})
_STREAM_SCOPE_KINDS = frozenset(
    {"command", "command_substitution", "process_substitution", "subshell_group", "pipeline"}
)
_PROCESS_RESOURCE_DIRECTIONS = frozenset({"input", "output"})


class _ShellSourceKind(str, Enum):  # noqa: UP042
    """The source an interpreted shell command will execute."""

    NONE = "none"
    COMMAND = "command"
    SCRIPT = "script"
    STDIN = "stdin"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class _ShellSourceSelection:
    """A selected shell source port or conservative ambiguity set."""

    kind: _ShellSourceKind
    argv_index: int | None = None
    candidate_indices: tuple[int, ...] = ()
    include_stdin: bool = False


def _normalized_shell_head(name: str | None) -> str | None:
    """Return a case-insensitive shell head without an executable suffix."""
    if name is None:
        return None
    return name.casefold().removesuffix(".exe")


def _select_shell_source(argv: tuple[_ArgPort, ...], head_index: int) -> _ShellSourceSelection:  # noqa: PLR0911, PLR0912
    """Select the shell source according to its literal option grammar."""
    index = head_index + 1
    stdin_selected = False
    while index < len(argv):
        port = argv[index]
        if port.dynamic:
            return _ShellSourceSelection(
                _ShellSourceKind.AMBIGUOUS,
                candidate_indices=tuple(range(index, len(argv))),
                include_stdin=True,
            )
        literal = port.literal
        if literal in _SHELL_EAGER_STOPS:
            return _ShellSourceSelection(_ShellSourceKind.NONE)
        if literal in ("-", "--"):
            index += 1
            if stdin_selected or index >= len(argv):
                return _ShellSourceSelection(_ShellSourceKind.STDIN)
            return _ShellSourceSelection(_ShellSourceKind.SCRIPT, argv_index=index)
        if literal.startswith("--"):
            if literal in _SHELL_LONG_OPTIONS_WITH_VALUE:
                value_index = index + 1
                if value_index >= len(argv) or argv[value_index].dynamic:
                    return _ShellSourceSelection(
                        _ShellSourceKind.AMBIGUOUS,
                        candidate_indices=tuple(range(index, len(argv))),
                        include_stdin=True,
                    )
                index += 2
            else:
                index += 1
            continue
        if literal and literal[0] in "-+":
            short_options = literal[1:]
            for option_index, option in enumerate(short_options):
                if option == "c":
                    if index + 1 >= len(argv):
                        return _ShellSourceSelection(_ShellSourceKind.NONE)
                    return _ShellSourceSelection(_ShellSourceKind.COMMAND, argv_index=index + 1)
                if option == "s":
                    stdin_selected = True
                if option in {"o", "O"} and option_index == len(short_options) - 1:
                    value_index = index + 1
                    if value_index >= len(argv) or argv[value_index].dynamic:
                        return _ShellSourceSelection(
                            _ShellSourceKind.AMBIGUOUS,
                            candidate_indices=tuple(range(index, len(argv))),
                            include_stdin=True,
                        )
                    index += 2
                    break
            else:
                index += 1
            continue
        if stdin_selected:
            return _ShellSourceSelection(_ShellSourceKind.STDIN)
        return _ShellSourceSelection(_ShellSourceKind.SCRIPT, argv_index=index)
    return _ShellSourceSelection(_ShellSourceKind.STDIN)


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
    pending = [expression]
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if isinstance(current, Choice | Concat):
            pending.extend(current.parts)
    return nodes


def _expression_edges(expression: ContentExpr) -> int:
    pending = [expression]
    edges = 0
    while pending:
        current = pending.pop()
        if isinstance(current, VariableRef | ResourceRef | StreamRef):
            edges += 1
        elif isinstance(current, Choice | Concat):
            pending.extend(current.parts)
    return edges


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
    pending: list[tuple[ContentExpr, bool]] = [(expression, False)]
    values: list[_ContentValue] = []
    while pending:
        current, expanded = pending.pop()
        if isinstance(current, LiteralTransfer):
            values.append(frozenset({_TransferSummary.literal(current.text)}))
        elif isinstance(current, OutsideGap):
            values.append(_OUTSIDE_VALUE)
        elif isinstance(current, VariableRef | ResourceRef | StreamRef):
            raise ValueError("closed content expression contains an unresolved reference")
        elif expanded:
            parts = values[-len(current.parts) :] if current.parts else []
            if current.parts:
                del values[-len(current.parts) :]
            if isinstance(current, Choice):
                values.append(_join_values(*parts))
            else:
                value = frozenset({_EPSILON})
                for part in parts:
                    value = _compose_values(value, part)
                values.append(value)
        else:
            pending.append((current, True))
            pending.extend((part, False) for part in reversed(current.parts))
    return values[0]


def _evaluate_with_tables(
    expression: ContentExpr,
    variables: dict[str | int, _ContentValue],
    resources: dict[str | int, _ContentValue],
    streams: dict[str | int, _ContentValue],
    limits: TaintLimits,
) -> _ContentValue:
    pending: list[tuple[ContentExpr, bool]] = [(expression, False)]
    values: list[_ContentValue] = []
    while pending:
        current, expanded = pending.pop()
        if isinstance(current, LiteralTransfer):
            values.append(frozenset({_TransferSummary.literal(current.text)}))
        elif isinstance(current, OutsideGap):
            values.append(_OUTSIDE_VALUE)
        elif isinstance(current, VariableRef):
            values.append(variables.get(current.name, _OUTSIDE_VALUE))
        elif isinstance(current, ResourceRef):
            values.append(resources.get(current.key, _OUTSIDE_VALUE))
        elif isinstance(current, StreamRef):
            values.append(streams.get(current.scope_id, _OUTSIDE_VALUE))
        elif expanded:
            parts = values[-len(current.parts) :] if current.parts else []
            if current.parts:
                del values[-len(current.parts) :]
            if isinstance(current, Choice):
                values.append(_cap_value(_join_values(*parts), limits))
            else:
                value = frozenset({_EPSILON})
                for part in parts:
                    value = _cap_value(_compose_values(value, part), limits)
                values.append(value)
        else:
            pending.append((current, True))
            pending.extend((part, False) for part in reversed(current.parts))
    return _cap_value(values[0], limits)


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

    variables: dict[str | int, _ContentValue] = {
        write.key: frozenset() for write in definitions.variable_writes
    }
    resources: dict[str | int, _ContentValue] = {
        write.key: frozenset() for write in definitions.resource_writes
    }
    streams: dict[str | int, _ContentValue] = {
        write.key: frozenset() for write in definitions.stream_writes
    }
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


def _pipe_inputs(evidence: _ShellTaintEvidence) -> dict[int, ContentExpr]:
    """Return the initial stdin expression for each pipe consumer."""
    return {pipe.consumer_command_id: StreamRef(pipe.producer_scope_id) for pipe in evidence.pipes}


def _process_resource_input(
    resource_id: int | None,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> ContentExpr:
    """Return the input stream for a valid process-substitution resource."""
    if resource_id is None:
        return OutsideGap()
    resource = process_resources.get(resource_id)
    if resource is None or resource.direction != "input":
        return OutsideGap()
    return StreamRef(resource.scope_id)


def _input_expression(
    command: _CommandEvidence,
    pipe_inputs: dict[int, ContentExpr],
    process_resources: dict[int, _ProcessResourceEvidence],
) -> ContentExpr:
    """Replay ordered descriptor-zero input redirections for one command."""
    expression = pipe_inputs.get(command.command_id, OutsideGap())
    for event in sorted(command.redirections, key=lambda candidate: candidate.ordinal):
        if event.descriptor != 0 or event.operator not in _INPUT_REDIRECTION_OPERATORS:
            continue
        if isinstance(event.target, StaticResourceTarget):
            expression = ResourceRef(event.target.key)
        elif isinstance(event.target, ContentTarget):
            expression = event.target.content
        elif isinstance(event.target, ProcessResourceTarget):
            expression = _process_resource_input(event.target.resource_id, process_resources)
        elif isinstance(event.target, NullTarget):
            expression = LiteralTransfer("")
        else:
            expression = OutsideGap()
    return expression


def _producer_stdout(command: _CommandEvidence, stdin: ContentExpr) -> ContentExpr:
    """Return a conservative stdout expression for one command."""
    head_index = command.executable.argv_index
    payload_start = head_index + 1 if head_index is not None else min(1, len(command.argv))
    argv_content = concat(*(port.content for port in command.argv[payload_start:]))
    return choice(OutsideGap(), argv_content, stdin)


def _static_write_definitions(
    events: tuple[_RedirectionEvent, ...], output: ContentExpr
) -> tuple[_FlowWrite, ...]:
    """Replay output descriptors and return static resource writes they receive."""
    writes: list[_FlowWrite] = []
    final_output: dict[int, _RedirectionEvent] = {}
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _OUTPUT_REDIRECTION_OPERATORS:
            continue
        if isinstance(event.target, StaticResourceTarget):
            if event.operator not in _APPEND_REDIRECTION_OPERATORS:
                writes.append(_FlowWrite(event.target.key, LiteralTransfer("")))
            final_output[event.descriptor] = event
        elif isinstance(event.target, NullTarget | DynamicResourceTarget | DescriptorTarget):
            final_output[event.descriptor] = event
        else:
            final_output[event.descriptor] = event
    for descriptor, event in final_output.items():
        if not isinstance(event.target, StaticResourceTarget):
            continue
        writes.append(
            _FlowWrite(
                event.target.key,
                output if descriptor == 1 else OutsideGap(),
                append=event.operator in _APPEND_REDIRECTION_OPERATORS,
            )
        )
    return tuple(writes)


@dataclass(slots=True)
class _OutputLowering:
    """Lower structured output records into recursive stream definitions."""

    command_scopes: dict[int, int]
    next_synthetic_scope: int = -1

    def lower(self, output: OutputExpr, stream_writes: list[_FlowWrite]) -> ContentExpr:
        """Lower one output expression, adding repeat definitions when needed."""
        pending: list[tuple[OutputExpr, bool]] = [(output, False)]
        values: list[ContentExpr] = []
        while pending:
            current, expanded = pending.pop()
            if isinstance(current, CommandOutput):
                scope_id = self.command_scopes.get(current.command_id)
                values.append(StreamRef(scope_id) if scope_id is not None else OutsideGap())
            elif expanded:
                if isinstance(current, SequenceOutput | ChoiceOutput):
                    parts = values[-len(current.parts) :] if current.parts else []
                    if current.parts:
                        del values[-len(current.parts) :]
                    expression = (
                        concat(*parts) if isinstance(current, SequenceOutput) else choice(*parts)
                    )
                    values.append(expression)
                else:
                    repeated = values.pop()
                    scope_id = self.next_synthetic_scope
                    self.next_synthetic_scope -= 1
                    stream_writes.extend(
                        (
                            _FlowWrite(scope_id, LiteralTransfer("")),
                            _FlowWrite(scope_id, Concat((repeated, StreamRef(scope_id)))),
                        )
                    )
                    values.append(StreamRef(scope_id))
            else:
                pending.append((current, True))
                if isinstance(current, SequenceOutput | ChoiceOutput):
                    pending.extend((part, False) for part in reversed(current.parts))
                else:
                    pending.append((current.part, False))
        return values[0]


def _build_flow_definitions(
    evidence: _ShellTaintEvidence,
) -> tuple[_FlowDefinitions, dict[int, ContentExpr]]:
    """Lower typed shell evidence into fixed-point definitions and command stdin."""
    command_scopes = {command.command_id: command.output_scope_id for command in evidence.commands}
    process_resources = {resource.resource_id: resource for resource in evidence.process_resources}
    pipe_inputs = _pipe_inputs(evidence)
    inputs = {
        command.command_id: _input_expression(command, pipe_inputs, process_resources)
        for command in evidence.commands
    }
    variable_writes: list[_FlowWrite] = []
    resource_writes: list[_FlowWrite] = []
    stream_writes: list[_FlowWrite] = []

    for command in evidence.commands:
        variable_writes.extend(
            _FlowWrite(assignment.name, assignment.content, append=assignment.append)
            for assignment in command.assignments
        )
        output = _producer_stdout(command, inputs[command.command_id])
        stream_writes.append(_FlowWrite(command.output_scope_id, output))
        resource_writes.extend(_static_write_definitions(command.redirections, output))

    lowering = _OutputLowering(command_scopes)
    for scope in evidence.scopes:
        variable_writes.extend(
            _FlowWrite(binding.name, binding.content, append=binding.append)
            for binding in scope.loop_bindings
        )
        stream_writes.append(
            _FlowWrite(
                scope.scope_id,
                lowering.lower(scope.output, stream_writes),
                strip_trailing_newlines=scope.kind == "command_substitution",
            )
        )
        resource_writes.extend(
            _static_write_definitions(scope.redirections, StreamRef(scope.scope_id))
        )

    return (
        _FlowDefinitions(
            variable_writes=tuple(variable_writes),
            resource_writes=tuple(resource_writes),
            stream_writes=tuple(stream_writes),
        ),
        inputs,
    )


def _eval_arguments(command: _CommandEvidence) -> ContentExpr:
    """Return eval arguments joined by the literal shell argument separator."""
    head_index = command.executable.argv_index
    if head_index is None:
        return LiteralTransfer("")
    arguments = command.argv[head_index + 1 :]
    parts: list[ContentExpr] = []
    for index, argument in enumerate(arguments):
        if index:
            parts.append(LiteralTransfer(" "))
        parts.append(argument.content)
    return concat(*parts)


def _script_port_expression(port: _ArgPort) -> ContentExpr:
    """Return a static script resource reference or an external gap."""
    if port.process_resource_id is not None:
        return OutsideGap()
    key = normalize_static_resource(port.literal, dynamic=port.dynamic)
    return ResourceRef(key) if key is not None else OutsideGap()


def _sink_expressions(  # noqa: PLR0911, PLR0912
    command: _CommandEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    """Return every conservative execution sink expression for one command."""
    executable = command.executable
    if executable.argv_index is None or executable.name is None:
        return ()
    name = executable.name
    literal = executable.literal
    if name == "eval" and literal == "eval" and not executable.external_lookup:
        return (_eval_arguments(command),)
    if name in {"source", "."} and literal == name and not executable.external_lookup:
        operand_index = executable.argv_index + 1
        if operand_index >= len(command.argv):
            return ()
        operand = command.argv[operand_index]
        if operand.process_resource_id is not None:
            return (_process_resource_input(operand.process_resource_id, process_resources),)
        return (_script_port_expression(operand),)
    if _normalized_shell_head(executable.name) in _SHELL_HEADS:
        selection = _select_shell_source(command.argv, executable.argv_index)
        if selection.kind is _ShellSourceKind.NONE:
            return ()
        if selection.kind is _ShellSourceKind.STDIN:
            return (stdin,)
        if selection.kind is _ShellSourceKind.COMMAND:
            if selection.argv_index is None:
                return ()
            return (command.argv[selection.argv_index].content,)
        if selection.kind is _ShellSourceKind.SCRIPT:
            if selection.argv_index is None:
                return ()
            port = command.argv[selection.argv_index]
            if port.process_resource_id is not None:
                return (_process_resource_input(port.process_resource_id, process_resources),)
            return (_script_port_expression(port),)
        candidates = [command.argv[index].content for index in selection.candidate_indices]
        if selection.include_stdin:
            candidates.append(stdin)
        return (choice(*candidates),)
    if literal is not None and (literal.startswith("/") or literal.startswith("./")):
        key = normalize_static_resource(literal, dynamic=False)
        if key is not None:
            return (ResourceRef(key),)
    return ()


def analyze_marker_taint(  # noqa: PLR0911
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits = TaintLimits(),  # noqa: B008
) -> tuple[bool, str | None]:
    """Return a fail-closed verdict for authored marker flow in one run body."""
    if any(scope.kind not in _STREAM_SCOPE_KINDS for scope in evidence.scopes):
        return True, "shell taint stream scope cannot be structured"
    if any(
        resource.direction not in _PROCESS_RESOURCE_DIRECTIONS
        for resource in evidence.process_resources
    ):
        return True, "shell taint process resource cannot be structured"
    evidence_edges = (
        len(evidence.pipes)
        + len(evidence.process_resources)
        + sum(len(command.redirections) for command in evidence.commands)
        + sum(len(scope.redirections) for scope in evidence.scopes)
    )
    if evidence_edges > limits.max_edges:
        return True, "shell taint edge limit exceeded"
    evidence_entries = (
        len(evidence.commands) + len(evidence.scopes) + len(evidence.process_resources)
    )
    if evidence_entries > limits.max_table_entries:
        return True, "shell taint table entry limit exceeded"

    try:
        definitions, inputs = _build_flow_definitions(evidence)
        solved = _solve_flow_definitions(definitions, limits=limits)
        process_resources = {
            resource.resource_id: resource for resource in evidence.process_resources
        }
        for command in evidence.commands:
            stdin = inputs[command.command_id]
            for expression in _sink_expressions(command, stdin, process_resources):
                if _marker_capable(solved.evaluate(expression)):
                    return True, TAINT_REFUSAL_REASON
    except _TaintLimitExceeded as error:
        return True, str(error)
    return False, None
