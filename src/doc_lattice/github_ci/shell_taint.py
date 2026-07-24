"""Pure authored-marker taint analysis for one CI shell run body."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

from doc_lattice.error_types import ProjectError

if TYPE_CHECKING:
    from collections.abc import Iterator

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
class _SecondPassVariableRef:
    """A parameter expanded by eval's second shell parse, not by its input-producing shell."""

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
    LiteralTransfer
    | VariableRef
    | _SecondPassVariableRef
    | StreamRef
    | ResourceRef
    | Choice
    | Concat
    | OutsideGap
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


class _MalformedTaintEvidence(ProjectError):
    """Structured shell evidence cannot be analyzed safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SHELL_TAINT_EVIDENCE_INVALID")


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
    alternates: tuple[_ExecutableEvidence, ...] = ()


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


@dataclass(frozen=True, slots=True)
class _ImplicitPipeTarget:
    """Internal descriptor binding for pipeline-provided standard output."""


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
class ScopeOutput:
    """Output from one nested stream scope."""

    scope_id: int


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


OutputExpr: TypeAlias = (  # noqa: UP040
    CommandOutput | ScopeOutput | SequenceOutput | ChoiceOutput | RepeatOutput
)


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
    entry: OutputExpr | None = None


@dataclass(frozen=True, slots=True)
class _PipeEvidence:
    """A producer scope piped to a command or compound-scope consumer."""

    producer_scope_id: int
    consumer_command_id: int | None = None
    consumer_scope_id: int | None = None


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


@dataclass(frozen=True, slots=True)
class _BuiltContent:
    """Completed authored content plus word-local taint annotations."""

    expression: ContentExpr
    assignment_name: str | None = None
    assignment_content: ContentExpr | None = None
    assignment_append: bool = False
    conditional_assignments: tuple[_AssignmentEvidence, ...] = ()
    process_resource_id: int | None = None


@dataclass(slots=True)
class ContentBuilder:
    """Incrementally construct one word's authored content evidence."""

    parts: list[ContentExpr] = field(default_factory=list)
    assignment_value_start: int | None = None
    assignment_name: str | None = None
    assignment_append: bool = False
    conditional_assignments: list[_AssignmentEvidence] = field(default_factory=list)
    process_resource_id: int | None = None

    @classmethod
    def empty(cls) -> ContentBuilder:
        return cls()

    def append_literal(self, text: str) -> None:
        if text:
            self.parts.append(LiteralTransfer(text))

    def append_expression(self, expression: ContentExpr) -> None:
        self.parts.append(expression)

    def mark_assignment(self, name: str, *, append: bool) -> None:
        self.assignment_name = name
        self.assignment_append = append
        self.assignment_value_start = len(self.parts)

    def add_conditional_assignment(self, assignment: _AssignmentEvidence) -> None:
        self.conditional_assignments.append(assignment)

    def build(self) -> _BuiltContent:
        expression = concat(*self.parts)
        assignment_content = (
            concat(*self.parts[self.assignment_value_start :])
            if self.assignment_value_start is not None
            else None
        )
        return _BuiltContent(
            expression,
            assignment_name=self.assignment_name,
            assignment_content=assignment_content,
            assignment_append=self.assignment_append,
            conditional_assignments=tuple(self.conditional_assignments),
            process_resource_id=self.process_resource_id,
        )


@dataclass(slots=True)
class _EvidenceBuilder:
    """Mutable collector for one scanner-owned taint pass."""

    commands: list[_CommandEvidence] = field(default_factory=list)
    scopes: list[_StreamScopeEvidence] = field(default_factory=list)
    pipes: list[_PipeEvidence] = field(default_factory=list)
    process_resources: list[_ProcessResourceEvidence] = field(default_factory=list)
    next_command_id: int = 1
    next_scope_id: int = 1
    next_process_resource_id: int = 1

    @classmethod
    def empty(cls) -> _EvidenceBuilder:
        return cls()

    def allocate_command(self) -> tuple[int, int]:
        command_id = self.next_command_id
        self.next_command_id += 1
        return command_id, self.allocate_scope()

    def allocate_scope(self) -> int:
        scope_id = self.next_scope_id
        self.next_scope_id += 1
        return scope_id

    def allocate_process_resource(self) -> int:
        resource_id = self.next_process_resource_id
        self.next_process_resource_id += 1
        return resource_id

    def attach_redirection_content(
        self,
        command_id: int,
        ordinal: int,
        content: ContentExpr,
    ) -> None:
        """Replace one command redirection's placeholder with authored content."""
        for index, command in enumerate(self.commands):
            if command.command_id != command_id:
                continue
            redirections = tuple(
                replace(event, target=ContentTarget(content)) if event.ordinal == ordinal else event
                for event in command.redirections
            )
            self.commands[index] = replace(command, redirections=redirections)
            return
        raise ValueError("heredoc owner command is missing")

    def attach_scope_redirection_content(
        self,
        scope_id: int,
        ordinal: int,
        content: ContentExpr,
    ) -> None:
        """Replace one compound-scope heredoc placeholder with authored content."""
        for index, scope in enumerate(self.scopes):
            if scope.scope_id != scope_id:
                continue
            redirections = tuple(
                replace(event, target=ContentTarget(content)) if event.ordinal == ordinal else event
                for event in scope.redirections
            )
            self.scopes[index] = replace(scope, redirections=redirections)
            return
        raise ValueError("heredoc owner scope is missing")

    def command_output_scope(self, command_id: int) -> int:
        """Return the stdout scope owned by one command."""
        for command in self.commands:
            if command.command_id == command_id:
                return command.output_scope_id
        raise ValueError("pipe producer command is missing")

    def attach_scope_parent(self, scope_id: int, command_id: int) -> None:
        """Record the command whose word expansion references one scope."""
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(scope, parent_command_id=command_id)
                return

    def attach_scope_redirection(self, scope_id: int, event: _RedirectionEvent) -> None:
        """Append a redirection owned by a compound command scope."""
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(scope, redirections=(*scope.redirections, event))
                return
        raise ValueError("compound redirection owner scope is missing")

    def replace_scope_output(self, scope_id: int, output: OutputExpr) -> None:
        """Replace one frozen stream scope's structured stdout."""
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(scope, output=output)
                return
        raise ValueError("compound output scope is missing")

    def freeze(self) -> _ShellTaintEvidence:
        return _ShellTaintEvidence(
            commands=tuple(self.commands),
            scopes=tuple(self.scopes),
            pipes=tuple(self.pipes),
            process_resources=tuple(self.process_resources),
        )


def normalize_static_resource(literal: str, *, dynamic: bool) -> str | None:
    """Return a lexical static resource key without resolving filesystem state."""
    if dynamic or not literal:
        return None
    normalized = literal.replace("\\", "/")
    absolute = normalized.startswith("/")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append(part)
            continue
        parts.append(part)
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
_COMBINED_OUTPUT_REDIRECTION_OPERATORS = frozenset({"&>", "&>>"})
_STREAM_SCOPE_KINDS = frozenset(
    {
        "brace_group",
        "command",
        "command_substitution",
        "process_substitution",
        "subshell_group",
        "pipeline",
    }
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
    pending = list(reversed(parts))
    while pending:
        part = pending.pop()
        if isinstance(part, Concat):
            pending.extend(reversed(part.parts))
        elif not isinstance(part, LiteralTransfer) or part.text:
            flattened.append(part)

    if not flattened:
        return LiteralTransfer("")
    if len(flattened) == 1:
        return flattened[0]
    return Concat(tuple(flattened))


def choice(*parts: ContentExpr) -> ContentExpr:
    """Flatten choices while retaining epsilon as a real alternative."""
    flattened: list[ContentExpr] = []
    pending = list(reversed(parts))
    while pending:
        part = pending.pop()
        if isinstance(part, Choice):
            pending.extend(reversed(part.parts))
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
        if isinstance(current, VariableRef | _SecondPassVariableRef | ResourceRef | StreamRef):
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
        elif isinstance(current, VariableRef | _SecondPassVariableRef | ResourceRef | StreamRef):
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
        elif isinstance(current, VariableRef | _SecondPassVariableRef):
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


@dataclass(slots=True)
class _OutputValidationState:
    """Memoized bounded state for validating structured output expressions."""

    remaining_nodes: int
    memo: dict[int, frozenset[int]] = field(default_factory=dict)


def _output_children(output: OutputExpr) -> tuple[OutputExpr, ...]:
    """Return one output expression's nested structural children."""
    if isinstance(output, SequenceOutput | ChoiceOutput):
        return output.parts
    if isinstance(output, RepeatOutput):
        return (output.part,)
    if isinstance(output, CommandOutput | ScopeOutput):
        return ()
    raise _MalformedTaintEvidence("shell taint evidence cannot be structured")


def _validated_output_scope_refs(
    output: OutputExpr,
    command_ids: set[int],
    stream_ids: set[int],
    scope_ids: set[int],
    state: _OutputValidationState,
) -> frozenset[int]:
    """Validate one output tree iteratively and return referenced structured scopes."""
    pending = [(output, False)]
    active: set[int] = set()
    while pending:
        current, expanded = pending.pop()
        current_id = id(current)
        if current_id in state.memo:
            continue
        if expanded:
            active.remove(current_id)
            if isinstance(current, CommandOutput):
                if current.command_id not in command_ids:
                    raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
                refs: set[int] = set()
            elif isinstance(current, ScopeOutput):
                if current.scope_id not in stream_ids:
                    raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
                refs = {current.scope_id} if current.scope_id in scope_ids else set()
            else:
                refs = set()
                for child in _output_children(current):
                    refs.update(state.memo[id(child)])
            state.memo[current_id] = frozenset(refs)
            continue
        if current_id in active:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
        if state.remaining_nodes < 1:
            raise _TaintLimitExceeded("shell taint expression node limit exceeded")
        state.remaining_nodes -= 1
        active.add(current_id)
        pending.append((current, True))
        pending.extend((child, False) for child in reversed(_output_children(current)))
    return state.memo[id(output)]


def _validate_acyclic_graph(
    graph: dict[int, frozenset[int]],
    *,
    reason: str,
) -> None:
    """Reject directed cycles without recursive graph traversal."""
    active: set[int] = set()
    visited: set[int] = set()
    for root in graph:
        if root in visited:
            continue
        pending = [(root, False)]
        while pending:
            node, expanded = pending.pop()
            if expanded:
                active.remove(node)
                visited.add(node)
                continue
            if node in visited:
                continue
            if node in active:
                raise _MalformedTaintEvidence(reason)
            active.add(node)
            pending.append((node, True))
            pending.extend((child, False) for child in reversed(tuple(graph[node])))


def _validate_nested_evidence(
    evidence: _ShellTaintEvidence,
    limits: TaintLimits,
) -> None:
    """Validate nested evidence identifiers, references, and output graphs."""
    command_id_values = tuple(command.command_id for command in evidence.commands)
    scope_id_values = tuple(scope.scope_id for scope in evidence.scopes)
    resource_id_values = tuple(resource.resource_id for resource in evidence.process_resources)
    stream_id_values = (
        *(command.output_scope_id for command in evidence.commands),
        *scope_id_values,
    )
    if any(
        len(values) != len(set(values))
        for values in (
            command_id_values,
            scope_id_values,
            resource_id_values,
            stream_id_values,
        )
    ):
        raise _MalformedTaintEvidence("shell taint evidence cannot be structured")

    command_ids = set(command_id_values)
    scope_ids = set(scope_id_values)
    resource_ids = set(resource_id_values)
    stream_ids = set(stream_id_values)
    parent_graph: dict[int, frozenset[int]] = {}
    output_graph: dict[int, frozenset[int]] = {}
    output_state = _OutputValidationState(limits.max_expression_nodes)
    for scope in evidence.scopes:
        if scope.parent_scope_id is not None and scope.parent_scope_id not in scope_ids:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
        if scope.parent_command_id is not None and scope.parent_command_id not in command_ids:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
        parent_graph[scope.scope_id] = (
            frozenset({scope.parent_scope_id}) if scope.parent_scope_id is not None else frozenset()
        )
        refs = set(
            _validated_output_scope_refs(
                scope.output,
                command_ids,
                stream_ids,
                scope_ids,
                output_state,
            )
        )
        if scope.entry is not None:
            refs.update(
                _validated_output_scope_refs(
                    scope.entry,
                    command_ids,
                    stream_ids,
                    scope_ids,
                    output_state,
                )
            )
        output_graph[scope.scope_id] = frozenset(refs)

    _validate_acyclic_graph(
        parent_graph,
        reason="shell taint stream scope cannot be structured",
    )
    _validate_acyclic_graph(
        output_graph,
        reason="shell taint evidence cannot be structured",
    )

    for pipe in evidence.pipes:
        if pipe.producer_scope_id not in stream_ids:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
        if pipe.consumer_command_id is not None and pipe.consumer_command_id not in command_ids:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
        if pipe.consumer_scope_id is not None and pipe.consumer_scope_id not in scope_ids:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
    if any(resource.scope_id not in scope_ids for resource in evidence.process_resources):
        raise _MalformedTaintEvidence("shell taint evidence cannot be structured")

    redirection_groups = (
        *(command.redirections for command in evidence.commands),
        *(scope.redirections for scope in evidence.scopes),
    )
    if any(
        isinstance(event.target, ProcessResourceTarget)
        and event.target.resource_id not in resource_ids
        for events in redirection_groups
        for event in events
    ):
        raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
    if any(
        port.process_resource_id is not None and port.process_resource_id not in resource_ids
        for command in evidence.commands
        for port in command.argv
    ):
        raise _MalformedTaintEvidence("shell taint evidence cannot be structured")


def _output_input_parts(
    output: OutputExpr,
    scopes: dict[int, _StreamScopeEvidence],
    command_scopes: dict[int, int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return direct commands and nested scopes that may consume inherited stdin."""
    pending = [output]
    seen_nodes: set[int] = set()
    commands: list[int] = []
    command_set: set[int] = set()
    dependencies: list[int] = []
    dependency_set: set[int] = set()
    while pending:
        current = pending.pop()
        current_id = id(current)
        if current_id in seen_nodes:
            continue
        seen_nodes.add(current_id)
        if isinstance(current, CommandOutput):
            if current.command_id not in command_set:
                command_set.add(current.command_id)
                commands.append(current.command_id)
        elif isinstance(current, ScopeOutput):
            command_id = command_scopes.get(current.scope_id)
            if command_id is not None and command_id not in command_set:
                command_set.add(command_id)
                commands.append(command_id)
            elif current.scope_id in scopes and current.scope_id not in dependency_set:
                dependency_set.add(current.scope_id)
                dependencies.append(current.scope_id)
        elif isinstance(current, SequenceOutput | ChoiceOutput):
            pending.extend(reversed(current.parts))
        elif isinstance(current, RepeatOutput):
            pending.append(current.part)
        else:
            raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
    return tuple(commands), tuple(dependencies)


def _scope_input_commands(
    scopes: dict[int, _StreamScopeEvidence],
    command_scopes: dict[int, int],
) -> dict[int, tuple[int, ...]]:
    """Return memoized possible stdin consumers for every structured scope."""
    parts = {
        scope_id: _output_input_parts(
            (scope.entry if scope.kind == "pipeline" and scope.entry is not None else scope.output),
            scopes,
            command_scopes,
        )
        for scope_id, scope in scopes.items()
    }
    memo: dict[int, tuple[int, ...]] = {}
    state: dict[int, int] = {}
    for root in scopes:
        if root in memo:
            continue
        pending = [(root, False)]
        while pending:
            scope_id, expanded = pending.pop()
            if scope_id in memo:
                continue
            if expanded:
                direct, dependencies = parts[scope_id]
                commands = list(direct)
                seen = set(commands)
                for dependency in dependencies:
                    for command_id in memo[dependency]:
                        if command_id not in seen:
                            seen.add(command_id)
                            commands.append(command_id)
                memo[scope_id] = tuple(commands)
                state[scope_id] = 2
                continue
            if state.get(scope_id) == 1:
                raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
            state[scope_id] = 1
            pending.append((scope_id, True))
            for dependency in reversed(parts[scope_id][1]):
                if dependency not in memo:
                    pending.append((dependency, False))
    return memo


def _output_bindings(
    events: tuple[_RedirectionEvent, ...],
    *,
    implicit_pipe: bool = False,
) -> dict[int, tuple[RedirectionTarget | _ImplicitPipeTarget, bool]]:
    """Replay output descriptor mutations left-to-right."""
    bindings: dict[int, tuple[RedirectionTarget | _ImplicitPipeTarget, bool]] = (
        {1: (_ImplicitPipeTarget(), False)} if implicit_pipe else {}
    )
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _OUTPUT_REDIRECTION_OPERATORS:
            continue
        if isinstance(event.target, DescriptorTarget):
            binding = bindings.get(event.target.descriptor, (DynamicResourceTarget(), False))
        else:
            binding = (event.target, event.operator in _APPEND_REDIRECTION_OPERATORS)
        if event.operator in _COMBINED_OUTPUT_REDIRECTION_OPERATORS:
            bindings[1] = binding
            bindings[2] = bindings[1]
        else:
            bindings[event.descriptor] = binding
    return bindings


def _replay_input_bindings(
    events: tuple[_RedirectionEvent, ...],
    initial: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> ContentExpr:
    """Replay ordered stdin bindings over one inherited descriptor-zero expression."""
    bindings: dict[int, ContentExpr] = {0: initial}
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _INPUT_REDIRECTION_OPERATORS:
            continue
        if isinstance(event.target, StaticResourceTarget):
            expression = ResourceRef(event.target.key)
        elif isinstance(event.target, ContentTarget):
            expression = event.target.content
        elif isinstance(event.target, ProcessResourceTarget):
            expression = _process_resource_input(event.target.resource_id, process_resources)
        elif isinstance(event.target, NullTarget):
            expression = LiteralTransfer("")
        elif isinstance(event.target, DescriptorTarget):
            expression = bindings.get(event.target.descriptor, OutsideGap())
        else:
            expression = OutsideGap()
        bindings[event.descriptor] = expression
    return bindings[0]


def _pipe_source(
    scope_id: int,
    commands: dict[int, _CommandEvidence],
    scopes: dict[int, _StreamScopeEvidence],
) -> ContentExpr:
    """Return what a pipe receives after the producer replays descriptor-one redirects."""
    command = next(
        (candidate for candidate in commands.values() if candidate.output_scope_id == scope_id),
        None,
    )
    scope = scopes.get(scope_id)
    events = command.redirections if command is not None else scope.redirections if scope else ()
    stdout = _output_bindings(events, implicit_pipe=True)[1][0]
    return StreamRef(scope_id) if isinstance(stdout, _ImplicitPipeTarget) else LiteralTransfer("")


def _output_process_scope_inputs(
    evidence: _ShellTaintEvidence,
    scopes: dict[int, _StreamScopeEvidence],
    resources: dict[int, _ProcessResourceEvidence],
) -> dict[int, ContentExpr]:
    """Return direct scope inputs created by output process substitutions."""
    inputs: dict[int, ContentExpr] = {}
    for writer in evidence.commands:
        binding = _output_bindings(writer.redirections).get(1)
        if binding is None or not isinstance(binding[0], ProcessResourceTarget):
            continue
        resource = resources.get(binding[0].resource_id)
        if resource is None or resource.direction != "output":
            continue
        if resource.scope_id in scopes:
            inputs[resource.scope_id] = StreamRef(writer.output_scope_id)
    return inputs


def _resolved_scope_inputs(
    scopes: dict[int, _StreamScopeEvidence],
    direct_inputs: dict[int, ContentExpr],
    resources: dict[int, _ProcessResourceEvidence],
    active_scopes: set[int],
) -> dict[int, ContentExpr]:
    """Resolve inherited scope stdin iteratively, replaying each scope's redirects."""
    memo: dict[int, ContentExpr] = {}
    for scope_id in active_scopes:
        if scope_id in memo:
            continue
        path: list[int] = []
        active: set[int] = set()
        current = scope_id
        while current not in memo:
            if current in active:
                raise _MalformedTaintEvidence("shell taint evidence cannot be structured")
            active.add(current)
            scope = scopes.get(current)
            if scope is None:
                inherited: ContentExpr = OutsideGap()
                break
            path.append(current)
            if current in direct_inputs:
                inherited = direct_inputs[current]
                break
            if scope.parent_scope_id is None:
                inherited = OutsideGap()
                break
            current = scope.parent_scope_id
        else:
            inherited = memo[current]
        for current in reversed(path):
            scope = scopes[current]
            inherited = _replay_input_bindings(
                scope.redirections,
                direct_inputs.get(current, inherited),
                resources,
            )
            memo[current] = inherited
    return memo


def _scope_depths(scopes: dict[int, _StreamScopeEvidence]) -> dict[int, int]:
    """Return each validated structured scope's parent depth."""
    depths: dict[int, int] = {}
    for scope_id in scopes:
        if scope_id in depths:
            continue
        path: list[int] = []
        current = scope_id
        while current not in depths:
            path.append(current)
            parent = scopes[current].parent_scope_id
            if parent is None:
                depth = -1
                break
            current = parent
        else:
            depth = depths[current]
        for current in reversed(path):
            depth += 1
            depths[current] = depth
    return depths


def _apply_scope_inputs(
    command_inputs: dict[int, ContentExpr],
    active_scopes: set[int],
    scope_inputs: dict[int, ContentExpr],
    scope_commands: dict[int, tuple[int, ...]],
    depths: dict[int, int],
) -> None:
    """Apply the closest possible scope stdin to commands without a direct pipe."""
    candidates: dict[int, tuple[int, ContentExpr]] = {}
    for scope_id in active_scopes:
        expression = scope_inputs[scope_id]
        for command_id in scope_commands[scope_id]:
            prior = candidates.get(command_id)
            if prior is None or depths[scope_id] > prior[0]:
                candidates[command_id] = (depths[scope_id], expression)
    for command_id, (_depth, expression) in candidates.items():
        command_inputs.setdefault(command_id, expression)


def _pipe_inputs(evidence: _ShellTaintEvidence) -> dict[int, ContentExpr]:
    """Return stdin expressions from pipes and output process resources through scope entries."""
    commands = {command.command_id: command for command in evidence.commands}
    scopes = {scope.scope_id: scope for scope in evidence.scopes}
    command_scopes = {command.output_scope_id: command.command_id for command in evidence.commands}
    command_inputs: dict[int, ContentExpr] = {}
    scope_inputs: dict[int, ContentExpr] = {}
    for pipe in evidence.pipes:
        expression = _pipe_source(pipe.producer_scope_id, commands, scopes)
        if pipe.consumer_command_id is not None:
            command_inputs[pipe.consumer_command_id] = expression
        if pipe.consumer_scope_id is not None:
            scope_inputs[pipe.consumer_scope_id] = expression
    resources = {resource.resource_id: resource for resource in evidence.process_resources}
    scope_inputs.update(_output_process_scope_inputs(evidence, scopes, resources))

    active_scopes = {
        scope_id
        for scope_id, scope in scopes.items()
        if scope_id in scope_inputs or scope.redirections
    }
    _apply_scope_inputs(
        command_inputs,
        active_scopes,
        _resolved_scope_inputs(scopes, scope_inputs, resources, active_scopes),
        _scope_input_commands(scopes, command_scopes),
        _scope_depths(scopes),
    )
    return command_inputs


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
    """Replay ordered input descriptor bindings and return descriptor zero."""
    return _replay_input_bindings(
        command.redirections,
        pipe_inputs.get(command.command_id, OutsideGap()),
        process_resources,
    )


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
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _OUTPUT_REDIRECTION_OPERATORS:
            continue
        if (
            isinstance(event.target, StaticResourceTarget)
            and event.operator not in _APPEND_REDIRECTION_OPERATORS
        ):
            writes.append(_FlowWrite(event.target.key, LiteralTransfer("")))
    bindings = _output_bindings(events)
    for descriptor, (target, append) in bindings.items():
        if not isinstance(target, StaticResourceTarget):
            continue
        writes.append(
            _FlowWrite(
                target.key,
                output if descriptor == 1 else OutsideGap(),
                append=append,
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
            elif isinstance(current, ScopeOutput):
                values.append(StreamRef(current.scope_id))
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
                elif isinstance(current, RepeatOutput):
                    pending.append((current.part, False))
        return values[0]


def stream_ref_ids(expression: ContentExpr) -> tuple[int, ...]:
    """Return stream references visible through authored content composition only."""
    pending = [expression]
    stream_ids: list[int] = []
    while pending:
        current = pending.pop()
        if isinstance(current, StreamRef):
            stream_ids.append(current.scope_id)
        elif isinstance(current, Choice | Concat):
            pending.extend(reversed(current.parts))
    return tuple(stream_ids)


def _scope_environment_ids(
    scopes: tuple[_StreamScopeEvidence, ...],
) -> tuple[dict[int, int], dict[int, int | None]]:
    """Return lexical shell environments and their inherited parent environments."""
    by_id = {scope.scope_id: scope for scope in scopes}
    environments: dict[int, int] = {}
    parents: dict[int, int | None] = {}

    for scope in scopes:
        if scope.scope_id in environments:
            continue
        path: list[int] = []
        active: set[int] = set()
        current = scope.scope_id
        while current not in environments:
            if current in active:
                raise _MalformedTaintEvidence("shell taint stream scope cannot be structured")
            active.add(current)
            path.append(current)
            current_scope = by_id.get(current)
            if current_scope is None or current_scope.parent_scope_id is None:
                parent_environment = None
                break
            current = current_scope.parent_scope_id
        else:
            parent_environment = environments[current]

        for scope_id in reversed(path):
            current_scope = by_id.get(scope_id)
            if (
                current_scope is not None
                and current_scope.kind == "brace_group"
                and parent_environment is not None
            ):
                environment = parent_environment
            else:
                environment = scope_id
                parents[scope_id] = parent_environment
            environments[scope_id] = environment
            parent_environment = environment
    return environments, parents


def _scoped_variable_name(environment: int, name: str) -> str:
    """Return an internal variable-table key that shell source cannot spell."""
    if name.startswith("\0"):
        return name
    return f"\0{environment}\0{name}"


def _unscoped_variable_name(name: str) -> str:
    """Return the shell variable name from one internal scoped key."""
    if not name.startswith("\0"):
        return name
    _environment, separator, unscoped = name[1:].partition("\0")
    return unscoped if separator else name


def _scope_expression(expression: ContentExpr, environment: int) -> ContentExpr:
    """Bind first-pass shell variable references to one lexical environment."""
    if isinstance(expression, VariableRef):
        return VariableRef(_scoped_variable_name(environment, expression.name))
    if isinstance(
        expression,
        LiteralTransfer | _SecondPassVariableRef | OutsideGap | ResourceRef | StreamRef,
    ):
        return expression
    if isinstance(expression, Choice):
        return choice(*(_scope_expression(part, environment) for part in expression.parts))
    return concat(*(_scope_expression(part, environment) for part in expression.parts))


def _contextualize_evidence(evidence: _ShellTaintEvidence) -> _ShellTaintEvidence:
    """Bind command-local first-pass variable reads before solving global flow tables."""
    if not evidence.scopes:
        return evidence
    environments, _parents = _scope_environment_ids(evidence.scopes)
    commands = tuple(
        replace(
            command,
            argv=tuple(
                replace(
                    port,
                    content=_scope_expression(
                        port.content,
                        environments.get(command.container_scope_id, command.container_scope_id),
                    ),
                )
                for port in command.argv
            ),
            assignments=tuple(
                replace(
                    assignment,
                    content=_scope_expression(
                        assignment.content,
                        environments.get(command.container_scope_id, command.container_scope_id),
                    ),
                )
                for assignment in command.assignments
            ),
            redirections=tuple(
                replace(
                    event,
                    target=ContentTarget(
                        _scope_expression(
                            event.target.content,
                            environments.get(
                                command.container_scope_id,
                                command.container_scope_id,
                            ),
                        )
                    ),
                )
                if isinstance(event.target, ContentTarget)
                else event
                for event in command.redirections
            ),
        )
        for command in evidence.commands
    )
    scopes = tuple(
        replace(
            scope,
            redirections=tuple(
                replace(
                    event,
                    target=ContentTarget(
                        _scope_expression(
                            event.target.content,
                            environments.get(scope.scope_id, scope.scope_id),
                        )
                    ),
                )
                if isinstance(event.target, ContentTarget)
                else event
                for event in scope.redirections
            ),
            loop_bindings=tuple(
                replace(
                    binding,
                    name=_scoped_variable_name(
                        environments.get(scope.scope_id, scope.scope_id),
                        binding.name,
                    ),
                    content=_scope_expression(
                        binding.content,
                        environments.get(scope.scope_id, scope.scope_id),
                    ),
                )
                for binding in scope.loop_bindings
            ),
        )
        for scope in evidence.scopes
    )
    return replace(evidence, commands=commands, scopes=scopes)


def _build_flow_definitions(
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits = TaintLimits(),  # noqa: B008
) -> tuple[_FlowDefinitions, dict[int, ContentExpr]]:
    """Lower typed shell evidence into fixed-point definitions and command stdin."""
    environments, environment_parents = _scope_environment_ids(evidence.scopes)
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
    scoped_variables = bool(evidence.scopes)
    has_eval = any(_builtin_eval_candidates(command) for command in evidence.commands)
    command_environments = {
        command.command_id: environments.get(command.container_scope_id, command.container_scope_id)
        for command in evidence.commands
    }

    def visible_environments(environment: int) -> set[int]:
        visible: set[int] = set()
        current: int | None = environment
        while current is not None and current not in visible:
            visible.add(current)
            current = environment_parents.get(current)
        return visible

    for command in evidence.commands:
        origin_environment = command_environments[command.command_id]
        for target_environment in set(command_environments.values()):
            if origin_environment not in visible_environments(target_environment):
                continue
            variable_writes.extend(
                _FlowWrite(
                    (
                        _scoped_variable_name(target_environment, assignment.name)
                        if scoped_variables
                        else assignment.name
                    ),
                    assignment.content,
                    append=assignment.append,
                )
                for assignment in command.assignments
            )
        if scoped_variables and has_eval and environment_parents.get(origin_environment) is None:
            variable_writes.extend(
                _FlowWrite(assignment.name, assignment.content, append=assignment.append)
                for assignment in command.assignments
            )
        output = _producer_stdout(command, inputs[command.command_id])
        stream_writes.append(_FlowWrite(command.output_scope_id, output))
        resource_writes.extend(_static_write_definitions(command.redirections, output))

    lowering = _OutputLowering(command_scopes)
    target_environments = set(environments.values())
    visible_by_target = {
        target_environment: visible_environments(target_environment)
        for target_environment in target_environments
    }
    variable_keys = {write.key for write in variable_writes}
    for scope in evidence.scopes:
        origin_environment = environments.get(scope.scope_id, scope.scope_id)
        for target_environment, visible in visible_by_target.items():
            if origin_environment not in visible:
                continue
            for binding in scope.loop_bindings:
                key = _scoped_variable_name(
                    target_environment,
                    _unscoped_variable_name(binding.name),
                )
                if len(variable_writes) >= limits.max_edges:
                    raise _TaintLimitExceeded("shell taint edge limit exceeded")
                if key not in variable_keys and len(variable_keys) >= limits.max_table_entries:
                    raise _TaintLimitExceeded("shell taint table entry limit exceeded")
                variable_writes.append(_FlowWrite(key, binding.content, append=binding.append))
                variable_keys.add(key)
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


def _eval_arguments_raw(command: _CommandEvidence, executable: _ExecutableEvidence) -> ContentExpr:
    """Return eval arguments joined by the literal shell argument separator."""
    head_index = executable.argv_index
    if head_index is None:
        return LiteralTransfer("")
    arguments = command.argv[head_index + 1 :]
    parts: list[ContentExpr] = []
    for index, argument in enumerate(arguments):
        if index:
            parts.append(LiteralTransfer(" "))
        parts.append(argument.content)
    return concat(*parts)


def _eval_arguments_from(command: _CommandEvidence, executable: _ExecutableEvidence) -> ContentExpr:
    """Return second-pass eval input after joining argv with authored spaces."""
    return _eval_reparse_content(_eval_arguments_raw(command, executable))


_MAX_EVAL_REPARSE_BRANCHES = 256
_MAX_EVAL_REPARSE_DEPTH = 512
_EVAL_QUOTE_STATES = (None, "'", '"')
_EVAL_ANSI_OCTAL_BASE = 8
_EVAL_UNICODE_MAX = 0x10FFFF
_EVAL_SURROGATE_MIN = 0xD800
_EVAL_SURROGATE_MAX = 0xDFFF
_EVAL_SPECIAL_PARAMETERS = frozenset("@*#?-$!0123456789")
_EVAL_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}

_EvalSyntaxValue: TypeAlias = frozenset[tuple[_TransferSummary, str | None]]  # noqa: UP040


def _eval_reparse_content(expression: ContentExpr) -> ContentExpr:
    """Interpret the minimal shell syntax that ``eval`` reparses from literal content."""
    branches = _eval_reparse_branches(expression, quote=None, depth=0)
    return choice(*(branch for branch, _quote in branches))


def _eval_reparse_branches(
    expression: ContentExpr,
    *,
    quote: str | None,
    depth: int,
) -> list[tuple[ContentExpr, str | None]]:
    """Reparse content while retaining quote state across symbolic expression boundaries."""
    if depth > _MAX_EVAL_REPARSE_DEPTH:
        raise _TaintLimitExceeded("shell taint eval reparse depth limit exceeded")
    if isinstance(expression, LiteralTransfer):
        parsed, resulting_quote = _eval_reparse_literal(expression.text, quote)
        return [(parsed, resulting_quote)]
    if isinstance(expression, Concat):
        branches: list[tuple[ContentExpr, str | None]] = [(LiteralTransfer(""), quote)]
        for part in _coalesced_eval_parts(expression.parts):
            expanded: list[tuple[ContentExpr, str | None]] = []
            for prefix, current_quote in branches:
                for suffix, resulting_quote in _eval_reparse_branches(
                    part,
                    quote=current_quote,
                    depth=depth + 1,
                ):
                    expanded.append((concat(prefix, suffix), resulting_quote))
                    if len(expanded) > _MAX_EVAL_REPARSE_BRANCHES:
                        raise _TaintLimitExceeded("shell taint eval reparse branch limit exceeded")
            branches = expanded
        return branches
    if isinstance(expression, Choice):
        branches = []
        for part in expression.parts:
            branches.extend(_eval_reparse_branches(part, quote=quote, depth=depth + 1))
            if len(branches) > _MAX_EVAL_REPARSE_BRANCHES:
                raise _TaintLimitExceeded("shell taint eval reparse branch limit exceeded")
        return branches
    return [(expression, quote)]


def _eval_reparse_literal(  # noqa: PLR0912, PLR0915
    text: str, quote: str | None
) -> tuple[ContentExpr, str | None]:
    """Reparse quote, escape, and simple parameter syntax from one literal transfer."""
    parts: list[ContentExpr] = []
    literal: list[str] = []
    index = 0

    def flush_literal() -> None:
        if literal:
            parts.append(LiteralTransfer("".join(literal)))
            literal.clear()

    while index < len(text):
        character = text[index]
        if quote == "'":
            if character == "'":
                quote = None
            else:
                literal.append(character)
            index += 1
            continue
        if quote == '"' and character == '"':
            quote = None
            index += 1
            continue
        if quote is None and character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if quote is None and text.startswith("$'", index):
            decoded, index = _eval_ansi_c_literal(text, index)
            literal.append(decoded)
            continue
        if character == "\\":
            if index + 1 >= len(text):
                literal.append(character)
                index += 1
                continue
            escaped = text[index + 1]
            if quote == '"' and escaped not in {"$", '"', "\\", "`", "\n"}:
                literal.extend((character, escaped))
            elif escaped != "\n":
                literal.append(escaped)
            index += 2
            continue
        if character == "$":
            if text.startswith("${", index):
                closing = text.find("}", index + 2)
                contents = text[index + 2 : closing] if closing != -1 else ""
                if (
                    closing == -1
                    or not contents
                    or contents in _EVAL_SPECIAL_PARAMETERS
                    or contents.isdigit()
                    or _eval_identifier_end(contents, 0) != len(contents)
                ):
                    flush_literal()
                    parts.append(OutsideGap())
                    index = closing + 1 if closing != -1 else len(text)
                    continue
            name, end = _eval_parameter_name(text, index)
            if name is not None:
                flush_literal()
                parts.append(_SecondPassVariableRef(name))
                index = end
                continue
            if index + 1 < len(text) and text[index + 1] in _EVAL_SPECIAL_PARAMETERS:
                flush_literal()
                parts.append(OutsideGap())
                index += 2
                continue
        literal.append(character)
        index += 1
    flush_literal()
    return concat(*parts), quote


def _eval_parameter_name(text: str, start: int) -> tuple[str | None, int]:
    """Return one simple eval parameter name without interpreting complex shell forms."""
    index = start + 1
    if index < len(text) and text[index] == "{":
        name_start = index + 1
        name_end = _eval_identifier_end(text, name_start)
        if name_end > name_start and name_end < len(text) and text[name_end] == "}":
            return text[name_start:name_end], name_end + 1
        return None, start + 1
    name_end = _eval_identifier_end(text, index)
    return (text[index:name_end], name_end) if name_end > index else (None, start + 1)


def _eval_identifier_end(text: str, start: int) -> int:
    """Return the exclusive endpoint of an ASCII shell identifier."""
    if start >= len(text) or not (text[start].isalpha() or text[start] == "_"):
        return start
    index = start + 1
    while index < len(text) and (text[index].isalnum() or text[index] == "_"):
        index += 1
    return index


def _eval_ansi_c_literal(text: str, start: int) -> tuple[str, int]:
    """Decode one second-pass ANSI-C shell literal without importing the scanner."""
    characters: list[str] = []
    index = start + 2
    while index < len(text):
        character = text[index]
        if character == "'":
            return "".join(characters), index + 1
        if character != "\\":
            characters.append(character)
            index += 1
            continue
        decoded, index = _eval_ansi_c_escape(text, index + 1)
        characters.append(decoded)
    raise _TaintLimitExceeded("unterminated eval ANSI-C quoted literal")


def _eval_ansi_c_escape(text: str, start: int) -> tuple[str, int]:  # noqa: PLR0911
    """Decode the scanner-supported ANSI-C escape surface for eval reparsing."""
    if start >= len(text):
        return "\\", start
    character = text[start]
    if character in _EVAL_ANSI_C_SIMPLE_ESCAPES:
        return _EVAL_ANSI_C_SIMPLE_ESCAPES[character], start + 1
    if character in "01234567":
        return _eval_ansi_c_digits(text, start, 8, 3, byte_mask=True)
    if character == "x":
        return _eval_ansi_c_digits(text, start + 1, 16, 2, prefix="x")
    if character == "u":
        return _eval_ansi_c_digits(text, start + 1, 16, 4, prefix="u")
    if character == "U":
        return _eval_ansi_c_digits(text, start + 1, 16, 8, prefix="U")
    if character == "c" and start + 1 < len(text):
        controlled = text[start + 1]
        value = 127 if controlled == "?" else ord(controlled.upper()) & 0x1F
        return _eval_ansi_c_character(value), start + 2
    return f"\\{character}", start + 1


def _eval_ansi_c_digits(  # noqa: PLR0913
    text: str,
    start: int,
    base: int,
    limit: int,
    *,
    prefix: str | None = None,
    byte_mask: bool = False,
) -> tuple[str, int]:
    """Decode a bounded octal or hexadecimal ANSI-C escape digit sequence."""
    valid = "01234567" if base == _EVAL_ANSI_OCTAL_BASE else "0123456789abcdefABCDEF"
    end = start
    while end < len(text) and end - start < limit and text[end] in valid:
        end += 1
    if end == start:
        return f"\\{prefix}" if prefix is not None else "\\", end
    value = int(text[start:end], base)
    if byte_mask:
        value &= 0xFF
    return _eval_ansi_c_character(value), end


def _eval_ansi_c_character(value: int) -> str:
    """Return a representable ANSI-C decoded character or fail closed."""
    if (
        value == 0
        or value > _EVAL_UNICODE_MAX
        or _EVAL_SURROGATE_MIN <= value <= _EVAL_SURROGATE_MAX
    ):
        raise _TaintLimitExceeded("eval ANSI-C escape cannot be represented")
    return chr(value)


def _eval_syntax_outside(quote: str | None) -> _EvalSyntaxValue:
    """Keep unknown external text non-evidentiary without inventing quote syntax."""
    return frozenset((summary, quote) for summary in _OUTSIDE_VALUE)


def _cap_eval_syntax(value: _EvalSyntaxValue, limits: TaintLimits) -> _EvalSyntaxValue:
    if len(value) > limits.max_alternatives:
        raise _TaintLimitExceeded("shell taint eval syntax alternative limit exceeded")
    return value


def _eval_syntax_expression(  # noqa: PLR0913
    expression: ContentExpr,
    quote: str | None,
    variables: dict[tuple[str | int, str | None], _EvalSyntaxValue],
    raw_variables: dict[str | int, _ContentValue],
    limits: TaintLimits,
    *,
    depth: int = 0,
) -> _EvalSyntaxValue:
    """Evaluate eval-reparsed syntax with quote-sensitive variable definitions."""
    if depth > _MAX_EVAL_REPARSE_DEPTH:
        raise _TaintLimitExceeded("shell taint eval syntax depth limit exceeded")
    if isinstance(expression, LiteralTransfer):
        reparsed, resulting_quote = _eval_reparse_literal(expression.text, quote)
        return frozenset(
            (summary, resulting_quote)
            for summary in _evaluate_with_tables(reparsed, raw_variables, {}, {}, limits)
        )
    if isinstance(expression, VariableRef):
        return variables.get((expression.name, quote), _eval_syntax_outside(quote))
    if isinstance(expression, OutsideGap | ResourceRef | StreamRef):
        return _eval_syntax_outside(quote)
    if isinstance(expression, Choice):
        value = frozenset(
            alternative
            for part in expression.parts
            for alternative in _eval_syntax_expression(
                part,
                quote,
                variables,
                raw_variables,
                limits,
                depth=depth + 1,
            )
        )
        return _cap_eval_syntax(value, limits)
    if not isinstance(expression, Concat):
        return _eval_syntax_outside(quote)
    value: _EvalSyntaxValue = frozenset({(_EPSILON, quote)})
    for part in _coalesced_eval_parts(expression.parts):
        value = _cap_eval_syntax(
            frozenset(
                (summary.compose(after), resulting_quote)
                for summary, current_quote in value
                for after, resulting_quote in _eval_syntax_expression(
                    part,
                    current_quote,
                    variables,
                    raw_variables,
                    limits,
                    depth=depth + 1,
                )
            ),
            limits,
        )
    return value


def _coalesced_eval_parts(parts: tuple[ContentExpr, ...]) -> tuple[ContentExpr, ...]:
    """Join adjacent literal transfers before syntax reparsing crosses their boundaries."""
    coalesced: list[ContentExpr] = []
    literal: list[str] = []
    for part in parts:
        if isinstance(part, LiteralTransfer):
            literal.append(part.text)
            continue
        if literal:
            coalesced.append(LiteralTransfer("".join(literal)))
            literal.clear()
        coalesced.append(part)
    if literal:
        coalesced.append(LiteralTransfer("".join(literal)))
    return tuple(coalesced)


def _solve_eval_syntax_variables(
    writes: tuple[_FlowWrite, ...],
    raw_variables: dict[str | int, _ContentValue],
    limits: TaintLimits,
) -> dict[tuple[str | int, str | None], _EvalSyntaxValue]:
    """Solve quote-sensitive variable text only for eval's second parsing pass."""
    variables = {
        (write.key, quote): frozenset() for write in writes for quote in _EVAL_QUOTE_STATES
    }
    updates = 0
    changed = True
    while changed:
        changed = False
        for write in writes:
            for quote in _EVAL_QUOTE_STATES:
                prior = variables[(write.key, quote)]
                if write.append:
                    base = prior or _eval_syntax_outside(quote)
                    value = _cap_eval_syntax(
                        frozenset(
                            (summary.compose(after), resulting_quote)
                            for summary, current_quote in base
                            for after, resulting_quote in _eval_syntax_expression(
                                write.expression,
                                current_quote,
                                variables,
                                raw_variables,
                                limits,
                            )
                        ),
                        limits,
                    )
                else:
                    value = _eval_syntax_expression(
                        write.expression,
                        quote,
                        variables,
                        raw_variables,
                        limits,
                    )
                widened = _cap_eval_syntax(prior | value, limits)
                if widened == prior:
                    continue
                variables[(write.key, quote)] = widened
                updates += 1
                if updates > limits.max_fixed_point_updates:
                    raise _TaintLimitExceeded(
                        "shell taint eval syntax fixed-point update limit exceeded"
                    )
                changed = True
    return variables


def _builtin_eval_candidates(command: _CommandEvidence) -> tuple[_ExecutableEvidence, ...]:
    """Return the exact builtin eval candidates whose arguments enter a second parse."""
    return tuple(
        executable
        for executable in _iter_executable_evidence(command.executable)
        if (
            executable.name == "eval"
            and executable.literal == "eval"
            and not executable.external_lookup
        )
    )


def _eval_content_dependencies(expression: ContentExpr) -> set[str]:
    """Collect variable names that can enter eval syntax from one authored expression."""
    names: set[str] = set()
    pending = [_eval_reparse_content(expression)]
    while pending:
        current = pending.pop()
        if isinstance(current, VariableRef):
            names.add(current.name)
        elif isinstance(current, Choice | Concat):
            pending.extend(current.parts)
    return names


def _reachable_eval_variable_writes(
    commands: tuple[_CommandEvidence, ...], writes: tuple[_FlowWrite, ...]
) -> tuple[_FlowWrite, ...]:
    """Retain only variable definitions reachable from exact builtin eval argument content."""
    names: set[str] = set()
    for command in commands:
        for executable in _builtin_eval_candidates(command):
            names.update(_eval_content_dependencies(_eval_arguments_raw(command, executable)))
    changed = True
    while changed:
        changed = False
        for write in writes:
            if not isinstance(write.key, str) or write.key not in names:
                continue
            dependencies = _eval_content_dependencies(write.expression)
            if dependencies <= names:
                continue
            names.update(dependencies)
            changed = True
    return tuple(write for write in writes if isinstance(write.key, str) and write.key in names)


def _eval_sink_marker_capable(
    command: _CommandEvidence,
    variables: dict[tuple[str | int, str | None], _EvalSyntaxValue],
    raw_variables: dict[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether any builtin eval candidate reparses an authored marker flow."""
    for executable in _builtin_eval_candidates(command):
        raw = _eval_arguments_raw(command, executable)
        if _marker_capable(_evaluate_with_tables(raw, raw_variables, {}, {}, limits)):
            return True
        if any(
            summary.full.entries[_DFA_START][1]
            for summary, _quote in _eval_syntax_expression(
                raw,
                None,
                variables,
                raw_variables,
                limits,
            )
        ):
            return True
    return False


def _script_port_expression(port: _ArgPort) -> ContentExpr:
    """Return a static script resource reference or an external gap."""
    if port.process_resource_id is not None:
        return OutsideGap()
    key = normalize_static_resource(port.literal, dynamic=port.dynamic)
    return ResourceRef(key) if key is not None else OutsideGap()


def _shell_script_source_expression(
    port: _ArgPort,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> ContentExpr:
    """Return the source expression when one shell port becomes a script operand."""
    if port.process_resource_id is not None:
        return _process_resource_input(port.process_resource_id, process_resources)
    return _script_port_expression(port)


def _candidate_sink_expressions(  # noqa: PLR0911, PLR0912
    command: _CommandEvidence,
    executable: _ExecutableEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    """Return conservative sink expressions for one resolved executable candidate."""
    if executable.argv_index is None or executable.name is None:
        return ()
    name = executable.name
    literal = executable.literal
    direct_sinks: tuple[ContentExpr, ...] = ()
    if literal is not None and "/" in literal:
        key = normalize_static_resource(literal, dynamic=False)
        if key is not None:
            direct_sinks = (ResourceRef(key),)
    if name == "eval" and literal == "eval" and not executable.external_lookup:
        return (_eval_arguments_from(command, executable),)
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
            return direct_sinks
        if selection.kind is _ShellSourceKind.STDIN:
            return (stdin, *direct_sinks)
        if selection.kind is _ShellSourceKind.COMMAND:
            if selection.argv_index is None:
                return direct_sinks
            return (command.argv[selection.argv_index].content, *direct_sinks)
        if selection.kind is _ShellSourceKind.SCRIPT:
            if selection.argv_index is None:
                return direct_sinks
            port = command.argv[selection.argv_index]
            return (_shell_script_source_expression(port, process_resources), *direct_sinks)
        candidates: list[ContentExpr] = []
        for index in selection.candidate_indices:
            port = command.argv[index]
            candidates.extend(
                (port.content, _shell_script_source_expression(port, process_resources))
            )
        if selection.include_stdin:
            candidates.append(stdin)
        return (choice(*candidates), *direct_sinks)
    return direct_sinks


def _iter_executable_evidence(executable: _ExecutableEvidence) -> Iterator[_ExecutableEvidence]:
    """Yield nested alternates before their primary, without revisiting synthetic evidence."""
    pending = [(executable, False)]
    seen: set[int] = set()
    while pending:
        candidate, expanded = pending.pop()
        candidate_id = id(candidate)
        if expanded:
            yield candidate
            continue
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        pending.append((candidate, True))
        pending.extend((alternate, False) for alternate in reversed(candidate.alternates))


def _executable_alternate_count(executable: _ExecutableEvidence) -> int:
    """Return distinct nested alternates without counting the primary command evidence."""
    return sum(1 for _ in _iter_executable_evidence(executable)) - 1


def _sink_expressions(
    command: _CommandEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    """Return every conservative execution sink expression for all candidates."""
    expressions: list[ContentExpr] = []
    for executable in _iter_executable_evidence(command.executable):
        expressions.extend(
            _candidate_sink_expressions(command, executable, stdin, process_resources)
        )
    return tuple(expressions)


def analyze_marker_taint(  # noqa: PLR0911
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits = TaintLimits(),  # noqa: B008
) -> tuple[bool, str | None]:
    """Return a fail-closed verdict for authored marker flow in one run body."""
    if any(scope.kind not in _STREAM_SCOPE_KINDS for scope in evidence.scopes):
        return True, "shell taint stream scope cannot be structured"
    if any(
        (pipe.consumer_command_id is None) == (pipe.consumer_scope_id is None)
        for pipe in evidence.pipes
    ):
        return True, "shell taint pipe cannot be structured"
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
        len(evidence.commands)
        + len(evidence.scopes)
        + len(evidence.process_resources)
        + sum(_executable_alternate_count(command.executable) for command in evidence.commands)
    )
    if evidence_entries > limits.max_table_entries:
        return True, "shell taint table entry limit exceeded"

    try:
        _validate_nested_evidence(evidence, limits)
        evidence = _contextualize_evidence(evidence)
        definitions, inputs = _build_flow_definitions(evidence, limits=limits)
        solved = _solve_flow_definitions(definitions, limits=limits)
        eval_commands = tuple(
            command for command in evidence.commands if _builtin_eval_candidates(command)
        )
        eval_syntax_variables = (
            _solve_eval_syntax_variables(
                _reachable_eval_variable_writes(eval_commands, definitions.variable_writes),
                solved.variables,
                limits,
            )
            if eval_commands
            else {}
        )
        process_resources = {
            resource.resource_id: resource for resource in evidence.process_resources
        }
        for command in evidence.commands:
            if command in eval_commands and _eval_sink_marker_capable(
                command,
                eval_syntax_variables,
                solved.variables,
                limits,
            ):
                return True, TAINT_REFUSAL_REASON
            stdin = inputs[command.command_id]
            for expression in _sink_expressions(command, stdin, process_resources):
                if _marker_capable(solved.evaluate(expression)):
                    return True, TAINT_REFUSAL_REASON
    except (_MalformedTaintEvidence, _TaintLimitExceeded) as error:
        return True, str(error)
    return False, None
