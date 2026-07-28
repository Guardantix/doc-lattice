"""Pure authored-marker taint analysis for one CI shell run body."""

from __future__ import annotations

import fnmatch
import shlex
from collections import ChainMap
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

from doc_lattice.error_types import ProjectError
from doc_lattice.github_ci.shell_guards import (
    Certified,
    GuardRefusal,
    MarkerDetected,
    ScanVerdict,
    TaintLimits,
)

__all__ = ["TaintLimits", "analyze_marker_taint"]

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

TAINT_REFUSAL_REASON = "authored marker flow reaches an execution sink"
_RANGE_PARTS_WITH_STEP = 3
_MAX_BRACE_INTEGER_DIGITS = 256
_QUOTED_FUNCTION_POSITIONAL_STAR = "\0quoted-function-positional-star"
_STATIC_EVAL_SHADOW_NAMES = frozenset({"builtin", "command", "eval", "false", "true"})
_UNICODE_MAX = 0x10FFFF
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
_OCTAL_BYTE_MASK = 0xFF
# The single Bash ``$'...'`` simple-escape table. The scanner, the first-pass static eval replay
# and the second-pass reparser must agree byte for byte, or the value recorded for a variable
# assigned inside an eval payload differs from the one Bash sets.
_ANSI_C_SIMPLE_ESCAPES = {
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
class _SecondPassConditionalAssignment:
    """One eval-time ``${name=word}`` or ``${name:=word}`` expansion."""

    name: str
    operand: ContentExpr
    assign_if_null: bool

    @property
    def parts(self) -> tuple[ContentExpr, ContentExpr]:
        """Expose the expansion's mutually exclusive existing/default values."""
        return (_SecondPassVariableRef(self.name), self.operand)


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
    | _SecondPassConditionalAssignment
    | StreamRef
    | ResourceRef
    | Choice
    | Concat
    | OutsideGap
)


class _TaintLimitExceeded(ProjectError):
    """A deterministic taint bound prevented certification.

    The constructor accepts only a `GuardRefusal` so a bound can never be reported as bare text.
    Handlers that re-raise or re-wrap this error carry `refusal` through unchanged.
    """

    def __init__(self, refusal: GuardRefusal) -> None:
        if not isinstance(refusal, GuardRefusal):
            raise TypeError("a fail-closed taint bound must carry a GuardRefusal origin")
        super().__init__(refusal.reason, code="SHELL_TAINT_LIMIT_EXCEEDED")
        self.refusal = refusal


class _MalformedTaintEvidence(ProjectError):
    """Structured shell evidence cannot be analyzed safely.

    The constructor accepts only a `GuardRefusal` so malformed evidence can never be reported as
    bare text. Handlers that re-raise or re-wrap this error carry `refusal` through unchanged.
    """

    def __init__(self, refusal: GuardRefusal) -> None:
        if not isinstance(refusal, GuardRefusal):
            raise TypeError("malformed taint evidence must carry a GuardRefusal origin")
        super().__init__(refusal.reason, code="SHELL_TAINT_EVIDENCE_INVALID")
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class _FlowWrite:
    key: str | int
    expression: ContentExpr
    append: bool = False
    strip_trailing_newlines: bool = False
    read_ifs: str | None = None
    read_target_index: int | None = None
    read_target_count: int | None = None
    read_raw: bool = False


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
    active_argv_expansion: bool = False


@dataclass(frozen=True, slots=True)
class _AssignmentEvidence:
    """One shell variable assignment from the parsed run body."""

    name: str
    content: ContentExpr
    append: bool = False
    conditional: bool = False
    assign_if_null: bool = False
    from_stdin: bool = False
    nameref_target: str | None = None
    read_target_index: int | None = None
    read_target_count: int | None = None
    read_raw: bool = False
    read_ifs: str | None = None
    nameref_unset: bool = False


@dataclass(frozen=True, slots=True)
class _StaticEvalAssignment:
    """One exact eval assignment with builtin scope semantics."""

    assignment: _AssignmentEvidence
    local: bool = False
    force_global: bool = False
    eval_content: ContentExpr | None = None


@dataclass(frozen=True, slots=True)
class _StaticEvalCommand:
    """One exact eval simple command with reserved-word eligibility per word."""

    words: tuple[str, ...]
    keyword_eligible: tuple[bool, ...]
    source_words: tuple[str, ...]
    execution_status: bool | None = True
    active_function_names: frozenset[str] = frozenset()
    asynchronous: bool = False
    array_compound: bool = False
    redirections: tuple[_RedirectionEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class _StaticEvalExecutable:
    """Resolved executable identity for one bounded static eval command."""

    name: str
    negated: bool
    bypasses_functions: bool
    literal_status_available: bool
    argv_index: int = 0


@dataclass(slots=True)
class _StaticEvalControl:
    """Reachability state for one exact eval if/elif/else chain."""

    parent_status: bool | None
    prior_branch_status: bool | None = False
    current_test_status: bool | None = None
    body_status: bool | None = None
    phase: str = "test"


@dataclass(frozen=True, slots=True)
class _StaticEvalWordMetadata:
    """Lexical properties shlex does not retain for one eval word."""

    keyword_eligible: bool
    redirection_prefix: bool
    source: str


@dataclass(frozen=True, slots=True)
class _ExactFunctionEffect:
    """One ordered caller-state mutation performed by a function."""

    assignment: _AssignmentEvidence | None = None
    unset_name: str | None = None
    optional: bool = False


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
    definite_assignments: tuple[_AssignmentEvidence, ...] = ()
    builtin_assignments: tuple[_AssignmentEvidence, ...] = ()
    builtin_unsets: tuple[str, ...] = ()
    builtin_local: bool = False
    builtin_force_global: bool = False
    builtin_dynamic_options: bool = False
    unknown_builtin_content: ContentExpr | None = None
    unsupported_builtin_write: bool = False
    function_context_id: int | None = None
    function_name: str | None = None
    defines_function_context_id: int | None = None
    defines_function_name: str | None = None
    called_function_context_ids: tuple[int, ...] = ()
    function_entry_definitely_set: tuple[str, ...] = ()
    function_entry_assignments: tuple[_AssignmentEvidence, ...] = ()
    function_entry_unsets: tuple[str, ...] = ()
    function_effect_assignments: tuple[_AssignmentEvidence, ...] = ()
    function_effect_unsets: tuple[str, ...] = ()
    function_effect_conditional: bool = False
    resolved_eval_program: str | None = None
    resolved_eval_programs: tuple[str, ...] = ()
    runtime_eval_program_authoritative: bool = False
    execution_status: bool | None = True
    prune_unreachable_effects: bool = False
    conditionally_executed: bool = False
    conditional_operator: str | None = None
    isolated_execution: bool = False
    isolated_context_id: int | None = None
    active_function_names: frozenset[str] = frozenset()


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
    binding_command_id: int | None = None


@dataclass(frozen=True, slots=True)
class _PipeEvidence:
    """A producer scope piped to a command or compound-scope consumer."""

    producer_scope_id: int
    consumer_command_id: int | None = None
    consumer_scope_id: int | None = None
    includes_stderr: bool = False


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
    argv_ports: tuple[_WordContentPort, ...] | None = None
    brace_expansion_error: GuardRefusal | None = None


@dataclass(frozen=True, slots=True)
class _ContentToken:
    """One authored content fragment with brace-expansion provenance."""

    expression: ContentExpr
    literal: str
    brace_active: bool
    field_present: bool = False


@dataclass(frozen=True, slots=True)
class _WordContentPort:
    """One concrete argv field produced from a lexical shell word."""

    literal: str
    content: ContentExpr


def _token_content(tokens: list[_ContentToken]) -> ContentExpr:
    """Join tokens while coalescing adjacent literal transfers."""
    parts: list[ContentExpr] = []
    literal: list[str] = []
    for token in tokens:
        if isinstance(token.expression, LiteralTransfer):
            literal.append(token.expression.text)
            continue
        if literal:
            parts.append(LiteralTransfer("".join(literal)))
            literal.clear()
        parts.append(token.expression)
    if literal:
        parts.append(LiteralTransfer("".join(literal)))
    return concat(*parts)


def _active_character(token: _ContentToken, character: str) -> bool:
    """Return whether one token is an active, literal brace syntax character."""
    return (
        isinstance(token.expression, LiteralTransfer)
        and token.brace_active
        and token.literal == character
    )


def _literal_tokens(text: str) -> list[_ContentToken]:
    return [_ContentToken(LiteralTransfer(character), character, True) for character in text]


def _signed_decimal(value: str) -> str | None:
    """Return unsigned decimal digits after at most one sign."""
    digits = value[1:] if value[:1] in {"+", "-"} else value
    return digits if digits and digits.isascii() and digits.isdigit() else None


def _brace_integer(value: str) -> int:
    """Convert one bounded signed brace integer or fail closed."""
    digits = _signed_decimal(value)
    if digits is None:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.brace.integer-not-decimal", "shell taint brace expansion limit exceeded"
            )
        )
    if len(digits) > _MAX_BRACE_INTEGER_DIGITS:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.brace.integer-digit-limit", "shell taint brace expansion limit exceeded"
            )
        )
    return int(value)


def _brace_range_step(first: int, last: int, step_text: str | None) -> int:
    """Normalize a brace range step magnitude and endpoint-derived direction like Bash."""
    magnitude = abs(_brace_integer(step_text)) if step_text is not None else 1
    magnitude = magnitude or 1
    return magnitude if first <= last else -magnitude


def _brace_range_number(value: int, start: str, stop: str) -> str:
    """Render one numeric range member with Bash's endpoint-derived zero padding."""
    padded = any(
        endpoint[:1] != "+"
        and len(endpoint.removeprefix("-")) > 1
        and endpoint.removeprefix("-").startswith("0")
        for endpoint in (start, stop)
    )
    return f"{value:0{max(len(start), len(stop))}d}" if padded else str(value)


def _brace_alternatives(
    tokens: list[_ContentToken],
    limits: TaintLimits,
) -> list[list[_ContentToken]] | None:
    """Return bounded brace operands, or ``None`` for literal braces."""
    depth = 0
    comma_indices: list[int] = []
    for index, token in enumerate(tokens):
        if _active_character(token, "{"):
            depth += 1
        elif _active_character(token, "}"):
            depth -= 1
        elif _active_character(token, ",") and depth == 0:
            comma_indices.append(index)
    literal_text = "".join(token.literal for token in tokens)
    range_parts = literal_text.split("..")
    recognized_range = len(range_parts) in {2, 3} and all(
        isinstance(token.expression, LiteralTransfer) for token in tokens
    )
    if not comma_indices and not recognized_range:
        return None
    if comma_indices:
        # Brace expansion precedes parameter expansion, so the authored commas fix the field
        # count even when a member's own content is only known at run time.
        starts = [0, *(index + 1 for index in comma_indices)]
        stops = [*comma_indices, len(tokens)]
        return [tokens[start:stop] for start, stop in zip(starts, stops, strict=True)]
    start, stop = range_parts[:2]
    step_text = range_parts[2] if len(range_parts) == _RANGE_PARTS_WITH_STEP else None
    if step_text is not None and _signed_decimal(step_text) is None:
        return None
    if _signed_decimal(start) is not None and _signed_decimal(stop) is not None:
        first = _brace_integer(start)
        last = _brace_integer(stop)
        step = _brace_range_step(first, last, step_text)
        expansion_count = abs(last - first) // abs(step) + 1
        if expansion_count > limits.max_brace_expansions:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.brace.numeric-sequence-limit",
                    "shell taint brace expansion limit exceeded",
                )
            )
        values = range(first, last + (1 if step > 0 else -1), step)
        return [_literal_tokens(_brace_range_number(value, start, stop)) for value in values]
    if (
        len(start) == len(stop) == 1
        and start.isascii()
        and stop.isascii()
        and start.isalpha()
        and stop.isalpha()
    ):
        first = ord(start)
        last = ord(stop)
        step = _brace_range_step(first, last, step_text)
        expansion_count = abs(last - first) // abs(step) + 1
        if expansion_count > limits.max_brace_expansions:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.brace.alpha-sequence-limit", "shell taint brace expansion limit exceeded"
                )
            )
        return [
            _literal_tokens(chr(value))
            for value in range(first, last + (1 if step > 0 else -1), step)
        ]
    return None


def _expand_braces(
    tokens: list[_ContentToken], limits: TaintLimits, depth: int = 0
) -> list[list[_ContentToken]]:
    """Expand active comma lists and bounded ranges into concrete argv ports."""
    for start, token in enumerate(tokens):
        if not _active_character(token, "{"):
            continue
        nested = 1
        for stop in range(start + 1, len(tokens)):
            if _active_character(tokens[stop], "{"):
                nested += 1
            elif _active_character(tokens[stop], "}"):
                nested -= 1
                if nested:
                    continue
                alternatives = _brace_alternatives(tokens[start + 1 : stop], limits)
                if alternatives is None:
                    break
                if depth >= limits.max_brace_depth:
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.brace.depth-limit",
                            "shell taint brace expansion depth limit exceeded",
                        )
                    )
                expanded: list[list[_ContentToken]] = []
                for alternative in alternatives:
                    for result in _expand_braces(
                        [*tokens[:start], *alternative, *tokens[stop + 1 :]],
                        limits,
                        depth + 1,
                    ):
                        expanded.append(result)
                        if len(expanded) > limits.max_brace_expansions:
                            raise _TaintLimitExceeded(
                                GuardRefusal(
                                    "taint.brace.expansion-limit",
                                    "shell taint brace expansion limit exceeded",
                                )
                            )
                return expanded
        # An unrecognized outer brace can still contain a recognized nested expansion.
    return [tokens]


@dataclass(slots=True)
class ContentBuilder:
    """Incrementally construct one word's authored content evidence."""

    tokens: list[_ContentToken] = field(default_factory=list)
    assignment_value_start: int | None = None
    assignment_name: str | None = None
    assignment_append: bool = False
    conditional_assignments: list[_AssignmentEvidence] = field(default_factory=list)
    process_resource_id: int | None = None

    @classmethod
    def empty(cls) -> ContentBuilder:
        return cls()

    def append_literal(self, text: str, *, brace_active: bool = False) -> None:
        if text:
            self.tokens.append(_ContentToken(LiteralTransfer(text), text, brace_active))

    def append_expression(self, expression: ContentExpr) -> None:
        self.tokens.append(_ContentToken(expression, "", False))

    def mark_field_presence(self) -> None:
        """Retain an empty expansion result when quoting authored a real argv field."""
        self.tokens.append(
            _ContentToken(
                LiteralTransfer(""),
                "",
                False,
                field_present=True,
            )
        )

    def mark_assignment(self, name: str, *, append: bool) -> None:
        self.assignment_name = name
        self.assignment_append = append
        self.assignment_value_start = len(self.tokens)

    def add_conditional_assignment(self, assignment: _AssignmentEvidence) -> None:
        self.conditional_assignments.append(assignment)

    def build(
        self,
        limits: TaintLimits,
        *,
        defer_brace_errors: bool = False,
    ) -> _BuiltContent:
        expression = _token_content(self.tokens)
        assignment_content = (
            _token_content(self.tokens[self.assignment_value_start :])
            if self.assignment_value_start is not None
            else None
        )
        brace_expansion_error: GuardRefusal | None = None
        try:
            expanded_ports = _expand_braces(self.tokens, limits)
        except _TaintLimitExceeded as error:
            if not defer_brace_errors:
                raise
            brace_expansion_error = error.refusal
            expanded_ports = [self.tokens]
        ports = tuple(
            _WordContentPort(
                "".join(token.literal for token in expanded),
                _token_content(expanded),
            )
            for expanded in expanded_ports
            if expanded
        )
        return _BuiltContent(
            expression,
            assignment_name=self.assignment_name,
            assignment_content=assignment_content,
            assignment_append=self.assignment_append,
            conditional_assignments=tuple(self.conditional_assignments),
            process_resource_id=self.process_resource_id,
            argv_ports=ports,
            brace_expansion_error=brace_expansion_error,
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
        assignments: tuple[_AssignmentEvidence, ...] = (),
    ) -> None:
        """Replace one command redirection's placeholder with authored content."""
        for index, command in enumerate(self.commands):
            if command.command_id != command_id:
                continue
            redirections = tuple(
                replace(event, target=ContentTarget(content)) if event.ordinal == ordinal else event
                for event in command.redirections
            )
            self.commands[index] = replace(
                command,
                assignments=(*command.assignments, *assignments),
                redirections=redirections,
            )
            return
        raise ValueError("heredoc owner command is missing")

    def attach_scope_redirection_content(
        self,
        scope_id: int,
        ordinal: int,
        content: ContentExpr,
        assignments: tuple[_AssignmentEvidence, ...] = (),
    ) -> None:
        """Replace one compound-scope heredoc placeholder with authored content."""
        for index, scope in enumerate(self.scopes):
            if scope.scope_id != scope_id:
                continue
            redirections = tuple(
                replace(event, target=ContentTarget(content)) if event.ordinal == ordinal else event
                for event in scope.redirections
            )
            self.scopes[index] = replace(
                scope,
                redirections=redirections,
                loop_bindings=(*scope.loop_bindings, *assignments),
            )
            return
        raise ValueError("heredoc owner scope is missing")

    def attach_scope_assignments(
        self,
        scope_id: int,
        assignments: tuple[_AssignmentEvidence, ...],
    ) -> None:
        """Attach redirection-word side effects to their compound-scope environment."""
        if not assignments:
            return
        for index, scope in enumerate(self.scopes):
            if scope.scope_id == scope_id:
                self.scopes[index] = replace(
                    scope,
                    loop_bindings=(*scope.loop_bindings, *assignments),
                )
                return
        raise ValueError("compound assignment owner scope is missing")

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


_STDIN_DEVICE_PATHS = frozenset({"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"})
# Bash blanks are space and tab only, so Python's broader ``str.isspace`` must not decide word
# starts; newline and the shell metacharacters end a word as well.
_SHELL_WORD_SEPARATORS = frozenset(" \t\n;&|<>()")
# Comment recognition uses a narrower set on purpose. A parenthesis is a metacharacter only at
# the top level; the ``)`` that closes ``$(( ))``, ``$( )``, ``<( )`` or ``>( )`` stays inside the
# surrounding word, so Bash keeps a following ``#`` as data rather than opening a comment.
# Distinguishing the two needs the full expansion grammar, so the stripper declines to treat any
# parenthesis as a word start. That retains the occasional real ``(cmd)#comment`` as live text,
# which can only add a refusal and never certify a marker the shell would still run.
_SHELL_COMMENT_WORD_SEPARATORS = frozenset(" \t\n;&|<>")
_SHELL_HEADS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "rbash", "rzsh", "rksh"})

# Wrapper builtins whose EXTERNAL shadow AD-18 declines to reinterpret as a launcher. The
# suppression is scoped to these names alone: an ordinary external head such as ``timeout`` reached
# through ``command``/``env``/``exec`` still selects a shell that appears later in its own argv.
_EXTERNAL_WRAPPER_SHADOW_NAMES = frozenset({"builtin", "command", "exec"})
_SHELL_LONG_OPTION_NAMES_WITH_VALUE = frozenset({"rcfile", "init-file", "emulate"})
# The subset of those whose value NAMES A FILE the shell reads as its own source before it reaches
# the ``-c`` payload or script operand. Skipping the value word as an inert option argument dropped
# that file entirely, so ``bash --rcfile env.sh -ic :`` certified while Bash 5.2 executed the marker
# ``env.sh`` composes. ``emulate`` is deliberately absent: its value is a mode name (``sh``,
# ``ksh``), not a path, so reading it as a script source would name a file nothing runs.
_SHELL_INIT_FILE_OPTION_NAMES = frozenset({"rcfile", "init-file"})
_SHELL_EAGER_STOP_NAMES = frozenset({"help", "version", "dump-strings", "dump-po-strings"})
# Bash accepts a subset of its GNU long options spelled with a single leading dash -- ``bash
# -norc`` and ``bash -posix`` behave exactly like ``--norc``/``--posix`` (verified under real Bash
# 5.2). Recognizing these atomically keeps them out of the short-option-cluster fallback, which
# would otherwise decompose the word letter by letter and misread an internal ``o``, ``s``, or
# ``c`` as the unrelated short option of that letter (``-norc`` and ``-posix`` both contain one).
_SHELL_SINGLE_DASH_LONG_OPTION_NAMES = frozenset(
    {
        "debug",
        "debugger",
        "dump-po-strings",
        "dump-strings",
        "help",
        "init-file",
        "login",
        "noediting",
        "noprofile",
        "norc",
        "posix",
        "pretty-print",
        "rcfile",
        "restricted",
        "verbose",
        "version",
    }
)
_INPUT_REDIRECTION_OPERATORS = frozenset({"<", "<<", "<<-", "<<<", "<&", "<>"})
_OUTPUT_REDIRECTION_OPERATORS = frozenset({">", ">|", ">>", ">&", "<>", "&>", "&>>"})
_APPEND_REDIRECTION_OPERATORS = frozenset({">>", "&>>"})
_COMBINED_OUTPUT_REDIRECTION_OPERATORS = frozenset({"&>", "&>>"})
# Ordered longest first so a prefix match never wins over the longer operator that contains it.
# The scanner matches these against raw source text; the exact eval payload tokenizer matches an
# already-lexed punctuation run against the same set, so a spelling neither recognizes fails
# closed in both rather than being silently dropped by one of them.
_REDIRECTION_OPERATORS = (
    "&>>",
    "<<<",
    "<<-",
    "&>",
    "<<",
    ">>",
    "<>",
    ">&",
    "<&",
    ">|",
    ">",
    "<",
)
# ``/dev/fd/N`` and ``/proc/self/fd/N`` are descriptor aliases on Linux, not ordinary files;
# ``/dev/stdout`` is the fixed alias for descriptor 1. Modeling a write through one of these as a
# static resource replaces the implicit pipe target a producer's own descriptor 1 would otherwise
# carry, so a downstream ``_pipe_source`` lookup sees an ordinary file and discards the taint
# instead of routing it. The read side already resolves ``/dev/stdin`` correctly through the
# script-source path lookup, so this recognition is write-side only.
_DEV_FD_WRITE_PREFIXES = ("/dev/fd/", "/proc/self/fd/")
_DEV_STD_WRITE_DESCRIPTORS = {"/dev/stdout": 1, "/dev/stderr": 2}
_MAX_SHELL_DESCRIPTOR_DIGITS = 64


def _static_eval_descriptor(digits: str) -> int:
    """Parse one bounded descriptor an exact eval payload spells, or fail closed."""
    if len(digits) > _MAX_SHELL_DESCRIPTOR_DIGITS:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-descriptor.digit-limit", "shell eval payload cannot be tokenized"
            )
        )
    try:
        return int(digits)
    except ValueError as error:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-descriptor.unparsable", "shell eval payload cannot be tokenized"
            )
        ) from error


def _dev_fd_write_descriptor(resource: str, parse_descriptor: Callable[[str], int]) -> int | None:
    """Return the descriptor a normalized write-side ``/dev/fd`` family path names, if any.

    ``/dev/stderr`` is recognized alongside ``/dev/stdout`` for the reason given above the prefix
    table: without it, ``producer 2>&1 > /dev/stderr | bash`` modeled the write as an ordinary file
    and discarded the taint, while the equivalent ``> /dev/fd/2`` spelling refused.

    ``str.isdigit`` is true for non-ASCII decimal digits, which Bash does not accept as a
    descriptor, so the suffix is restricted to ASCII before it is parsed.
    """
    descriptor = _DEV_STD_WRITE_DESCRIPTORS.get(resource)
    if descriptor is not None:
        return descriptor
    for prefix in _DEV_FD_WRITE_PREFIXES:
        if resource.startswith(prefix):
            suffix = resource[len(prefix) :]
            if suffix.isascii() and suffix.isdigit():
                return parse_descriptor(suffix)
    return None


def resolve_redirection_target(
    literal: str,
    operator: str,
    *,
    dynamic: bool,
    parse_descriptor: Callable[[str], int],
    process_resource_id: int | None = None,
) -> RedirectionTarget:
    """Resolve one redirection operand word to the target it names.

    Two callers share this rule set: the scanner, which reads an authored redirection out of the
    run body, and the exact eval payload tokenizer, which reads one out of a payload the body
    only executes. Keeping the ``/dev/null``, ``/dev/fd`` family, descriptor duplication, and
    dynamic fallbacks in one place is what stops the payload route from recognizing a narrower
    set of targets than the authored route and certifying a write the authored spelling refuses
    (issue #146).

    The descriptor parser is supplied rather than fixed because each caller bounds and reports a
    malformed descriptor in its own vocabulary: the scanner stops the scan, and the taint module
    raises its own limit error.

    Args:
        literal: The operand word's literal text.
        operator: The redirection operator this operand belongs to.
        dynamic: Whether the operand's value is unknowable to this analysis.
        parse_descriptor: The caller's bounded descriptor-digit parser.
        process_resource_id: The process substitution this operand names, when it names one.

    Returns:
        The target this operand resolves to.
    """
    if process_resource_id is not None:
        return ProcessResourceTarget(process_resource_id)
    # ``str.isdigit`` accepts non-ASCII decimal digits, which Bash does not read as a descriptor:
    # ``>&`` followed by an Arabic-Indic digit creates a FILE named with that character. Treating
    # it as a duplication routed the write into a descriptor and recorded no resource write.
    if operator in {"<&", ">&"} and not dynamic and literal.isascii() and literal.isdigit():
        return DescriptorTarget(parse_descriptor(literal))
    resource = normalize_static_resource(literal, dynamic=dynamic)
    if resource == "/dev/null":
        return NullTarget()
    if resource is not None and operator in _OUTPUT_REDIRECTION_OPERATORS:
        descriptor = _dev_fd_write_descriptor(resource, parse_descriptor)
        if descriptor is not None:
            return DescriptorTarget(descriptor)
    if resource is not None:
        return StaticResourceTarget(resource)
    return DynamicResourceTarget()


_STREAM_SCOPE_KINDS = frozenset(
    {
        "brace_group",
        "case",
        "command",
        "command_substitution",
        "for",
        "if",
        "process_substitution",
        "select",
        "subshell_group",
        "pipeline",
        "until",
        "while",
    }
)
_SHARED_ENVIRONMENT_SCOPE_KINDS = frozenset(
    {"brace_group", "case", "for", "if", "select", "until", "while"}
)
# A Bash function definition persists past the compound that contained it: once the defining
# statement runs, the name stays callable for the rest of that shell, independent of whether the
# statement itself sat inside a body that might run zero or more times. A command's own
# ``execution_status`` is conservatively ``None`` (conditional) for every command inside a
# repeating, ``case``, or unresolved-``if`` body, since the scanner does not attempt to prove that
# a loop body runs at least once, that a case arm is selected, or that a dynamic ``if`` test
# succeeds -- but that conditionality describes the body as a whole, not a per-iteration branch
# the definition could uniquely miss. A dynamic ``then``, ``elif``, or ``else`` branch has exactly
# that body-may-run character, and the whole chain is one ``"if"`` scope kind. AD-18's "reproduces
# that function scope's aggregated stdout" already holds unconditionally for a top-level,
# definitely-taken ``if``-branch, subshell, or brace-group definition (whose own execution status
# is definite); this set extends the same registration to a definition whose defining statement
# has any control ancestor in one of these kinds, without relaxing the definite-status requirement
# for any other conditional shape. The ambiguous-status branch this set gates must only ever *add*
# a definition, never pop or overwrite one: an ambiguous body may or may not run, so an unset or
# redefinition it contains may or may not take effect, and dropping/overwriting an existing
# linkage on that uncertainty is fail-open.
_AMBIGUOUS_DEFINITION_SCOPE_KINDS = frozenset({"case", "for", "if", "select", "until", "while"})
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
    """A selected shell source port or conservative ambiguity set.

    ``init_file_indices`` is orthogonal to ``kind``: an ``--rcfile``/``--init-file`` value names a
    file the shell reads IN ADDITION to whichever source the option grammar finally selects, so it
    accumulates across the whole walk and is reported alongside every kind rather than replacing
    one. ``argv_index`` carries the first positional operand for a ``STDIN`` selection, which is
    where a ``-s`` invocation's ``$1`` begins.
    """

    kind: _ShellSourceKind
    argv_index: int | None = None
    candidate_indices: tuple[int, ...] = ()
    include_stdin: bool = False
    init_file_indices: tuple[int, ...] = ()


def _normalized_shell_head(name: str | None) -> str | None:
    """Return a case-insensitive shell head without an executable suffix."""
    if name is None:
        return None
    return name.casefold().removesuffix(".exe")


def _shell_long_option_name(literal: str) -> str | None:
    """Return the GNU long option name a ``--name`` or recognized single-dash word spells.

    Bash accepts several of its GNU long options with a single leading dash as a compatibility
    spelling (``-norc`` behaves exactly like ``--norc``, verified under real Bash 5.2). Recognizing
    that spelling atomically, before the short-option-cluster fallback ever sees the word, keeps a
    name such as ``-norc`` or ``-posix`` from being decomposed letter by letter, where its internal
    ``o``, ``s``, or ``c`` would otherwise be misread as the unrelated short option of that letter.
    A double-dash word is recognized unconditionally, matching every long option Bash accepts that
    way whether or not this analysis tracks it individually.

    Args:
        literal: One argv word's literal text.

    Returns:
        The option name without its leading dash(es), or None when the word does not spell a long
        option in either form.
    """
    if literal.startswith("--") and literal[2:]:
        return literal[2:]
    if (
        literal.startswith("-")
        and not literal.startswith("--")
        and literal[1:] in _SHELL_SINGLE_DASH_LONG_OPTION_NAMES
    ):
        return literal[1:]
    return None


def _select_shell_source(argv: tuple[_ArgPort, ...], head_index: int) -> _ShellSourceSelection:
    """Select the shell source according to its literal option grammar.

    The walk itself is ``_walk_shell_source``. This wrapper owns the ``--rcfile``/``--init-file``
    values the walk collects along the way and stamps them onto whichever selection it returns,
    which keeps the walk's many exits from each having to carry them.

    Args:
        argv: The command's argv ports.
        head_index: The argv index the shell option grammar is read from.

    Returns:
        The selected shell source, carrying any init-file values the grammar named.
    """
    init_files: list[int] = []
    selection = _walk_shell_source(argv, head_index, init_files)
    if not init_files:
        return selection
    return replace(selection, init_file_indices=tuple(init_files))


def _walk_shell_source(  # noqa: PLR0911, PLR0912
    argv: tuple[_ArgPort, ...], head_index: int, init_files: list[int]
) -> _ShellSourceSelection:
    """Walk one shell's literal option grammar, collecting init-file values into ``init_files``."""
    index = head_index + 1
    stdin_selected = False
    # ``-c`` only marks the command form. Bash keeps parsing options afterwards and takes the
    # first operand as the command string, so an intervening ``--`` or option must not be
    # mistaken for the payload.
    command_selected = False
    while index < len(argv):
        port = argv[index]
        if port.dynamic or port.active_argv_expansion:
            # An unquoted glob or other argv-widening syntax can resolve to a different word (or
            # several) at run time, exactly like a variable expansion this analysis cannot read
            # statically. Treating it the same way keeps a glob script operand (``bash ta*.sh``)
            # from resolving to its literal, almost certainly nonexistent, name instead of
            # widening to every word it could become.
            return _ShellSourceSelection(
                _ShellSourceKind.AMBIGUOUS,
                candidate_indices=tuple(range(index, len(argv))),
                include_stdin=True,
            )
        literal = port.literal
        if literal in ("-", "--"):
            index += 1
            if index >= len(argv):
                return _ShellSourceSelection(
                    _ShellSourceKind.NONE if command_selected else _ShellSourceKind.STDIN
                )
            if command_selected:
                return _ShellSourceSelection(_ShellSourceKind.COMMAND, argv_index=index)
            if stdin_selected:
                # ``bash -s -- doc- lattice`` reads its program from stdin and binds the words
                # after ``--`` as the child's positionals, starting at ``$1``.
                return _ShellSourceSelection(_ShellSourceKind.STDIN, argv_index=index)
            return _ShellSourceSelection(_ShellSourceKind.SCRIPT, argv_index=index)
        long_option_name = _shell_long_option_name(literal)
        if long_option_name is not None:
            if long_option_name in _SHELL_EAGER_STOP_NAMES:
                return _ShellSourceSelection(_ShellSourceKind.NONE)
            if long_option_name in _SHELL_LONG_OPTION_NAMES_WITH_VALUE:
                value_index = index + 1
                if (
                    value_index >= len(argv)
                    or argv[value_index].dynamic
                    or argv[value_index].active_argv_expansion
                ):
                    return _ShellSourceSelection(
                        _ShellSourceKind.AMBIGUOUS,
                        candidate_indices=tuple(range(index, len(argv))),
                        include_stdin=True,
                    )
                if long_option_name in _SHELL_INIT_FILE_OPTION_NAMES:
                    init_files.append(value_index)
                index += 2
            else:
                index += 1
            continue
        if literal and literal[0] in "-+":
            short_options = literal[1:]
            # ``-o``/``-O`` always take their value from the NEXT argv word, never from the
            # remainder of the current word, regardless of where in the cluster they appear
            # (verified under real Bash 5.2: ``bash -oe pipefail -c '...'`` sets both ``errexit``
            # and ``pipefail``, so ``-o`` reached across to the following word for its value while
            # ``e`` stayed a plain flag in the same cluster). Consuming a whole extra word per
            # occurrence, rather than only when ``o``/``O`` is the cluster's last character, keeps
            # a mid-cluster ``-o`` from letting the option's value word be misread as the script or
            # command operand.
            consumed_values = 0
            for option in short_options:
                if option == "c":
                    command_selected = True
                elif option == "s":
                    stdin_selected = True
                elif option in {"o", "O"}:
                    value_index = index + 1 + consumed_values
                    if (
                        value_index >= len(argv)
                        or argv[value_index].dynamic
                        or argv[value_index].active_argv_expansion
                    ):
                        return _ShellSourceSelection(
                            _ShellSourceKind.AMBIGUOUS,
                            candidate_indices=tuple(range(index, len(argv))),
                            include_stdin=True,
                        )
                    consumed_values += 1
            index += 1 + consumed_values
            continue
        if command_selected:
            return _ShellSourceSelection(_ShellSourceKind.COMMAND, argv_index=index)
        if stdin_selected:
            # As above: with ``-s`` in effect the first operand is ``$1``, not the program.
            return _ShellSourceSelection(_ShellSourceKind.STDIN, argv_index=index)
        return _ShellSourceSelection(_ShellSourceKind.SCRIPT, argv_index=index)
    # ``bash -c`` without an operand is a usage error, so nothing is executed.
    if command_selected:
        return _ShellSourceSelection(_ShellSourceKind.NONE)
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

    if not flattened:
        # ``Choice(())`` evaluates to the lattice bottom, and ``_compose_values`` annihilates
        # bottom, so an empty choice would erase a marker held by a sibling fragment of the same
        # Concat. An alternative-free expansion contributes nothing, which is epsilon.
        return LiteralTransfer("")
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
_MAX_TRACKED_LITERAL_CHARS = 4_096
_MAX_TRACKED_LITERAL_ALTERNATIVES = 16


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
    first_record: _DfaTransfer
    record_open: bool
    literal_texts: frozenset[str]
    projection_opaque: bool
    projection_incomplete: bool

    @classmethod
    def literal(cls, text: str) -> _TransferSummary:
        """Return a summary for authored literal text."""
        first_record, separator, _remainder = text.partition("\n")
        return cls(
            full=_DfaTransfer.literal(text),
            stripped=_DfaTransfer.literal(text.rstrip("\n")),
            newline_only=all(character == "\n" for character in text),
            first_record=_DfaTransfer.literal(first_record),
            record_open=not separator,
            literal_texts=(
                frozenset({text}) if len(text) <= _MAX_TRACKED_LITERAL_CHARS else frozenset()
            ),
            projection_opaque=False,
            projection_incomplete=len(text) > _MAX_TRACKED_LITERAL_CHARS,
        )

    @classmethod
    def barrier(cls) -> _TransferSummary:
        """Return a summary for opaque external content."""
        transfer = _DfaTransfer.barrier()
        return cls(
            full=transfer,
            stripped=transfer,
            newline_only=False,
            first_record=transfer,
            record_open=False,
            literal_texts=frozenset({""}),
            projection_opaque=True,
            projection_incomplete=False,
        )

    @classmethod
    def unknown(cls) -> _TransferSummary:
        """Return the summary for content whose exact text is no longer known."""
        # ``compose`` unions acceptance from both sides, so accepting from every entry state
        # keeps this alternative accepting under any surrounding text. That makes it an upper
        # bound for every concrete value it stands in for.
        transfer = _DfaTransfer(tuple((_DFA_START, True) for _ in range(_DFA_STATE_COUNT)))
        return cls(
            full=transfer,
            stripped=transfer,
            newline_only=False,
            first_record=transfer,
            record_open=True,
            literal_texts=frozenset(),
            projection_opaque=True,
            projection_incomplete=True,
        )

    def compose(self, following: _TransferSummary) -> _TransferSummary:
        """Return this summary followed by another summary."""
        stripped = (
            self.stripped if following.newline_only else self.full.compose(following.stripped)
        )
        literal_texts: set[str] = set()
        projection_incomplete = self.projection_incomplete or following.projection_incomplete
        if not projection_incomplete:
            for before in self.literal_texts:
                for after in following.literal_texts:
                    if len(before) + len(after) > _MAX_TRACKED_LITERAL_CHARS:
                        projection_incomplete = True
                        break
                    literal_texts.add(before + after)
                    if len(literal_texts) > _MAX_TRACKED_LITERAL_ALTERNATIVES:
                        projection_incomplete = True
                        break
                if projection_incomplete:
                    break
        if projection_incomplete:
            literal_texts.clear()
        return _TransferSummary(
            full=self.full.compose(following.full),
            stripped=stripped,
            newline_only=self.newline_only and following.newline_only,
            first_record=(
                self.first_record.compose(following.first_record)
                if self.record_open
                else self.first_record
            ),
            record_open=self.record_open and following.record_open,
            literal_texts=frozenset(literal_texts),
            projection_opaque=(self.projection_opaque or following.projection_opaque),
            projection_incomplete=projection_incomplete,
        )


_ContentValue: TypeAlias = frozenset[_TransferSummary]  # noqa: UP040
_EPSILON = _TransferSummary.literal("")
_OUTSIDE_VALUE = frozenset({_EPSILON, _TransferSummary.barrier()})
# The top of the projection lattice, used where an exact projection is no longer computable.
_UNKNOWN_VALUE = frozenset({_TransferSummary.unknown()})


def _merge_content_summaries(
    summaries: Iterable[_TransferSummary],
) -> _ContentValue:
    """Merge equal DFA behavior while retaining bounded literal alternatives."""
    merged: dict[
        tuple[_DfaTransfer, _DfaTransfer, bool, _DfaTransfer, bool],
        _TransferSummary,
    ] = {}
    for summary in summaries:
        key = (
            summary.full,
            summary.stripped,
            summary.newline_only,
            summary.first_record,
            summary.record_open,
        )
        prior = merged.get(key)
        if prior is None:
            merged[key] = summary
        elif prior != summary:
            literal_texts = prior.literal_texts | summary.literal_texts
            projection_incomplete = (
                prior.projection_incomplete
                or summary.projection_incomplete
                or len(literal_texts) > _MAX_TRACKED_LITERAL_ALTERNATIVES
            )
            merged[key] = replace(
                prior,
                literal_texts=(frozenset() if projection_incomplete else literal_texts),
                projection_opaque=(prior.projection_opaque or summary.projection_opaque),
                projection_incomplete=projection_incomplete,
            )
    return frozenset(merged.values())


def _join_values(*values: _ContentValue) -> _ContentValue:
    return _merge_content_summaries(summary for value in values for summary in value)


def _compose_values(left: _ContentValue, right: _ContentValue) -> _ContentValue:
    return _merge_content_summaries(before.compose(after) for before in left for after in right)


def _strip_trailing_newlines(value: _ContentValue) -> _ContentValue:
    return frozenset(
        _TransferSummary(
            full=alternative.stripped,
            stripped=alternative.stripped,
            newline_only=alternative.newline_only,
            first_record=alternative.first_record,
            record_open=alternative.record_open,
            literal_texts=frozenset(
                literal_text.rstrip("\n") for literal_text in alternative.literal_texts
            ),
            projection_opaque=alternative.projection_opaque,
            projection_incomplete=alternative.projection_incomplete,
        )
        for alternative in value
    )


def _project_read_value(
    value: _ContentValue,
    ifs: str,
    target_index: int,
    target_count: int,
    *,
    raw: bool,
) -> _ContentValue:
    """Apply one bounded scalar ``read`` projection after stream solving."""
    projected: set[_TransferSummary] = set()
    for alternative in value:
        if alternative.projection_incomplete:
            if target_count == 1 and alternative.first_record.entries[_DFA_START][1]:
                projected.add(_TransferSummary.literal("doc-lattice"))
                continue
            # The exact field split is gone, so the target could hold any substring of the
            # record, including one that leaves a partial marker open for the text that follows.
            # Widening to the top of the lattice keeps that flow visible at every sink instead of
            # refusing the whole body, which a marker-free chain of reads does not deserve.
            projected.update(_UNKNOWN_VALUE)
            continue
        if alternative.projection_opaque:
            projected.add(_TransferSummary.barrier())
            for literal_text in alternative.literal_texts:
                if sum(character in ifs for character in literal_text) > (
                    _MAX_TRACKED_LITERAL_ALTERNATIVES - 1
                ):
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.read.ifs-field-alternatives",
                            "shell read projection cannot be represented",
                        )
                    )
                relocatable_fields = _read_literal_fields(
                    literal_text,
                    ifs,
                    _MAX_TRACKED_LITERAL_ALTERNATIVES,
                    raw=raw,
                )
                sole_field = _read_literal_fields(literal_text, ifs, 1, raw=raw)
                if relocatable_fields is None or sole_field is None:
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.read.field-relocation",
                            "shell read projection cannot be represented",
                        )
                    )
                projected.update(
                    _TransferSummary.literal(field)
                    for field in (
                        *relocatable_fields,
                        sole_field[0],
                    )
                )
        for literal_text in alternative.literal_texts:
            fields = _read_literal_fields(
                literal_text,
                ifs,
                target_count,
                raw=raw,
            )
            if fields is None or target_index >= len(fields):
                projected.add(_TransferSummary.barrier())
                continue
            projected.add(_TransferSummary.literal(fields[target_index]))
    return _merge_content_summaries(projected)


def _marker_capable(value: _ContentValue) -> bool:
    return any(alternative.full.entries[_DFA_START][1] for alternative in value)


def _expression_nodes(expression: ContentExpr) -> int:
    pending = [expression]
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if isinstance(current, Choice | Concat | _SecondPassConditionalAssignment):
            pending.extend(current.parts)
    return nodes


def _expression_edges(expression: ContentExpr) -> int:
    pending = [expression]
    edges = 0
    while pending:
        current = pending.pop()
        if isinstance(current, VariableRef | _SecondPassVariableRef | ResourceRef | StreamRef):
            edges += 1
        elif isinstance(current, Choice | Concat | _SecondPassConditionalAssignment):
            pending.extend(current.parts)
    return edges


def _expression_identity(expression: ContentExpr) -> tuple[str | int | bool, ...]:
    """Return a flat structural identity without recursive dataclass hashing."""
    identity: list[str | int | bool] = []
    pending = [expression]
    while pending:
        current = pending.pop()
        if isinstance(current, LiteralTransfer):
            identity.extend(("literal", current.text))
        elif isinstance(current, VariableRef):
            identity.extend(("variable", current.name))
        elif isinstance(current, _SecondPassVariableRef):
            identity.extend(("second-variable", current.name))
        elif isinstance(current, StreamRef):
            identity.extend(("stream", current.scope_id))
        elif isinstance(current, ResourceRef):
            identity.extend(("resource", current.key))
        elif isinstance(current, OutsideGap):
            identity.append("outside")
        elif isinstance(current, _SecondPassConditionalAssignment):
            identity.extend(("conditional-assignment", current.name, current.assign_if_null))
            pending.append(current.operand)
        else:
            identity.extend(
                (
                    "choice" if isinstance(current, Choice) else "concat",
                    len(current.parts),
                )
            )
            pending.extend(reversed(current.parts))
    return tuple(identity)


def _flow_write_identity(
    write: _FlowWrite,
) -> tuple[
    str | int,
    bool,
    bool,
    str | None,
    int | None,
    int | None,
    bool,
    tuple[str | int | bool, ...],
]:
    """Return a recursion-safe structural identity for one flow write."""
    return (
        write.key,
        write.append,
        write.strip_trailing_newlines,
        write.read_ifs,
        write.read_target_index,
        write.read_target_count,
        write.read_raw,
        _expression_identity(write.expression),
    )


def _assignment_identity(
    assignment: _AssignmentEvidence,
) -> tuple[str, bool, bool, bool, bool, tuple[str | int | bool, ...]]:
    """Return a recursion-safe structural identity for assignment evidence."""
    return (
        assignment.name,
        assignment.append,
        assignment.conditional,
        assignment.assign_if_null,
        assignment.nameref_unset,
        _expression_identity(assignment.content),
    )


def _cap_value(value: _ContentValue, limits: TaintLimits) -> _ContentValue:
    if len(value) > limits.max_alternatives:
        raise _TaintLimitExceeded(
            GuardRefusal("taint.values.alternative-limit", "shell taint alternative limit exceeded")
        )
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
            if isinstance(current, Choice | _SecondPassConditionalAssignment):
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
    variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
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
            if isinstance(current, Choice | _SecondPassConditionalAssignment):
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


def _expression_variable_names(expression: ContentExpr) -> set[str]:
    """Return every variable name one content expression reads."""
    names: set[str] = set()
    pending = [expression]
    while pending:
        current = pending.pop()
        if isinstance(current, VariableRef | _SecondPassVariableRef):
            names.add(current.name)
            continue
        if isinstance(current, _SecondPassConditionalAssignment):
            names.add(current.name)
            pending.append(current.operand)
            continue
        if isinstance(current, Choice | Concat):
            pending.extend(current.parts)
    return names


def _cyclic_write_keys(writes: tuple[_FlowWrite, ...]) -> frozenset[str | int]:
    """Return the write keys that participate in a variable-reference cycle."""
    dependencies: dict[str | int, set[str]] = {}
    for write in writes:
        dependencies.setdefault(write.key, set()).update(
            _expression_variable_names(write.expression)
        )
    cyclic: set[str | int] = set()
    for key, direct in dependencies.items():
        reached: set[str] = set()
        pending = list(direct)
        while pending:
            name = pending.pop()
            if name in reached:
                continue
            reached.add(name)
            pending.extend(dependencies.get(name, ()))
        if key in reached:
            cyclic.add(key)
    return frozenset(cyclic)


def _solve_flow_definitions(
    definitions: _FlowDefinitions,
    *,
    limits: TaintLimits,
) -> _SolvedFlow:
    nodes, edges, entries = _definition_counts(definitions)
    if nodes > limits.max_expression_nodes:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.flow-solve.expression-node-limit",
                "shell taint expression node limit exceeded",
            )
        )
    if edges > limits.max_edges:
        raise _TaintLimitExceeded(
            GuardRefusal("taint.flow-solve.edge-limit", "shell taint edge limit exceeded")
        )
    if entries > limits.max_table_entries:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.flow-solve.table-entry-limit", "shell taint table entry limit exceeded"
            )
        )

    # Keys on a reference cycle are seeded with epsilon rather than the lattice bottom. Bottom
    # means "unreachable" and ``_compose_values`` annihilates it, so a key whose write set is
    # entirely self- or mutually-referential would never leave bottom and would read as
    # marker-free even though Bash expands the not-yet-assigned read to the empty string. Keys
    # off a cycle keep bottom, which is what the declared-forward-reference and definite-setness
    # models depend on.
    cyclic_variables = _cyclic_write_keys(definitions.variable_writes)
    variables: dict[str | int, _ContentValue] = {
        write.key: (frozenset({_EPSILON}) if write.key in cyclic_variables else frozenset())
        for write in definitions.variable_writes
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
            # This is the record-one projection site: today every write is projected as a
            # single record. Issue #121 (read-past-first-record) will need to reintroduce a
            # "project the whole write through its first record delimiter" primitive here,
            # analogous to the retired ``_first_record`` helper, once a caller can request
            # reading past the first record.
            if (
                write.read_ifs is not None
                and write.read_target_index is not None
                and write.read_target_count is not None
            ):
                value = _cap_value(
                    _project_read_value(
                        value,
                        write.read_ifs,
                        write.read_target_index,
                        write.read_target_count,
                        raw=write.read_raw,
                    ),
                    limits,
                )
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
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.flow-solve.fixed-point-limit",
                        "shell taint fixed-point update limit exceeded",
                    )
                )
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
    raise _MalformedTaintEvidence(
        GuardRefusal(
            "taint.evidence.unknown-output-node", "shell taint evidence cannot be structured"
        )
    )


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
                    raise _MalformedTaintEvidence(
                        GuardRefusal(
                            "taint.evidence.output-command-ref",
                            "shell taint evidence cannot be structured",
                        )
                    )
                refs: set[int] = set()
            elif isinstance(current, ScopeOutput):
                if current.scope_id not in stream_ids:
                    raise _MalformedTaintEvidence(
                        GuardRefusal(
                            "taint.evidence.nested-scope-output",
                            "shell taint evidence cannot be structured",
                        )
                    )
                refs = {current.scope_id} if current.scope_id in scope_ids else set()
            else:
                refs = set()
                for child in _output_children(current):
                    refs.update(state.memo[id(child)])
            state.memo[current_id] = frozenset(refs)
            continue
        if current_id in active:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.output-scope-cycle", "shell taint evidence cannot be structured"
                )
            )
        if state.remaining_nodes < 1:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.evidence.output-scope-node-limit",
                    "shell taint expression node limit exceeded",
                )
            )
        state.remaining_nodes -= 1
        active.add(current_id)
        pending.append((current, True))
        pending.extend((child, False) for child in reversed(_output_children(current)))
    return state.memo[id(output)]


def _validate_acyclic_graph(
    graph: dict[int, frozenset[int]],
    *,
    refusal: GuardRefusal,
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
                raise _MalformedTaintEvidence(refusal)
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
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.evidence.duplicate-identifier", "shell taint evidence cannot be structured"
            )
        )

    command_ids = set(command_id_values)
    scope_ids = set(scope_id_values)
    resource_ids = set(resource_id_values)
    stream_ids = set(stream_id_values)
    parent_graph: dict[int, frozenset[int]] = {}
    output_graph: dict[int, frozenset[int]] = {}
    output_state = _OutputValidationState(limits.max_expression_nodes)
    for scope in evidence.scopes:
        if scope.parent_scope_id is not None and scope.parent_scope_id not in scope_ids:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.unknown-parent-scope",
                    "shell taint evidence cannot be structured",
                )
            )
        if scope.parent_command_id is not None and scope.parent_command_id not in command_ids:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.unknown-parent-command",
                    "shell taint evidence cannot be structured",
                )
            )
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
        refusal=GuardRefusal(
            "taint.evidence.scope-parent-cycle",
            "shell taint stream scope cannot be structured",
        ),
    )
    _validate_acyclic_graph(
        output_graph,
        refusal=GuardRefusal(
            "taint.evidence.output-graph-cycle",
            "shell taint evidence cannot be structured",
        ),
    )

    for pipe in evidence.pipes:
        if pipe.producer_scope_id not in stream_ids:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.unknown-pipe-producer",
                    "shell taint evidence cannot be structured",
                )
            )
        if pipe.consumer_command_id is not None and pipe.consumer_command_id not in command_ids:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.unknown-pipe-consumer-command",
                    "shell taint evidence cannot be structured",
                )
            )
        if pipe.consumer_scope_id is not None and pipe.consumer_scope_id not in scope_ids:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.unknown-pipe-consumer-scope",
                    "shell taint evidence cannot be structured",
                )
            )
    if any(resource.scope_id not in scope_ids for resource in evidence.process_resources):
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.evidence.unknown-resource-scope", "shell taint evidence cannot be structured"
            )
        )

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
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.evidence.unknown-redirection-resource",
                "shell taint evidence cannot be structured",
            )
        )
    if any(
        port.process_resource_id is not None and port.process_resource_id not in resource_ids
        for command in evidence.commands
        for port in command.argv
    ):
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.evidence.unknown-argv-resource", "shell taint evidence cannot be structured"
            )
        )


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
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.evidence.unknown-output-input-node",
                    "shell taint evidence cannot be structured",
                )
            )
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
                raise _MalformedTaintEvidence(
                    GuardRefusal(
                        "taint.evidence.scope-input-cycle",
                        "shell taint evidence cannot be structured",
                    )
                )
            state[scope_id] = 1
            pending.append((scope_id, True))
            for dependency in reversed(parts[scope_id][1]):
                if dependency not in memo:
                    pending.append((dependency, False))
    return memo


_DescriptorBindings = dict[int, tuple[RedirectionTarget | _ImplicitPipeTarget, bool]]

# Descriptors 1 and 2 are always open in the enclosing shell, so a ``>&1``/``>&2`` source that no
# authored redirection bound is inherited stdout/stderr rather than missing evidence.
_INHERITED_OUTPUT_DESCRIPTORS = frozenset({1, 2})


def _resolve_output_descriptor_source(
    descriptor: int,
    bindings: _DescriptorBindings,
    inherited: _DescriptorBindings | None,
    guarded: frozenset[int],
) -> tuple[RedirectionTarget | _ImplicitPipeTarget, bool]:
    """Chase a ``>&N`` source through any deferred duplication hops to its concrete target.

    A duplication that does not itself route stdout (for example ``exec 3>&4`` inside a body
    that also has ``>&3``) is left unresolved in ``bindings`` rather than collapsed to a dynamic
    placeholder when it is created, so this walk -- not the point a duplication is bound -- is
    where the guard has to apply. Resolving eagerly at the binding site let an intermediate
    duplication collapse into a placeholder before the guard could see it, and a later
    duplication of that placeholder skipped the guard entirely because its source was already
    "found".

    Args:
        descriptor: The descriptor a ``>&N`` redirection names as its source.
        bindings: The bindings this command or compound has established so far.
        inherited: Descriptor bindings an enclosing compound installed, or ``None``.
        guarded: Descriptors some other command or compound in this body binds, directly or
            through a duplication chain reaching one.

    Returns:
        The concrete or dynamic target the chain resolves to.

    Raises:
        _TaintLimitExceeded: If the chain reaches an unresolved reference to a guarded
            descriptor, or the chain does not terminate within the descriptors visited so far.
    """
    visited: set[int] = set()
    current = descriptor
    while True:
        if current in visited:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.descriptor.output-alias-cycle",
                    "shell descriptor source cannot be represented",
                )
            )
        visited.add(current)
        found = bindings.get(current)
        if found is None and inherited is not None:
            found = inherited.get(current)
        if found is None:
            if current in guarded and current not in _INHERITED_OUTPUT_DESCRIPTORS:
                # A bare ``exec 3> f`` rebinds the descriptor for the rest of the shell, which
                # the per-command redirection evidence cannot carry. Resolving the source to a
                # dynamic target here would discard this command's stdout instead of routing
                # it, so an authored marker could reach a file the scan reads as unwritten.
                # A descriptor no part of this body binds is a runtime error in Bash rather
                # than missing evidence, so it keeps the existing dynamic-target behavior.
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.descriptor.output-alias-unresolved",
                        "shell descriptor source cannot be represented",
                    )
                )
            return (DynamicResourceTarget(), False)
        target, append = found
        if isinstance(target, DescriptorTarget):
            current = target.descriptor
            continue
        return (target, append)


def _output_bindings(
    events: tuple[_RedirectionEvent, ...],
    *,
    implicit_pipe: bool = False,
    inherited: _DescriptorBindings | None = None,
    guarded: frozenset[int] = frozenset(),
) -> _DescriptorBindings:
    """Replay output descriptor mutations left-to-right.

    Args:
        events: The redirection events attached to one command or compound.
        implicit_pipe: Whether descriptor 1 starts bound to this stage's pipe endpoint.
        inherited: Descriptor bindings an enclosing compound installed, consulted only to
            resolve a ``>&N`` source this event set does not bind itself.
        guarded: Descriptors some other command or compound in this body binds. A stdout write
            whose source is one of these but is not visible here has to fail closed.

    Returns:
        The final binding per output descriptor. A descriptor this event set duplicates without
        ever routing it to stdout may still hold an unresolved ``DescriptorTarget`` reference;
        see ``_resolve_output_descriptor_source``.

    Raises:
        _TaintLimitExceeded: If a write routes stdout through a guarded descriptor whose binding
            the evidence cannot name.
    """
    bindings: _DescriptorBindings = {1: (_ImplicitPipeTarget(), False)} if implicit_pipe else {}
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _OUTPUT_REDIRECTION_OPERATORS:
            continue
        if isinstance(event.target, DescriptorTarget):
            routes_stdout = (
                event.descriptor == 1 or event.operator in _COMBINED_OUTPUT_REDIRECTION_OPERATORS
            )
            if routes_stdout:
                binding = _resolve_output_descriptor_source(
                    event.target.descriptor, bindings, inherited, guarded
                )
            else:
                # Defer resolution: a duplication that never routes to stdout here might still
                # be dereferenced by a later duplication that does. Storing the raw reference
                # keeps that later lookup subject to the same guard instead of inheriting an
                # already-collapsed placeholder.
                binding = (event.target, event.operator in _APPEND_REDIRECTION_OPERATORS)
        else:
            binding = (event.target, event.operator in _APPEND_REDIRECTION_OPERATORS)
        if event.operator in _COMBINED_OUTPUT_REDIRECTION_OPERATORS:
            bindings[1] = binding
            bindings[2] = bindings[1]
        else:
            bindings[event.descriptor] = binding
    return bindings


def _guarded_output_descriptors(evidence: _ShellTaintEvidence) -> frozenset[int]:
    """Return the non-standard output descriptors some command or compound in this body binds.

    A descriptor that only ever appears as a duplication of another (``exec 3>&4``) is guarded
    transitively when the descriptor it duplicates is, so a duplication chain cannot walk an
    unresolved reference out from under the guard by hopping through an intermediate descriptor
    ``_guarded_output_descriptors`` previously did not track.

    Args:
        evidence: The typed shell execution evidence for one run body.

    Returns:
        The descriptors a ``>&N`` source may legitimately refer to, so an unresolved reference to
        one of them is missing evidence rather than a Bash runtime error.
    """
    direct: set[int] = set()
    dependents: dict[int, list[int]] = {}
    for events in (
        *(command.redirections for command in evidence.commands),
        *(scope.redirections for scope in evidence.scopes),
    ):
        for event in events:
            if (
                event.descriptor is None
                or event.descriptor in _INHERITED_OUTPUT_DESCRIPTORS
                or event.operator not in _OUTPUT_REDIRECTION_OPERATORS
            ):
                continue
            if isinstance(event.target, DescriptorTarget):
                dependents.setdefault(event.target.descriptor, []).append(event.descriptor)
            else:
                direct.add(event.descriptor)
    guarded = set(direct)
    pending = list(direct)
    while pending:
        current = pending.pop()
        for dependent in dependents.get(current, ()):
            if dependent not in guarded:
                guarded.add(dependent)
                pending.append(dependent)
    return frozenset(guarded)


def _scope_inherited_bindings(
    evidence: _ShellTaintEvidence,
) -> dict[int, _DescriptorBindings]:
    """Return the descriptor bindings each structured scope installs for the commands inside it.

    Args:
        evidence: The typed shell execution evidence for one run body.

    Returns:
        A mapping from scope id to the bindings visible inside that scope, with an inner
        compound's redirections overriding an outer one's.
    """
    scopes = {scope.scope_id: scope for scope in evidence.scopes}
    resolved: dict[int, _DescriptorBindings] = {}
    # An explicit stack keeps a deep or reverse-ordered scope chain within Python's recursion
    # budget; ``state`` marks a scope as in-progress so a malformed parent cycle stops instead of
    # looping forever.
    state: dict[int, int] = {}
    for root in scopes:
        if root in resolved:
            continue
        pending = [(root, False)]
        while pending:
            scope_id, expanded = pending.pop()
            if scope_id in resolved:
                continue
            scope = scopes[scope_id]
            parent_id = scope.parent_scope_id
            parent = resolved.get(parent_id, {}) if parent_id is not None else {}
            resolved_parent = (
                parent_id is None or parent_id not in scopes or state.get(parent_id) == 1
            )
            if expanded or resolved_parent:
                bindings = dict(parent)
                bindings.update(_output_bindings(scope.redirections, inherited=parent))
                resolved[scope_id] = bindings
                state[scope_id] = 2
                continue
            state[scope_id] = 1
            pending.append((scope_id, True))
            pending.append((parent_id, False))
    return resolved


_InputDescriptorBindings = dict[int, "ContentExpr | DescriptorTarget"]

# Descriptor 0 is always open in the enclosing shell, so a ``<&0`` source that no authored
# redirection bound is inherited stdin rather than missing evidence.
_INHERITED_INPUT_DESCRIPTORS = frozenset({0})


@dataclass(frozen=True, slots=True)
class _InputDescriptorContext:
    """Bundled state for resolving ``<&N`` sources across the scopes of one evidence body."""

    scope_bindings: dict[int, _InputDescriptorBindings]
    guarded: frozenset[int]


def _guarded_input_descriptors(evidence: _ShellTaintEvidence) -> frozenset[int]:
    """Return the non-standard input descriptors some command or compound in this body binds.

    A descriptor that only ever appears as a duplication of another (``exec 3<&4``) is guarded
    transitively when the descriptor it duplicates is, mirroring the output-side treatment in
    ``_guarded_output_descriptors``.

    Args:
        evidence: The typed shell execution evidence for one run body.

    Returns:
        The descriptors a ``<&N`` source may legitimately refer to, so an unresolved reference
        to one of them is missing evidence rather than a Bash runtime error.
    """
    direct: set[int] = set()
    dependents: dict[int, list[int]] = {}
    for events in (
        *(command.redirections for command in evidence.commands),
        *(scope.redirections for scope in evidence.scopes),
    ):
        for event in events:
            if (
                event.descriptor is None
                or event.descriptor in _INHERITED_INPUT_DESCRIPTORS
                or event.operator not in _INPUT_REDIRECTION_OPERATORS
            ):
                continue
            if isinstance(event.target, DescriptorTarget):
                dependents.setdefault(event.target.descriptor, []).append(event.descriptor)
            else:
                direct.add(event.descriptor)
    guarded = set(direct)
    pending = list(direct)
    while pending:
        current = pending.pop()
        for dependent in dependents.get(current, ()):
            if dependent not in guarded:
                guarded.add(dependent)
                pending.append(dependent)
    return frozenset(guarded)


def _resolve_input_descriptor_source(
    descriptor: int,
    bindings: _InputDescriptorBindings,
    inherited: _InputDescriptorBindings | None,
    guarded: frozenset[int],
) -> ContentExpr:
    """Chase a ``<&N`` source through any deferred duplication hops to its content.

    Mirrors ``_resolve_output_descriptor_source``: a duplication that does not itself route to
    descriptor zero is left unresolved rather than collapsed into a certifying ``OutsideGap()``
    when it is created, so this walk is where the guard applies.

    Args:
        descriptor: The descriptor a ``<&N`` redirection names as its source.
        bindings: The bindings this command or compound has established so far.
        inherited: Descriptor bindings an enclosing compound installed, or ``None``.
        guarded: Descriptors some other command or compound in this body binds, directly or
            through a duplication chain reaching one.

    Returns:
        The content expression the chain resolves to.

    Raises:
        _TaintLimitExceeded: If the chain reaches an unresolved reference to a guarded
            descriptor, or the chain does not terminate within the descriptors visited so far.
    """
    visited: set[int] = set()
    current = descriptor
    while True:
        if current in visited:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.descriptor.input-alias-cycle",
                    "shell descriptor source cannot be represented",
                )
            )
        visited.add(current)
        found = bindings.get(current)
        if found is None and inherited is not None:
            found = inherited.get(current)
        if found is None:
            if current in guarded and current not in _INHERITED_INPUT_DESCRIPTORS:
                # ``exec 3< f`` rebinds the descriptor for the rest of the shell, which the
                # per-command redirection evidence cannot carry. Substituting a certifying
                # ``OutsideGap()`` here would discard this read instead of routing it, so an
                # authored marker could reach a sink through a source the scan reads as absent.
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.descriptor.input-alias-unresolved",
                        "shell descriptor source cannot be represented",
                    )
                )
            return OutsideGap()
        if isinstance(found, DescriptorTarget):
            current = found.descriptor
            continue
        return found


def _input_redirection_expression(
    event: _RedirectionEvent,
    bindings: _InputDescriptorBindings,
    inherited: _InputDescriptorBindings | None,
    process_resources: dict[int, _ProcessResourceEvidence],
    guarded: frozenset[int],
) -> ContentExpr | DescriptorTarget:
    """Return one input redirection event's binding, deferring an unrouted duplication."""
    expression: ContentExpr | DescriptorTarget
    if isinstance(event.target, StaticResourceTarget):
        expression = ResourceRef(event.target.key)
    elif isinstance(event.target, ContentTarget):
        expression = event.target.content
    elif isinstance(event.target, ProcessResourceTarget):
        expression = _process_resource_input(event.target.resource_id, process_resources)
    elif isinstance(event.target, NullTarget):
        expression = LiteralTransfer("")
    elif isinstance(event.target, DescriptorTarget):
        if event.descriptor == 0:
            expression = _resolve_input_descriptor_source(
                event.target.descriptor, bindings, inherited, guarded
            )
        else:
            # Defer resolution: see ``_output_bindings`` for why collapsing this now would let
            # a later duplication skip the guard.
            expression = event.target
    else:
        # A dynamic or otherwise unrecognized target is external content this analysis cannot
        # name.
        expression = OutsideGap()
    return expression


def _replay_input_bindings(
    events: tuple[_RedirectionEvent, ...],
    initial: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
    *,
    inherited: _InputDescriptorBindings | None = None,
    guarded: frozenset[int] = frozenset(),
) -> ContentExpr:
    """Replay ordered stdin bindings over one inherited descriptor-zero expression."""
    bindings: _InputDescriptorBindings = {0: initial}
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _INPUT_REDIRECTION_OPERATORS:
            continue
        bindings[event.descriptor] = _input_redirection_expression(
            event, bindings, inherited, process_resources, guarded
        )
    result = bindings[0]
    # Descriptor zero is always routed through the guarded chase above, so it can never be left
    # as a deferred reference here.
    return result if not isinstance(result, DescriptorTarget) else OutsideGap()


def _input_descriptor_bindings(
    events: tuple[_RedirectionEvent, ...],
    process_resources: dict[int, _ProcessResourceEvidence],
    *,
    inherited: _InputDescriptorBindings | None = None,
) -> _InputDescriptorBindings:
    """Replay input descriptor mutations left-to-right without a pipe-provided seed.

    Builds the descriptor table one structured scope installs so a nested scope's own ``<&N``
    replay can resolve a source its local redirections do not bind, mirroring
    ``_scope_inherited_bindings`` on the output side.
    """
    bindings: _InputDescriptorBindings = {}
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _INPUT_REDIRECTION_OPERATORS:
            continue
        bindings[event.descriptor] = _input_redirection_expression(
            event, bindings, inherited, process_resources, frozenset()
        )
    return bindings


def _scope_inherited_input_bindings(
    evidence: _ShellTaintEvidence,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> dict[int, _InputDescriptorBindings]:
    """Return the input descriptor bindings each structured scope installs for its contents.

    Mirrors ``_scope_inherited_bindings`` on the output side.

    Args:
        evidence: The typed shell execution evidence for one run body.
        process_resources: Process-substitution resources referenced by descriptor.

    Returns:
        A mapping from scope id to the bindings visible inside that scope, with an inner
        compound's redirections overriding an outer one's.
    """
    scopes = {scope.scope_id: scope for scope in evidence.scopes}
    resolved: dict[int, _InputDescriptorBindings] = {}
    state: dict[int, int] = {}
    for root in scopes:
        if root in resolved:
            continue
        pending = [(root, False)]
        while pending:
            scope_id, expanded = pending.pop()
            if scope_id in resolved:
                continue
            scope = scopes[scope_id]
            parent_id = scope.parent_scope_id
            parent = resolved.get(parent_id, {}) if parent_id is not None else {}
            resolved_parent = (
                parent_id is None or parent_id not in scopes or state.get(parent_id) == 1
            )
            if expanded or resolved_parent:
                bindings: _InputDescriptorBindings = dict(parent)
                bindings.update(
                    _input_descriptor_bindings(
                        scope.redirections, process_resources, inherited=parent
                    )
                )
                resolved[scope_id] = bindings
                state[scope_id] = 2
                continue
            state[scope_id] = 1
            pending.append((scope_id, True))
            pending.append((parent_id, False))
    return resolved


def _pipe_source(
    pipe: _PipeEvidence,
    commands: dict[int, _CommandEvidence],
    scopes: dict[int, _StreamScopeEvidence],
) -> ContentExpr:
    """Return what a pipe receives after explicit and implicit descriptor redirects."""
    scope_id = pipe.producer_scope_id
    command = next(
        (candidate for candidate in commands.values() if candidate.output_scope_id == scope_id),
        None,
    )
    scope = scopes.get(scope_id)
    events = command.redirections if command is not None else scope.redirections if scope else ()
    bindings = _output_bindings(events, implicit_pipe=True)
    if pipe.includes_stderr:
        # Bash applies ``|&``'s implicit ``2>&1`` after authored redirections.
        bindings[2] = bindings[1]
    descriptors = (1, 2) if pipe.includes_stderr else (1,)
    return (
        StreamRef(scope_id)
        if any(
            isinstance(bindings.get(descriptor, (None, False))[0], _ImplicitPipeTarget)
            for descriptor in descriptors
        )
        else LiteralTransfer("")
    )


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
    descriptors: _InputDescriptorContext,
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
                raise _MalformedTaintEvidence(
                    GuardRefusal(
                        "taint.evidence.scope-input-resolution-cycle",
                        "shell taint evidence cannot be structured",
                    )
                )
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
            parent_id = scope.parent_scope_id
            inherited = _replay_input_bindings(
                scope.redirections,
                direct_inputs.get(current, inherited),
                resources,
                inherited=descriptors.scope_bindings.get(parent_id, {})
                if parent_id is not None
                else {},
                guarded=descriptors.guarded,
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


def _pipe_inputs(
    evidence: _ShellTaintEvidence,
    descriptors: _InputDescriptorContext,
) -> dict[int, ContentExpr]:
    """Return stdin expressions from pipes and output process resources through scope entries."""
    commands = {command.command_id: command for command in evidence.commands}
    scopes = {scope.scope_id: scope for scope in evidence.scopes}
    command_scopes = {command.output_scope_id: command.command_id for command in evidence.commands}
    command_inputs: dict[int, ContentExpr] = {}
    scope_inputs: dict[int, ContentExpr] = {}
    for pipe in evidence.pipes:
        expression = _pipe_source(pipe, commands, scopes)
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
        _resolved_scope_inputs(
            scopes,
            scope_inputs,
            resources,
            active_scopes,
            descriptors,
        ),
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
    command_scope_paths: dict[int, tuple[int, ...]],
    descriptors: _InputDescriptorContext,
) -> ContentExpr:
    """Replay ordered input descriptor bindings and return descriptor zero.

    Args:
        command: The command whose stdin expression this replay resolves.
        pipe_inputs: Stdin expressions from pipes and output process resources, by command id.
        process_resources: Process-substitution resources referenced by descriptor.
        command_scope_paths: Each command's structured scope ancestry, outermost to innermost.
        descriptors: Input descriptor bindings and guarded descriptors for this evidence body.
    """
    enclosing = command_scope_paths.get(command.command_id, ())
    return _replay_input_bindings(
        command.redirections,
        pipe_inputs.get(command.command_id, OutsideGap()),
        process_resources,
        inherited=descriptors.scope_bindings.get(enclosing[-1], {}) if enclosing else {},
        guarded=descriptors.guarded,
    )


def _strip_one_trailing_newline(expression: ContentExpr) -> ContentExpr:
    """Remove the record delimiter consumed by one bounded ``read``."""
    if isinstance(expression, LiteralTransfer):
        return LiteralTransfer(
            expression.text[:-1] if expression.text.endswith("\n") else expression.text
        )
    if isinstance(expression, Concat) and expression.parts:
        return concat(
            *expression.parts[:-1],
            _strip_one_trailing_newline(expression.parts[-1]),
        )
    return expression


def _exact_content_literal(
    expression: ContentExpr,
    values: Mapping[str, str],
    limits: TaintLimits,
) -> str | None:
    """Resolve one bounded content expression against exact scalar values."""
    memo: dict[int, str | None] = {}
    pending = [(expression, False)]
    while pending:
        current, expanded = pending.pop()
        current_id = id(current)
        if current_id in memo:
            continue
        if not expanded:
            pending.append((current, True))
            if isinstance(current, Choice | Concat):
                pending.extend((part, False) for part in reversed(current.parts))
            continue
        if isinstance(current, LiteralTransfer):
            value: str | None = current.text
        elif isinstance(current, VariableRef):
            value = values.get(_unscoped_variable_name(current.name))
        elif isinstance(current, Concat):
            parts = tuple(memo.get(id(part)) for part in current.parts)
            value = (
                None
                if any(part is None for part in parts)
                else "".join(part for part in parts if part is not None)
            )
        elif isinstance(current, Choice):
            alternatives = {memo.get(id(part)) for part in current.parts}
            value = alternatives.pop() if len(alternatives) == 1 else None
        else:
            value = None
        if value is not None and len(value) > limits.max_exact_value_chars:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.exact-value.length-limit",
                    "shell taint exact value length limit exceeded",
                )
            )
        memo[current_id] = value
    return memo[id(expression)]


def _read_literal_fields(  # noqa: PLR0912
    value: str,
    ifs: str,
    target_count: int,
    *,
    raw: bool,
) -> tuple[str, ...] | None:
    """Split one exact read record across scalar targets with bounded IFS semantics."""
    if target_count < 1:
        return None
    record: list[tuple[str, bool]] = []
    cursor = 0
    while cursor < len(value):
        character = value[cursor]
        if not raw and character == "\\" and cursor + 1 < len(value):
            escaped = value[cursor + 1]
            cursor += 2
            if escaped == "\n":
                continue
            record.append((escaped, True))
            continue
        if character == "\n":
            break
        record.append((character, False))
        cursor += 1
    if not ifs:
        return (
            "".join(character for character, _escaped in record),
            *("" for _ in range(target_count - 1)),
        )
    ifs_characters = frozenset(ifs)
    ifs_whitespace = frozenset(character for character in ifs if character in " \t\n")
    cursor = 0

    def is_delimiter(index: int) -> bool:
        character, escaped = record[index]
        return not escaped and character in ifs_characters

    def is_ifs_whitespace(index: int) -> bool:
        character, escaped = record[index]
        return not escaped and character in ifs_whitespace

    def skip_ifs_whitespace(start: int) -> int:
        while start < len(record) and is_ifs_whitespace(start):
            start += 1
        return start

    assigned: list[str] = []
    for _index in range(target_count - 1):
        cursor = skip_ifs_whitespace(cursor)
        start = cursor
        while cursor < len(record) and not is_delimiter(cursor):
            cursor += 1
        assigned.append("".join(character for character, _escaped in record[start:cursor]))
        if cursor >= len(record):
            continue
        if is_ifs_whitespace(cursor):
            cursor = skip_ifs_whitespace(cursor)
            if (
                cursor < len(record)
                and is_delimiter(cursor)
                and record[cursor][0] not in ifs_whitespace
            ):
                cursor += 1
                cursor = skip_ifs_whitespace(cursor)
        else:
            cursor += 1
            cursor = skip_ifs_whitespace(cursor)
    cursor = skip_ifs_whitespace(cursor)
    remainder = record[cursor:]
    while remainder and (not remainder[-1][1] and remainder[-1][0] in ifs_whitespace):
        remainder.pop()
    assigned.append("".join(character for character, _escaped in remainder))
    return tuple(assigned)


def _resolve_builtin_writer_evidence(  # noqa: PLR0915
    evidence: _ShellTaintEvidence,
    limits: TaintLimits,
) -> _ShellTaintEvidence:
    """Attach finalized stdin and exact bounded read-field projections."""
    process_resources = {resource.resource_id: resource for resource in evidence.process_resources}
    command_scope_paths = _command_scope_paths(evidence)
    input_descriptors = _InputDescriptorContext(
        scope_bindings=_scope_inherited_input_bindings(evidence, process_resources),
        guarded=_guarded_input_descriptors(evidence),
    )
    pipe_inputs = _pipe_inputs(evidence, input_descriptors)
    command_environments, environment_parents, _lastpipe = _execution_environment_ids(evidence)
    exact_values_by_environment: dict[tuple[int | None, int], dict[str, str]] = {}
    unknown_values_by_environment: dict[tuple[int | None, int], set[str]] = {}
    commands: list[_CommandEvidence] = []

    def exact_values_for(context: int | None, environment: int) -> dict[str, str]:
        key = (context, environment)
        cached = exact_values_by_environment.get(key)
        if cached is not None:
            return cached
        parent = environment_parents.get(environment)
        inherited = dict(exact_values_for(context, parent)) if parent is not None else {}
        exact_values_by_environment[key] = inherited
        return inherited

    def unknown_values_for(context: int | None, environment: int) -> set[str]:
        key = (context, environment)
        cached = unknown_values_by_environment.get(key)
        if cached is not None:
            return cached
        parent = environment_parents.get(environment)
        inherited = set(unknown_values_for(context, parent)) if parent is not None else set()
        unknown_values_by_environment[key] = inherited
        return inherited

    def apply_exact(
        assignment: _AssignmentEvidence,
        values: dict[str, str],
        unknown_values: set[str],
        *,
        conditional: bool,
    ) -> None:
        name = _unscoped_variable_name(assignment.name)
        if conditional:
            values.pop(name, None)
            unknown_values.add(name)
            return
        value = _exact_content_literal(assignment.content, values, limits)
        if value is None:
            values.pop(name, None)
            unknown_values.add(name)
        elif assignment.append:
            prior = values.get(name)
            if prior is None:
                values.pop(name, None)
                unknown_values.add(name)
            else:
                values[name] = prior + value
                unknown_values.discard(name)
        else:
            values[name] = value
            unknown_values.discard(name)

    for command in evidence.commands:
        context = command.function_context_id
        environment = command_environments[command.command_id]
        exact_values = exact_values_for(context, environment)
        unknown_values = unknown_values_for(context, environment)
        command_values = dict(exact_values)
        command_unknown_values = set(unknown_values)
        for assignment in command.definite_assignments:
            apply_exact(
                assignment,
                command_values,
                command_unknown_values,
                conditional=False,
            )
        stdin = _strip_one_trailing_newline(
            _input_expression(
                command,
                pipe_inputs,
                process_resources,
                command_scope_paths,
                input_descriptors,
            )
        )
        exact_stdin = _exact_content_literal(stdin, command_values, limits)
        read_target_count = next(
            (
                assignment.read_target_count
                for assignment in command.builtin_assignments
                if assignment.from_stdin
            ),
            None,
        )
        read_raw = next(
            (
                assignment.read_raw
                for assignment in command.builtin_assignments
                if assignment.from_stdin
            ),
            False,
        )
        read_ifs_unknown = read_target_count is not None and "IFS" in command_unknown_values
        read_fields = (
            _read_literal_fields(
                exact_stdin,
                command_values.get("IFS", " \t\n"),
                read_target_count,
                raw=read_raw,
            )
            if exact_stdin is not None and read_target_count is not None and not read_ifs_unknown
            else None
        )

        def route(
            assignment: _AssignmentEvidence,
            *,
            input_content: ContentExpr = stdin,
            projected_fields: tuple[str, ...] | None = read_fields,
            ifs_value: str = command_values.get("IFS", " \t\n"),
        ) -> _AssignmentEvidence:
            content = (
                LiteralTransfer(projected_fields[assignment.read_target_index])
                if assignment.from_stdin
                and projected_fields is not None
                and assignment.read_target_index is not None
                else input_content
                if assignment.from_stdin
                else assignment.content
            )
            deferred_projection = assignment.from_stdin and projected_fields is None
            return replace(
                assignment,
                content=content,
                from_stdin=False,
                read_target_index=(assignment.read_target_index if deferred_projection else None),
                read_target_count=(assignment.read_target_count if deferred_projection else None),
                read_raw=assignment.read_raw if deferred_projection else False,
                read_ifs=ifs_value if deferred_projection else None,
            )

        routed = replace(
            command,
            assignments=tuple(route(assignment) for assignment in command.assignments),
            definite_assignments=tuple(
                route(assignment) for assignment in command.definite_assignments
            ),
            builtin_assignments=tuple(
                route(assignment) for assignment in command.builtin_assignments
            ),
            unsupported_builtin_write=(command.unsupported_builtin_write or read_ifs_unknown),
        )
        commands.append(routed)
        # ``if``/``elif``/``else`` branch uncertainty lives in ``execution_status``, not in
        # ``conditionally_executed``. Without it an assignment inside an untaken branch replaced
        # the live value, and the exact ``read`` projection below substituted that stale text as a
        # literal.
        conditional = command.conditionally_executed or command.execution_status is not True
        assignment_only = not command.argv or command.executable.argv_index is None
        if assignment_only:
            for assignment in routed.definite_assignments:
                apply_exact(
                    assignment,
                    exact_values,
                    unknown_values,
                    conditional=conditional,
                )
        for assignment in routed.builtin_assignments:
            apply_exact(
                assignment,
                exact_values,
                unknown_values,
                conditional=conditional,
            )
        unset_names, unknown_unset = _unset_action(command)
        for name in unset_names:
            exact_values.pop(name, None)
            if conditional:
                unknown_values.add(name)
            else:
                unknown_values.discard(name)
        if unknown_unset:
            exact_values.clear()
            unknown_values.add("IFS")
        if routed.unknown_builtin_content is not None:
            exact_values.clear()
            # The dynamic write may target IFS, so later reads must not assume the default.
            unknown_values.add("IFS")
    return replace(evidence, commands=tuple(commands))


def _route_runtime_nameref_writes(  # noqa: PLR0915
    evidence: _ShellTaintEvidence,
    limits: TaintLimits,
) -> _ShellTaintEvidence:
    """Route contextualized writes through Bash namerefs in execution order."""
    command_environments, environment_parents, _lastpipe = _execution_environment_ids(evidence)
    aliases_by_environment: dict[int, dict[str, str | None]] = {}
    commands: list[_CommandEvidence] = []
    saw_nameref = False

    def aliases_for(environment: int) -> dict[str, str | None]:
        cached = aliases_by_environment.get(environment)
        if cached is not None:
            return cached
        parent = environment_parents.get(environment)
        inherited = dict(aliases_for(parent)) if parent is not None else {}
        aliases_by_environment[environment] = inherited
        return inherited

    def resolve_alias(
        name: str,
        aliases: Mapping[str, str | None],
    ) -> tuple[str | None, bool]:
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current in aliases:
            if current in seen:
                return current, False
            seen.add(current)
            current = aliases[current]
        return current, True

    def scoped_binding_target(name: str, target: str, environment: int) -> str:
        target_environment = _scoped_variable_environment(name)
        return _scoped_variable_name(
            target_environment if target_environment is not None else environment,
            target,
        )

    for command in evidence.commands:
        environment = command_environments[command.command_id]
        aliases = aliases_for(environment)
        unsupported = command.unsupported_builtin_write

        def route_assignment(  # noqa: PLR0911, PLR0912
            assignment: _AssignmentEvidence,
            *,
            activate_declaration: bool = True,
            environment_id: int = environment,
            alias_map: dict[str, str | None] = aliases,
        ) -> _AssignmentEvidence:
            nonlocal saw_nameref, unsupported
            if assignment.nameref_unset:
                saw_nameref = True
                alias_names = {
                    assignment.name,
                    scoped_binding_target(
                        assignment.name,
                        _unscoped_variable_name(assignment.name),
                        environment_id,
                    ),
                }
                for alias_name in alias_names:
                    alias_map.pop(alias_name, None)
                return assignment
            declaration_target = assignment.nameref_target
            if declaration_target is not None:
                saw_nameref = True
                if not activate_declaration:
                    return assignment
                alias_names = {
                    assignment.name,
                    scoped_binding_target(
                        assignment.name,
                        _unscoped_variable_name(assignment.name),
                        environment_id,
                    ),
                }
                if not declaration_target:
                    for alias_name in alias_names:
                        alias_map[alias_name] = None
                    return assignment
                target = (
                    assignment.content.name
                    if isinstance(assignment.content, VariableRef)
                    else declaration_target
                )
                resolved, valid = resolve_alias(target, alias_map)
                if not valid or resolved is None or resolved in alias_names:
                    unsupported = True
                    for alias_name in alias_names:
                        alias_map.pop(alias_name, None)
                    return assignment
                for alias_name in alias_names:
                    alias_map[alias_name] = resolved
                return assignment

            target, valid = resolve_alias(assignment.name, alias_map)
            if not valid:
                unsupported = True
                return assignment
            if assignment.name not in alias_map:
                return assignment
            if target is None:
                binding = _exact_content_literal(assignment.content, {}, limits)
                if binding is None or not _static_variable_name(binding):
                    unsupported = True
                    return assignment
                target = scoped_binding_target(assignment.name, binding, environment_id)
                alias_map[assignment.name] = target
                scoped_alias = scoped_binding_target(
                    assignment.name,
                    _unscoped_variable_name(assignment.name),
                    environment_id,
                )
                if scoped_alias in alias_map:
                    alias_map[scoped_alias] = target
                return replace(
                    assignment,
                    content=VariableRef(target),
                    nameref_target=binding,
                )
            return replace(
                assignment,
                name=_scoped_variable_name(
                    environment_id,
                    _unscoped_variable_name(target),
                ),
            )

        routed_definite = tuple(
            route_assignment(assignment) for assignment in command.definite_assignments
        )
        routed_by_identity = {
            (
                _assignment_identity(original),
                original.nameref_target,
            ): routed
            for original, routed in zip(
                command.definite_assignments,
                routed_definite,
                strict=True,
            )
        }
        routed_assignments_list: list[_AssignmentEvidence] = []
        for assignment in command.assignments:
            routed = routed_by_identity.get(
                (_assignment_identity(assignment), assignment.nameref_target)
            )
            routed_assignments_list.append(
                routed if routed is not None else route_assignment(assignment)
            )
        routed_assignments = tuple(routed_assignments_list)
        # ``-g`` makes a nameref declaration MORE visible, not less: it outlives the function
        # body. Suppressing alias registration for it left a later ``ref=...`` write recorded
        # against ``ref`` rather than the aliased target, so
        # ``f() { declare -g -n ref=cmd; ref="$A$B"; }; f; $cmd`` certified while the spelling
        # without ``-g`` refused. The declaration is registered in the declaring environment;
        # its persistence past the function's return stays the narrower disclosed gap.
        activate_builtin_declarations = True
        routed_builtin = tuple(
            routed
            for assignment in command.builtin_assignments
            for routed in (
                route_assignment(
                    assignment,
                    activate_declaration=activate_builtin_declarations,
                ),
            )
            if not routed.nameref_unset
        )
        eval_assignments, _eval_unsets = _static_eval_mutations(command, limits=limits)
        for mutation in eval_assignments:
            if mutation.assignment.nameref_target is not None or mutation.assignment.nameref_unset:
                route_assignment(mutation.assignment)
        routed_effects_list: list[_AssignmentEvidence] = []
        for assignment in command.function_effect_assignments:
            if assignment.nameref_unset:
                saw_nameref = True
                name = assignment.name
                aliases.pop(name, None)
                aliases.pop(
                    scoped_binding_target(
                        name,
                        _unscoped_variable_name(name),
                        environment,
                    ),
                    None,
                )
                continue
            routed_effects_list.append(route_assignment(assignment))
        routed_effects = tuple(routed_effects_list)
        nameref_unsets, unknown_nameref_unset = _unset_nameref_action(command)
        if unknown_nameref_unset:
            unsupported = True
        if nameref_unsets:
            saw_nameref = True
        for name in nameref_unsets:
            aliases.pop(name, None)
            aliases.pop(
                scoped_binding_target(name, name, environment),
                None,
            )
        commands.append(
            replace(
                command,
                assignments=routed_assignments,
                definite_assignments=routed_definite,
                builtin_assignments=routed_builtin,
                function_effect_assignments=routed_effects,
                unsupported_builtin_write=unsupported,
            )
        )
    if saw_nameref:
        commands = list(
            _refresh_runtime_eval_programs(
                tuple(commands), command_environments, environment_parents, limits
            )
        )
    return replace(evidence, commands=tuple(commands))


def _refresh_runtime_eval_programs(
    commands: tuple[_CommandEvidence, ...],
    command_environments: Mapping[int, int],
    environment_parents: Mapping[int, int | None],
    limits: TaintLimits,
) -> tuple[_CommandEvidence, ...]:
    """Replay routed exact writes before refreshing eval programs."""
    values_by_environment: dict[tuple[int | None, int], dict[str, str]] = {}
    refreshed: list[_CommandEvidence] = []

    def values_for(context: int | None, environment: int) -> dict[str, str]:
        key = (context, environment)
        cached = values_by_environment.get(key)
        if cached is not None:
            return cached
        parent = environment_parents.get(environment)
        inherited = dict(values_for(context, parent)) if parent is not None else {}
        values_by_environment[key] = inherited
        return inherited

    def apply_exact(
        assignment: _AssignmentEvidence,
        values: dict[str, str],
        *,
        conditional: bool,
    ) -> None:
        name = _unscoped_variable_name(assignment.name)
        if conditional:
            values.pop(name, None)
            return
        value = _exact_content_literal(assignment.content, values, limits)
        if value is None:
            values.pop(name, None)
        elif assignment.append:
            prior = values.get(name)
            if prior is None:
                values.pop(name, None)
            else:
                values[name] = prior + value
        else:
            values[name] = value

    for command in commands:
        values = values_for(
            command.function_context_id,
            command_environments[command.command_id],
        )
        # Bash expands every word of a command before performing that command's own assignment
        # prefix, so ``C=true eval "$C"`` runs the old ``C``. Expanding with the post-assignment
        # values resolved the payload to the new one and certified the marker the shell still ran.
        # A standalone-assignment command has no eval candidate, so this map is the right one for
        # both shapes.
        programs = {
            program
            for executable in _builtin_eval_candidates(command)
            for program in (
                _exact_content_literal(
                    _eval_arguments_raw(command, executable),
                    values,
                    limits,
                ),
            )
            if program is not None
        }
        resolved_program = next(iter(programs)) if len(programs) == 1 else None
        refreshed_command = replace(
            command,
            resolved_eval_program=resolved_program,
            resolved_eval_programs=(),
            runtime_eval_program_authoritative=resolved_program is not None,
        )
        refreshed.append(refreshed_command)

        # ``conditionally_executed`` only covers ``&&``/``||`` operands, ``case`` bodies and
        # function bodies. Branch uncertainty from ``if``/``elif``/``else`` lives in
        # ``execution_status``, which ``_contextualize_evidence`` already consults, so a write
        # inside an untaken branch must not be replayed as definite here either.
        conditional = (
            command.function_effect_conditional
            if command.function_context_id is not None
            else command.conditionally_executed
        ) or command.execution_status is not True
        assignment_only = not command.argv or command.executable.argv_index is None
        if assignment_only:
            for assignment in command.definite_assignments:
                apply_exact(assignment, values, conditional=conditional)
        for assignment in (
            *command.builtin_assignments,
            *command.function_effect_assignments,
        ):
            apply_exact(assignment, values, conditional=conditional)
        # ``command.assignments`` carries the conditional ``${name=word}``/``${name:=word}``
        # writes and the persisting redirection assignments this replay does not model. Leaving
        # them out kept the stale prior value in the table and then published it as the
        # authoritative eval program, so drop the name instead of asserting the old text.
        for assignment in command.assignments:
            if assignment.conditional or assignment.assign_if_null:
                apply_exact(assignment, values, conditional=True)
        eval_assignments, eval_unsets = _static_eval_mutations(refreshed_command, limits=limits)
        for mutation in eval_assignments:
            apply_exact(mutation.assignment, values, conditional=conditional)
        unset_names, unknown_unset = _unset_action(command)
        if unknown_unset or command.unknown_builtin_content is not None:
            values.clear()
        for name in (*unset_names, *eval_unsets):
            values.pop(name, None)
    return tuple(refreshed)


def _literal_printf_arguments(command: _CommandEvidence) -> tuple[_ArgPort, ...] | None:
    """Return arguments for one statically resolved builtin ``printf``."""
    pending = [command.executable]
    executable_index: int | None = None
    while pending:
        executable = pending.pop()
        if (
            executable.name == "printf"
            and executable.literal == "printf"
            and not executable.external_lookup
            and executable.argv_index is not None
        ):
            executable_index = executable.argv_index
            break
        pending.extend(executable.alternates)
    if executable_index is None:
        return None
    arguments = command.argv[executable_index + 1 :]
    if arguments and not arguments[0].dynamic and arguments[0].literal == "--":
        arguments = arguments[1:]
    return arguments


def _decode_printf_b_literal(text: str) -> tuple[str, bool] | None:
    """Decode one bounded Bash ``printf %b`` literal and whether ``\\c`` stopped output."""
    simple_escapes = {
        "\\": "\\",
        "a": "\a",
        "b": "\b",
        "e": "\x1b",
        "E": "\x1b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    rendered: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            rendered.append(text[index])
            index += 1
            continue
        if index + 1 >= len(text):
            return None
        escape = text[index + 1]
        if escape in simple_escapes:
            rendered.append(simple_escapes[escape])
            index += 2
            continue
        if escape == "c":
            return "".join(rendered), True
        if escape in "01234567":
            digit_limit = 4 if escape == "0" else 3
            stop = index + 1
            while stop < len(text) and stop < index + 1 + digit_limit and text[stop] in "01234567":
                stop += 1
            rendered.append(chr(int(text[index + 1 : stop], 8) & _OCTAL_BYTE_MASK))
            index = stop
            continue
        if escape in {"x", "u", "U"}:
            digit_limit = {"x": 2, "u": 4, "U": 8}[escape]
            start = index + 2
            stop = start
            while (
                stop < len(text)
                and stop < start + digit_limit
                and text[stop] in "0123456789abcdefABCDEF"
            ):
                stop += 1
            if stop == start:
                return None
            codepoint = int(text[start:stop], 16)
            if codepoint > _UNICODE_MAX or _SURROGATE_MIN <= codepoint <= _SURROGATE_MAX:
                return None
            rendered.append(chr(codepoint))
            index = stop
            continue
        rendered.extend(("\\", escape))
        index += 2
    return "".join(rendered), False


def _printf_conversion_at(
    format_text: str,
    index: int,
) -> tuple[str, int, int] | None:
    """Parse one bounded Bash printf conversion and its star-supplied arguments."""
    if index >= len(format_text) or format_text[index] != "%":
        return None
    cursor = index + 1
    if cursor < len(format_text) and format_text[cursor] == "%":
        return "%", cursor + 1, 0
    while cursor < len(format_text) and format_text[cursor] in "#0- +":
        cursor += 1
    star_arguments = 0
    if cursor < len(format_text) and format_text[cursor] == "*":
        star_arguments += 1
        cursor += 1
    else:
        while (
            cursor < len(format_text)
            and format_text[cursor].isascii()
            and format_text[cursor].isdigit()
        ):
            cursor += 1
    if cursor < len(format_text) and format_text[cursor] == ".":
        cursor += 1
        if cursor < len(format_text) and format_text[cursor] == "*":
            star_arguments += 1
            cursor += 1
        else:
            while (
                cursor < len(format_text)
                and format_text[cursor].isascii()
                and format_text[cursor].isdigit()
            ):
                cursor += 1
    if cursor >= len(format_text):
        return None
    return format_text[cursor], cursor + 1, star_arguments


def _literal_printf_stdout(  # noqa: PLR0911, PLR0912, PLR0915
    command: _CommandEvidence,
    limits: TaintLimits,
) -> ContentExpr | None:
    """Return one bounded exact builtin ``printf`` output."""
    arguments = _literal_printf_arguments(command)
    if (
        arguments is None
        or not arguments
        or arguments[0].dynamic
        or arguments[0].literal == "-v"
        or arguments[0].literal.startswith("-v")
    ):
        return None
    format_text = arguments[0].literal
    values = arguments[1:]
    rendered: list[ContentExpr] = []
    rendered_literal_chars = 0
    value_index = 0
    first_pass = True
    saw_conversion = False
    saw_escape = False
    while first_pass or value_index < len(values):
        first_pass = False
        pass_start = value_index
        index = 0
        while index < len(format_text):
            character = format_text[index]
            if character == "%":
                parsed = _printf_conversion_at(format_text, index)
                if parsed is None:
                    return None
                conversion, next_index, star_arguments = parsed
                if conversion == "%":
                    rendered.append(LiteralTransfer("%"))
                    rendered_literal_chars += 1
                elif conversion in {"b", "s"}:
                    saw_conversion = True
                    value_index += star_arguments
                    content = (
                        values[value_index].content
                        if value_index < len(values)
                        else LiteralTransfer("")
                    )
                    value_index += 1
                    if conversion == "b":
                        literal = _exact_content_literal(content, {}, limits)
                        decoded = _decode_printf_b_literal(literal) if literal is not None else None
                        if decoded is not None:
                            text, stopped = decoded
                            rendered.append(LiteralTransfer(text))
                            rendered_literal_chars += len(text)
                            if stopped:
                                return concat(*rendered)
                            index = next_index
                            continue
                    rendered.append(content)
                else:
                    return None
                index = next_index
                continue
            if character == "\\":
                saw_escape = True
                if index + 1 >= len(format_text):
                    return None
                escaped = format_text[index + 1]
                if escaped == "n":
                    rendered.append(LiteralTransfer("\n"))
                elif escaped == "\\":
                    rendered.append(LiteralTransfer("\\"))
                else:
                    return None
                rendered_literal_chars += 1
                index += 2
                continue
            rendered.append(LiteralTransfer(character))
            rendered_literal_chars += 1
            index += 1
        if value_index == pass_start:
            break
        if rendered_literal_chars > _MAX_TRACKED_LITERAL_CHARS:
            return None
    if values and not saw_conversion:
        return None
    if not saw_conversion and not saw_escape:
        return None
    return concat(*rendered)


def _printf_b_unrepresentable(command: _CommandEvidence, limits: TaintLimits) -> bool:
    """Return whether dynamic ``%b`` escape decoding remains after exact rendering."""
    arguments = _literal_printf_arguments(command)
    if (
        arguments is None
        or not arguments
        or arguments[0].dynamic
        or arguments[0].literal == "-v"
        or arguments[0].literal.startswith("-v")
    ):
        return False
    format_text = arguments[0].literal
    values = arguments[1:]
    value_index = 0
    first_pass = True
    while first_pass or value_index < len(values):
        first_pass = False
        pass_start = value_index
        index = 0
        while index < len(format_text):
            if format_text[index] != "%":
                index += 1
                continue
            parsed = _printf_conversion_at(format_text, index)
            if parsed is None:
                return False
            conversion, next_index, star_arguments = parsed
            if conversion == "%":
                index = next_index
                continue
            value_index += star_arguments
            content = (
                values[value_index].content if value_index < len(values) else LiteralTransfer("")
            )
            value_index += 1
            if conversion == "b":
                literal = _exact_content_literal(content, {}, limits)
                if literal is None or _decode_printf_b_literal(literal) is None:
                    return True
            index = next_index
        if value_index == pass_start:
            break
    return False


def _producer_stdout(
    command: _CommandEvidence,
    stdin: ContentExpr,
    limits: TaintLimits,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> ContentExpr:
    """Return a conservative stdout expression for one command."""
    executable = command.executable
    if (
        executable.name in {"false", "true"}
        and executable.name == executable.literal
        and not executable.external_lookup
        and not executable.alternates
        and not command.called_function_context_ids
    ):
        return LiteralTransfer("")
    head_index = command.executable.argv_index
    payload_start = head_index + 1 if head_index is not None else min(1, len(command.argv))
    operand_ports = command.argv[payload_start:]
    argv_content = concat(*(port.content for port in operand_ports))
    if not executable.alternates and not command.called_function_context_ids:
        literal_printf = _literal_printf_stdout(command, limits)
        if literal_printf is not None:
            return literal_printf
    # A command may reproduce a resource it names as an operand, or a process substitution it
    # reads, the same way it may reproduce its stdin (issue #136): the redirection form
    # ``cat < s.sh`` and the operand form ``cat s.sh`` are the same handoff idiom and must resolve
    # to the same content. Reusing ``_shell_script_source_expression`` keeps this content-aware --
    # an operand that names an untracked resource resolves to no marker-bearing evidence, so an
    # ordinary ``cat README.md | bash`` still certifies.
    operand_sources = tuple(
        _shell_script_source_expression(port, process_resources, stdin) for port in operand_ports
    )
    return choice(
        OutsideGap(),
        argv_content,
        stdin,
        *operand_sources,
        # A call reproduces the stdout its function body aggregated into that scope.
        *(StreamRef(context) for context in command.called_function_context_ids),
    )


def _static_write_definitions(
    events: tuple[_RedirectionEvent, ...],
    output: ContentExpr,
    inherited: _DescriptorBindings | None = None,
    guarded: frozenset[int] = frozenset(),
) -> tuple[_FlowWrite, ...]:
    """Replay output descriptors and return static resource writes they receive.

    Args:
        events: The redirection events attached to one command or compound.
        output: The stdout content this command or compound produces.
        inherited: Descriptor bindings the enclosing compounds installed.
        guarded: Descriptors some other command or compound in this body binds.

    Returns:
        The static resource writes the replayed descriptors receive.
    """
    writes: list[_FlowWrite] = []
    for event in sorted(events, key=lambda candidate: candidate.ordinal):
        if event.descriptor is None or event.operator not in _OUTPUT_REDIRECTION_OPERATORS:
            continue
        if (
            isinstance(event.target, StaticResourceTarget)
            and event.operator not in _APPEND_REDIRECTION_OPERATORS
        ):
            writes.append(_FlowWrite(event.target.key, LiteralTransfer("")))
    bindings = _output_bindings(events, inherited=inherited, guarded=guarded)
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
        elif isinstance(current, Choice | Concat | _SecondPassConditionalAssignment):
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
                raise _MalformedTaintEvidence(
                    GuardRefusal(
                        "taint.evidence.scope-environment-cycle",
                        "shell taint stream scope cannot be structured",
                    )
                )
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
                and current_scope.kind in _SHARED_ENVIRONMENT_SCOPE_KINDS
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


def _scoped_variable_environment(name: str | int) -> int | None:
    """Return the environment encoded in one internal scoped variable key."""
    if not isinstance(name, str) or not name.startswith("\0"):
        return None
    environment, separator, _unscoped = name[1:].partition("\0")
    if not separator:
        return None
    try:
        return int(environment)
    except ValueError:
        return None


def _is_function_positional_parameter(name: str) -> bool:
    """Return whether a variable is populated from shell-function call arguments."""
    return name in {"@", "*", _QUOTED_FUNCTION_POSITIONAL_STAR} or (
        name.isdecimal() and name.isascii() and bool(name.strip("0"))
    )


def _is_second_pass_positional_parameter(name: str) -> bool:
    """Return whether a second-pass parameter reads a positional an enclosing call can bind.

    This is ``_is_function_positional_parameter`` plus ``$0``, which that predicate deliberately
    excludes: inside a shell function ``$0`` is the script name rather than a call argument, so
    letting function-argument substitution reach it would be wrong. A shell ``-c`` payload binds
    it from the first operand after the payload word instead, so the second parse has to be able
    to name it. Everywhere else ``0`` stays unbound and resolves to the same absent-evidence value
    the previous ``OutsideGap`` lowering produced.

    Args:
        name: The parameter name as the second-pass reparse spells it.

    Returns:
        Whether the name reads a bindable positional parameter.
    """
    return name == "0" or _is_function_positional_parameter(name)


def _function_positional_index(name: str, argument_count: int) -> int | None:
    """Return a bounded zero-based call-argument index, or None when absent."""
    digits = name.lstrip("0")
    maximum = str(argument_count)
    if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
        return None
    index = int(digits) - 1
    return index if index < argument_count else None


def _expression_function_positionals(expression: ContentExpr) -> set[str]:
    """Collect function-positional reads from one content expression."""
    names: set[str] = set()
    pending = [expression]
    while pending:
        current = pending.pop()
        if isinstance(current, VariableRef):
            name = _unscoped_variable_name(current.name)
            if _is_function_positional_parameter(name):
                names.add(name)
        elif isinstance(current, Choice | Concat | _SecondPassConditionalAssignment):
            pending.extend(current.parts)
    return names


def _substitute_local_contents(
    expression: ContentExpr,
    local_contents: Mapping[str, ContentExpr],
    limits: TaintLimits,
    *,
    resolving: frozenset[str] = frozenset(),
) -> ContentExpr:
    """Substitute ordered callee-local definitions while retaining caller references."""
    pending: list[tuple[ContentExpr, frozenset[str], int, bool]] = [
        (expression, resolving, 0, False)
    ]
    results: list[ContentExpr] = []
    while pending:
        current, active_names, depth, assemble = pending.pop()
        if assemble:
            if isinstance(current, _SecondPassConditionalAssignment):
                operand = results.pop()
                results.append(
                    _SecondPassConditionalAssignment(
                        current.name,
                        operand,
                        current.assign_if_null,
                    )
                )
                continue
            if not isinstance(current, Choice | Concat):
                raise _MalformedTaintEvidence(
                    GuardRefusal(
                        "taint.local-substitution.unassembled-result",
                        "local content substitution cannot be structured",
                    )
                )
            parts = current.parts
            count = len(parts)
            resolved_parts = tuple(results[-count:]) if count else ()
            if count:
                del results[-count:]
            results.append(
                choice(*resolved_parts) if isinstance(current, Choice) else concat(*resolved_parts)
            )
            continue
        if isinstance(current, VariableRef):
            name = _unscoped_variable_name(current.name)
            replacement = local_contents.get(name)
            if replacement is None or name in active_names:
                results.append(current)
                continue
            if depth >= limits.max_local_substitution_depth:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.local-substitution.depth-limit", "local substitution depth limit"
                    )
                )
            pending.append(
                (
                    replacement,
                    active_names | {name},
                    depth + 1,
                    False,
                )
            )
            continue
        if isinstance(
            current,
            LiteralTransfer | _SecondPassVariableRef | OutsideGap | ResourceRef | StreamRef,
        ):
            results.append(current)
            continue
        pending.append((current, active_names, depth, True))
        if isinstance(current, _SecondPassConditionalAssignment):
            pending.append((current.operand, active_names, depth, False))
            continue
        pending.extend((part, active_names, depth, False) for part in reversed(current.parts))
    if len(results) != 1:
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.local-substitution.multiple-results",
                "local content substitution cannot be structured",
            )
        )
    return results[0]


def _substitute_function_positionals(  # noqa: PLR0911
    expression: ContentExpr,
    arguments: tuple[ContentExpr, ...] | None,
    shift_offset: int,
) -> ContentExpr:
    """Apply ordered function positional mutations to one effect expression."""
    if isinstance(expression, VariableRef):
        name = _unscoped_variable_name(expression.name)
        if not _is_function_positional_parameter(name):
            return expression
        if name in {"@", "*", _QUOTED_FUNCTION_POSITIONAL_STAR}:
            if arguments is None and shift_offset == 0:
                return expression
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.unsupported-star-mutation",
                    "unsupported function positional mutation",
                )
            )
        index = _function_positional_index(name, len(arguments)) if arguments is not None else None
        if arguments is not None:
            return arguments[index] if index is not None else LiteralTransfer("")
        if len(name) > _MAX_BRACE_INTEGER_DIGITS:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.index-digit-limit",
                    "function positional mutation limit exceeded",
                )
            )
        return VariableRef(str(int(name) + shift_offset))
    if isinstance(
        expression,
        LiteralTransfer | _SecondPassVariableRef | OutsideGap | ResourceRef | StreamRef,
    ):
        return expression
    if isinstance(expression, _SecondPassConditionalAssignment):
        return _SecondPassConditionalAssignment(
            expression.name,
            _substitute_function_positionals(
                expression.operand,
                arguments,
                shift_offset,
            ),
            expression.assign_if_null,
        )
    if isinstance(expression, Choice):
        return choice(
            *(
                _substitute_function_positionals(part, arguments, shift_offset)
                for part in expression.parts
            )
        )
    return concat(
        *(
            _substitute_function_positionals(part, arguments, shift_offset)
            for part in expression.parts
        )
    )


def _scope_expression(expression: ContentExpr, environment: int) -> ContentExpr:
    """Bind first-pass shell variable references to one lexical environment."""
    if isinstance(expression, VariableRef):
        return VariableRef(_scoped_variable_name(environment, expression.name))
    if isinstance(
        expression,
        LiteralTransfer | _SecondPassVariableRef | OutsideGap | ResourceRef | StreamRef,
    ):
        return expression
    if isinstance(expression, _SecondPassConditionalAssignment):
        return _SecondPassConditionalAssignment(
            expression.name,
            _scope_expression(expression.operand, environment),
            expression.assign_if_null,
        )
    if isinstance(expression, Choice):
        return choice(*(_scope_expression(part, environment) for part in expression.parts))
    return concat(*(_scope_expression(part, environment) for part in expression.parts))


def _rescope_expression(
    expression: ContentExpr,
    source_environment: int,
    target_environment: int,
) -> ContentExpr:
    """Move lexical reads to an execution environment while preserving function locals."""
    if isinstance(expression, VariableRef):
        environment = _scoped_variable_environment(expression.name)
        if environment != source_environment:
            return expression
        return VariableRef(
            _scoped_variable_name(
                target_environment,
                _unscoped_variable_name(expression.name),
            )
        )
    if isinstance(
        expression,
        LiteralTransfer | _SecondPassVariableRef | OutsideGap | ResourceRef | StreamRef,
    ):
        return expression
    if isinstance(expression, _SecondPassConditionalAssignment):
        return _SecondPassConditionalAssignment(
            expression.name,
            _rescope_expression(
                expression.operand,
                source_environment,
                target_environment,
            ),
            expression.assign_if_null,
        )
    if isinstance(expression, Choice):
        return choice(
            *(
                _rescope_expression(part, source_environment, target_environment)
                for part in expression.parts
            )
        )
    return concat(
        *(
            _rescope_expression(part, source_environment, target_environment)
            for part in expression.parts
        )
    )


def _scope_function_variables(  # noqa: PLR0911
    expression: ContentExpr,
    function_context: int | None,
    local_names: frozenset[str],
    caller_locals: Mapping[str, tuple[int, ...]],
    call_prefixes: Mapping[str, tuple[ContentExpr, ...]],
) -> ContentExpr:
    """Bind reads to active locals, dynamic callers, and call-prefix overlays."""
    if function_context is None:
        return expression
    if isinstance(expression, VariableRef):
        if _is_function_positional_parameter(expression.name):
            return choice(*call_prefixes.get(expression.name, (LiteralTransfer(""),)))
        if expression.name in local_names:
            return VariableRef(_scoped_variable_name(function_context, expression.name))
        alternatives: list[ContentExpr] = [
            expression,
            *(
                VariableRef(_scoped_variable_name(context, expression.name))
                for context in caller_locals.get(expression.name, ())
            ),
            *call_prefixes.get(expression.name, ()),
        ]
        return choice(*alternatives)
    if isinstance(
        expression,
        LiteralTransfer | _SecondPassVariableRef | OutsideGap | ResourceRef | StreamRef,
    ):
        return expression
    if isinstance(expression, _SecondPassConditionalAssignment):
        return _SecondPassConditionalAssignment(
            expression.name,
            _scope_function_variables(
                expression.operand,
                function_context,
                local_names,
                caller_locals,
                call_prefixes,
            ),
            expression.assign_if_null,
        )
    if isinstance(expression, Choice):
        return choice(
            *(
                _scope_function_variables(
                    part,
                    function_context,
                    local_names,
                    caller_locals,
                    call_prefixes,
                )
                for part in expression.parts
            )
        )
    return concat(
        *(
            _scope_function_variables(
                part,
                function_context,
                local_names,
                caller_locals,
                call_prefixes,
            )
            for part in expression.parts
        )
    )


def _eval_argument_ports(command: _CommandEvidence, head_index: int) -> tuple[_ArgPort, ...]:
    """Return eval's program arguments with bash's end-of-options marker removed."""
    arguments = command.argv[head_index + 1 :]
    if arguments and not arguments[0].dynamic and arguments[0].literal == "--":
        # eval accepts no options; bash strips `--` before reparsing the program.
        return arguments[1:]
    return arguments


def _static_eval_programs(command: _CommandEvidence) -> tuple[str, ...]:
    """Return eval's bounded exact joined programs."""
    if command.resolved_eval_programs:
        return command.resolved_eval_programs
    if command.resolved_eval_program is not None:
        return (command.resolved_eval_program,)
    for executable in _builtin_eval_candidates(command):
        if executable.argv_index is None:
            continue
        arguments = _eval_argument_ports(command, executable.argv_index)
        if all(not argument.dynamic for argument in arguments):
            return (" ".join(argument.literal for argument in arguments),)
    return ()


def _static_eval_word_metadata(  # noqa: PLR0912
    program: str,
) -> tuple[_StaticEvalWordMetadata, ...] | None:
    """Return lexical properties for each eval word."""
    metadata: list[_StaticEvalWordMetadata] = []
    in_word = False
    eligible = True
    word_start = 0
    quote: str | None = None
    index = 0
    while index < len(program):
        character = program[index]
        if quote is not None:
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"' and index + 1 < len(program):
                index += 1
            index += 1
            continue
        if character in {"'", '"'}:
            if not in_word:
                word_start = index
            in_word = True
            eligible = False
            quote = character
            index += 1
            continue
        if character == "\\":
            if not in_word:
                word_start = index
            in_word = True
            eligible = False
            index += 2 if index + 1 < len(program) else 1
            continue
        if character in _SHELL_WORD_SEPARATORS:
            if in_word:
                raw_word = program[word_start:index]
                metadata.append(
                    _StaticEvalWordMetadata(
                        keyword_eligible=eligible,
                        redirection_prefix=(
                            character in "<>"
                            and eligible
                            and (
                                raw_word.isdigit()
                                or (
                                    raw_word.startswith("{")
                                    and raw_word.endswith("}")
                                    and _static_variable_name(raw_word[1:-1])
                                )
                            )
                        ),
                        source=raw_word,
                    )
                )
                in_word = False
                eligible = True
            index += 1
            continue
        if not in_word:
            word_start = index
        in_word = True
        index += 1
    if quote is not None:
        return None
    if in_word:
        metadata.append(
            _StaticEvalWordMetadata(
                keyword_eligible=eligible,
                redirection_prefix=False,
                source=program[word_start:],
            )
        )
    return tuple(metadata)


def _static_eval_shlex_program(program: str) -> str:
    """Remove unquoted Bash dollar-quote introducers before shlex tokenization."""
    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(program):
        character = program[index]
        if quote is not None:
            normalized.append(character)
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"' and index + 1 < len(program):
                index += 1
                normalized.append(program[index])
            index += 1
            continue
        if character == "\\" and index + 1 < len(program):
            normalized.extend((character, program[index + 1]))
            index += 2
            continue
        if character == "$" and index + 1 < len(program) and program[index + 1] == "'":
            closing = index + 2
            while closing < len(program):
                if program[closing] == "\\":
                    closing += 2
                    continue
                if program[closing] == "'":
                    break
                closing += 1
            if closing >= len(program):
                normalized.append(character)
                index += 1
                continue
            normalized.append(shlex.quote(_decode_ansi_c_eval_word(program[index + 2 : closing])))
            index = closing + 1
            continue
        if character == "$" and index + 1 < len(program) and program[index + 1] == '"':
            index += 1
            continue
        normalized.append(character)
        if character in {"'", '"'}:
            quote = character
        index += 1
    return "".join(normalized)


def _strip_active_shell_comments(program: str) -> str:
    """Remove exact Bash comments while preserving quoted and escaped ``#`` bytes."""
    stripped: list[str] = []
    quote: str | None = None
    ansi_c_quote = False
    at_word_start = True
    index = 0
    while index < len(program):
        character = program[index]
        if quote is not None:
            stripped.append(character)
            if character == quote:
                quote = None
                ansi_c_quote = False
            elif character == "\\" and (quote == '"' or ansi_c_quote) and index + 1 < len(program):
                index += 1
                stripped.append(program[index])
            index += 1
            continue
        if character == "\\":
            stripped.append(character)
            if index + 1 < len(program):
                index += 1
                stripped.append(program[index])
            at_word_start = False
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            # ``$'...'`` honours backslash escapes, so a ``\'`` inside it does not close the
            # word and the bytes after it are still quoted data rather than a comment.
            ansi_c_quote = character == "'" and index > 0 and program[index - 1] == "$"
            stripped.append(character)
            at_word_start = False
            index += 1
            continue
        if character == "#" and at_word_start:
            while index < len(program) and program[index] != "\n":
                index += 1
            continue
        stripped.append(character)
        at_word_start = character in _SHELL_COMMENT_WORD_SEPARATORS
        index += 1
    return "".join(stripped)


def _strip_eval_line_continuations(program: str) -> str:
    """Remove an unescaped backslash-newline line continuation outside single quotes.

    Bash removes an unquoted or double-quoted ``\\`` immediately followed by a newline from
    its input stream before further parsing, joining the two lines with no character
    inserted. A backslash retains no such meaning inside plain single quotes or ``$'...'``,
    which this function's quote tracker treats identically, so a continuation there is left
    untouched, matching real Bash.
    """
    stripped: list[str] = []
    quote: str | None = None
    index = 0
    length = len(program)
    while index < length:
        character = program[index]
        if quote == "'":
            stripped.append(character)
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "\\" and index + 1 < length:
            following = program[index + 1]
            if following == "\n":
                index += 2
                continue
            stripped.append(character)
            stripped.append(following)
            index += 2
            continue
        if character == quote:
            quote = None
        elif quote is None and character in {"'", '"'}:
            quote = character
        stripped.append(character)
        index += 1
    return "".join(stripped)


def _decode_ansi_c_eval_word(body: str) -> str:
    """Decode Bash ANSI-C quoting needed by static eval command words."""
    decoded: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\" or index + 1 >= len(body):
            decoded.append(character)
            index += 1
            continue
        escaped = body[index + 1]
        if escaped == "\n":
            index += 2
            continue
        if escaped in _ANSI_C_SIMPLE_ESCAPES:
            decoded.append(_ANSI_C_SIMPLE_ESCAPES[escaped])
            index += 2
            continue
        if escaped in {"x", "u", "U"}:
            width = {"x": 2, "u": 4, "U": 8}[escaped]
            end = index + 2
            while (
                end < len(body)
                and end < index + 2 + width
                and body[end].lower() in "0123456789abcdef"
            ):
                end += 1
            if end > index + 2:
                codepoint = int(body[index + 2 : end], 16)
                decoded.append(
                    chr(codepoint)
                    if codepoint <= _EVAL_UNICODE_MAX
                    and not _EVAL_SURROGATE_MIN <= codepoint <= _EVAL_SURROGATE_MAX
                    else "\ufffd"
                )
                index = end
                continue
        if escaped in "01234567":
            end = index + 2
            while end < len(body) and end < index + 4 and body[end] in "01234567":
                end += 1
            decoded.append(chr(int(body[index + 1 : end], 8) & 0xFF))
            index = end
            continue
        if escaped == "c" and index + 2 < len(body):
            controlled = body[index + 2]
            uppercased = controlled.upper()
            decoded.append(chr(ord(uppercased if len(uppercased) == 1 else controlled) ^ 0x40))
            index += 3
            continue
        decoded.extend(("\\", escaped))
        index += 2
    return "".join(decoded)


def _static_eval_commands(
    command: _CommandEvidence,
    *,
    limits: TaintLimits,
) -> tuple[_StaticEvalCommand, ...]:
    """Tokenize bounded static eval input into simple command words."""
    commands: list[_StaticEvalCommand] = []
    for program in _static_eval_programs(command):
        commands.extend(
            _static_eval_program_commands(
                program, active_function_names=command.active_function_names, limits=limits
            )
        )
    return tuple(commands)


def _static_eval_redirection_target(
    source_word: str,
    operator: str,
    *,
    limits: TaintLimits,
) -> RedirectionTarget:
    """Resolve one exact eval payload redirection operand to the target it names.

    The operand is reparsed with the payload's own second-pass quote and parameter semantics, so
    a quoted ``'$LOG'`` names the literal file Bash names while an unquoted ``$LOG`` resolves to
    no static key. The latter is the same dynamic target the authored path already declines to
    write through, and stays tracked as issue #151 for both routes rather than being modeled
    here.

    Args:
        source_word: The operand word exactly as the payload spells it.
        operator: The redirection operator this operand belongs to.

    Returns:
        The target this operand resolves to.
    """
    literal = _exact_content_literal(
        _eval_reparse_content(LiteralTransfer(source_word), limits), {}, limits
    )
    return resolve_redirection_target(
        source_word if literal is None else literal,
        operator,
        dynamic=literal is None,
        parse_descriptor=_static_eval_descriptor,
    )


def _static_eval_redirection_descriptor(lexeme: str, prefix: str | None) -> int | None:
    """Return the descriptor one exact eval payload redirection binds.

    A ``{name}>`` names a descriptor Bash chooses at run time, so it binds none statically and
    the replay skips it the same way the authored path skips its own dynamic descriptors.

    Args:
        lexeme: The redirection operator.
        prefix: The descriptor prefix word attached to it, when it carries one.

    Returns:
        The descriptor this redirection binds, or None when Bash chooses it at run time.
    """
    if prefix is None:
        return 0 if lexeme in _INPUT_REDIRECTION_OPERATORS else 1
    return _static_eval_descriptor(prefix) if prefix.isdigit() else None


def _static_eval_program_commands(  # noqa: PLR0915
    program: str,
    *,
    active_function_names: frozenset[str] = frozenset(),
    limits: TaintLimits,
) -> tuple[_StaticEvalCommand, ...]:
    """Tokenize one exact eval program and retain reserved-word eligibility."""
    program = _strip_active_shell_comments(program)
    # Bash removes a line continuation before its own lexer ever sees quotes or words, so this
    # runs once, ahead of both the metadata walk and the ``shlex`` pass below, keeping them in
    # lockstep. Left unhandled, an ordinary continuation both misaligns ``shlex``'s token count
    # against ``metadata`` and desyncs the two independent quote trackers below (issue #134).
    program = _strip_eval_line_continuations(program)
    lexer = shlex.shlex(
        _static_eval_shlex_program(program),
        posix=True,
        punctuation_chars=";&|<>()\n",
    )
    lexer.commenters = ""
    # ``shlex`` tests whitespace before punctuation, so a newline left in ``whitespace`` would
    # silently join the commands on either side of it into one word list.
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    lexer.whitespace_split = True
    metadata = _static_eval_word_metadata(program)
    if metadata is None:
        # An eval payload the tokenizer cannot accept is missing evidence, not the absence of
        # any: silently contributing nothing here let a prior mutation, or a later sink's
        # dependence on one, certify by omission (issue #134). Fail closed instead.
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-payload.missing-metadata", "shell eval payload cannot be tokenized"
            )
        )
    metadata_index = 0
    commands: list[_StaticEvalCommand] = []
    words: list[str] = []
    word_eligibility: list[bool] = []
    source_words: list[str] = []
    redirection_prefixes: list[bool] = []
    redirections: list[_RedirectionEvent] = []
    # Non-None exactly while the next word is a redirection's target rather than an argv word.
    pending_redirection: tuple[str, int | None] | None = None
    try:
        tokens = tuple(lexer)
    except ValueError as error:
        raise _TaintLimitExceeded(
            GuardRefusal("taint.eval-payload.lex-error", "shell eval payload cannot be tokenized")
        ) from error
    for lexeme in tokens:
        if lexeme and all(character in ";&|()\n" for character in lexeme):
            if words:
                commands.append(
                    _StaticEvalCommand(
                        tuple(words),
                        tuple(word_eligibility),
                        tuple(source_words),
                        active_function_names=active_function_names,
                        # Adjacent separators lex as one run, so the operator that actually
                        # terminates the command is the run with its trailing newlines removed.
                        asynchronous=lexeme.rstrip("\n") == "&",
                        # ``(`` never opens a subshell directly after an assignment prefix, so an
                        # unquoted one there begins a compound array assignment whose elements the
                        # lexer scatters into the following commands.
                        array_compound="(" in lexeme and _static_eval_assignment_prefix(words[-1]),
                        redirections=tuple(redirections),
                    )
                )
                words.clear()
                word_eligibility.clear()
                source_words.clear()
                redirection_prefixes.clear()
                redirections.clear()
            pending_redirection = None
            continue
        if (
            lexeme
            and any(character in "<>" for character in lexeme)
            and all(character in "<>&|" for character in lexeme)
        ):
            if lexeme not in _REDIRECTION_OPERATORS:
                # A punctuation run this analysis cannot name is missing evidence, not the
                # absence of any: dropping it would leave the write it performs unmodeled while
                # the rest of the payload still contributed state (issue #146, the same
                # reasoning issue #134 applied to an untokenizable payload).
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-payload.redirection-lexeme",
                        "shell eval payload cannot be tokenized",
                    )
                )
            prefix: str | None = None
            if redirection_prefixes and redirection_prefixes[-1]:
                prefix = source_words[-1]
                words.pop()
                word_eligibility.pop()
                source_words.pop()
                redirection_prefixes.pop()
            pending_redirection = (lexeme, _static_eval_redirection_descriptor(lexeme, prefix))
            continue
        if metadata_index >= len(metadata):
            # ``shlex`` and the metadata walk disagree on word count for this payload; treat the
            # mismatch as missing evidence rather than none (issue #134).
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-payload.metadata-exhausted",
                    "shell eval payload cannot be tokenized",
                )
            )
        token_metadata = metadata[metadata_index]
        metadata_index += 1
        if pending_redirection is not None:
            operator, descriptor = pending_redirection
            pending_redirection = None
            redirections.append(
                _RedirectionEvent(
                    len(redirections),
                    operator,
                    descriptor,
                    _static_eval_redirection_target(token_metadata.source, operator, limits=limits),
                )
            )
            continue
        words.append(lexeme)
        word_eligibility.append(token_metadata.keyword_eligible)
        source_words.append(token_metadata.source)
        redirection_prefixes.append(token_metadata.redirection_prefix)
    if metadata_index != len(metadata):
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-payload.metadata-residue", "shell eval payload cannot be tokenized"
            )
        )
    if words:
        commands.append(
            _StaticEvalCommand(
                tuple(words),
                tuple(word_eligibility),
                tuple(source_words),
                active_function_names=active_function_names,
                redirections=tuple(redirections),
            )
        )
    return _annotate_static_eval_control(tuple(commands), limits=limits)


def _static_status_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def _static_status_or(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return None


def _static_status_not(status: bool | None) -> bool | None:
    return None if status is None else not status


# Load bearing: these are the leading keywords `_static_eval_mutations` strips before looking for
# an assignment/command word, so a compound construct's own opening keyword does not stop it from
# reaching the assignment inside. `{`, `do`, `while`, and `until` in particular are what makes a
# brace group's or a loop body's mutation get replayed at all -- `eval '{ X=doc-; }'` and
# `eval 'for i in 1; do X=doc-; done'` need `{` and `do` stripped so `X=doc-` is still recognized as
# a prefix assignment, per AD-18's "brace groups and loop bodies are the exception to that
# stop-line and are modeled" (ARCHITECTURE.md). Removing any of the four looks like dead weight
# against that text, but it silently drops the mutation instead of raising, which reopens a false
# certification: the `eval-brace-group-assignment` and `eval-loop-body-assignment` fixtures in
# tests/test_github_ci_shell_scanner.py pin both cases refusing.
_STATIC_EVAL_MUTATION_PREFIXES = frozenset(
    {"coproc", "do", "elif", "else", "if", "then", "time", "until", "while", "{"}
)
# Options that declare an indexed or associative array rather than a scalar.
_STATIC_EVAL_ARRAY_DECLARATION_FLAGS = frozenset("aA")


def _static_eval_wrapped_executable(
    command: _StaticEvalCommand,
    index: int,
    *,
    negated: bool,
    literal_status_available: bool,
) -> _StaticEvalExecutable | None:
    """Resolve command/builtin wrappers around one static eval executable."""
    words = command.words
    bypasses_functions = False
    while index < len(words) and words[index] in {"builtin", "command"}:
        wrapper = words[index]
        if not bypasses_functions and wrapper in command.active_function_names:
            break
        bypasses_functions = True
        index += 1
        if wrapper == "builtin":
            if index < len(words) and words[index] == "--":
                index += 1
            continue
        while index < len(words):
            option = words[index]
            if option == "--":
                index += 1
                break
            if option.startswith("-") and option != "-":
                flags = option[1:]
                if "v" in flags or "V" in flags or set(flags) - {"p"}:
                    return None
                index += 1
                continue
            break
    if index >= len(words) or words[index] in {"done", "esac", "fi"}:
        return None
    return _StaticEvalExecutable(
        words[index],
        negated=negated,
        bypasses_functions=bypasses_functions,
        literal_status_available=literal_status_available,
        argv_index=index,
    )


def _static_eval_executable(
    command: _StaticEvalCommand,
    *,
    limits: TaintLimits,
) -> _StaticEvalExecutable | None:
    """Resolve one static eval command head and its literal negation."""
    index = 0
    negated = False
    literal_status_available = True
    # ``{`` belongs here for the same reason ``_STATIC_EVAL_MUTATION_PREFIXES`` carries it: a brace
    # group runs its body in the current shell. Omitting it resolved ``{ eval "$Y"; }`` to the head
    # ``{``, so the nested-eval guard and the call-graph name collection both looked past the real
    # command and ``eval '{ eval "$Y"; }'`` certified where the subshell spelling refused.
    control_prefixes = {
        "coproc",
        "do",
        "elif",
        "else",
        "if",
        "then",
        "time",
        "until",
        "while",
        "{",
    }
    while index < len(command.words):
        word = command.words[index]
        eligible = command.keyword_eligible[index]
        if eligible and word == "!":
            negated = not negated
            index += 1
            continue
        if not eligible or word not in control_prefixes:
            break
        index += 1
        if word == "coproc":
            literal_status_available = False
        if word == "time":
            while index < len(command.words) and command.words[index] == "-p":
                index += 1
    while (
        index < len(command.words)
        and _static_assignment_word(command.words[index]) is not None
        and _static_eval_assignment_content(command.source_words[index], limits=limits) is not None
    ):
        index += 1
    return _static_eval_wrapped_executable(
        command,
        index,
        negated=negated,
        literal_status_available=literal_status_available,
    )


def _static_eval_operand_expressions(
    parsed: _StaticEvalCommand,
    environment: int,
    *,
    scoped: bool,
    limits: TaintLimits,
) -> tuple[tuple[ContentExpr, ...], tuple[ContentExpr, ...]]:
    """Lower one payload command's operand words into content and script-source expressions."""
    executable = _static_eval_executable(parsed, limits=limits)
    start = min(1, len(parsed.source_words)) if executable is None else executable.argv_index + 1
    contents: list[ContentExpr] = []
    sources: list[ContentExpr] = []
    for source_word in parsed.source_words[start:]:
        reparsed = _eval_reparse_content(LiteralTransfer(source_word), limits)
        content = _lower_eval_assignment_operand(reparsed, environment, scoped=scoped)
        literal = _exact_content_literal(reparsed, {}, limits)
        contents.append(content)
        sources.append(
            _script_port_expression(
                _ArgPort(
                    source_word if literal is None else literal,
                    content,
                    dynamic=literal is None,
                ),
                OutsideGap(),
            )
        )
    return tuple(contents), tuple(sources)


def _static_eval_command_stdout(
    parsed: _StaticEvalCommand,
    environment: int,
    *,
    scoped: bool,
    limits: TaintLimits,
) -> ContentExpr:
    """Return the authored stdout one exact eval payload command produces.

    This mirrors ``_producer_stdout``'s conservative shape for an unknown command: an external
    gap, the command's own argv content, and the content of any static resource it names as an
    operand. Modeling the payload route more narrowly than the authored one is what let
    ``eval 'cat s.sh > t.sh'`` certify while the authored spelling refused (issue #146).

    Three limbs ``_producer_stdout`` carries are deliberately absent, each an over-approximation
    in the fail-closed direction rather than a dropped flow: ``printf`` format exactness, which
    is tied to the ``_CommandEvidence``/``_ArgPort`` graph the payload has no counterpart for;
    real standard input, which the payload replay does not model; and the called-function stdout
    limb, since a payload call site contributes no function scope here.

    Args:
        parsed: One tokenized command from an exact eval payload.
        environment: The execution environment the enclosing eval command runs in.
        scoped: Whether variable references resolve through scoped names.

    Returns:
        The content expression this payload command writes to its standard output.
    """
    contents, sources = _static_eval_operand_expressions(
        parsed, environment, scoped=scoped, limits=limits
    )
    return choice(OutsideGap(), concat(*contents), *sources)


def _static_eval_resource_writes(  # noqa: PLR0913
    command: _CommandEvidence,
    environment: int,
    inherited: _DescriptorBindings,
    guarded: frozenset[int],
    *,
    scoped: bool,
    limits: TaintLimits,
) -> tuple[_FlowWrite, ...]:
    """Return the resource writes an exact eval payload's own redirections perform.

    AD-18 replays a payload's state effects so a later sink observes them, but that replay only
    ever reached variable assignments. A redirection inside the payload registered nothing, so
    the file it wrote never entered the resource table at all and both the content-gated
    ``source`` guard and the ordinary script sink read a key the model believed was never
    written (issue #146). Lowering the write here, from the same ``_static_write_definitions``
    the authored path uses, puts it on the same footing as an authored redirection.

    A branch whose literal status is False contributes nothing, matching the reachability rule
    ``_static_eval_mutations`` already applies to a payload's assignments.

    The payload runs with the enclosing ``eval``'s own descriptors already installed, so those
    bindings, not just the enclosing compounds', are what a payload ``>&1`` resolves against.
    Passing the compound bindings alone left ``eval 'printf X=doc- >&1' > s.sh`` with descriptor
    1 unresolved and dropped the write, while the authored brace-group analogue refused.

    Args:
        command: The command whose payload may perform writes.
        environment: The execution environment that command runs in.
        inherited: Descriptor bindings the enclosing compounds installed.
        guarded: Descriptors some other command or compound in this body binds.
        scoped: Whether variable references resolve through scoped names.

    Returns:
        The static resource writes this command's exact eval payload performs.
    """
    if not _static_eval_programs(command):
        return ()
    payload_bindings = _output_bindings(command.redirections, inherited=inherited, guarded=guarded)
    writes: list[_FlowWrite] = []
    for parsed in _static_eval_commands(command, limits=limits):
        if parsed.execution_status is False or not parsed.redirections:
            continue
        writes.extend(
            _static_write_definitions(
                parsed.redirections,
                _static_eval_command_stdout(parsed, environment, scoped=scoped, limits=limits),
                payload_bindings,
                guarded,
            )
        )
    return tuple(writes)


def _static_eval_literal_status(
    command: _StaticEvalCommand,
    *,
    limits: TaintLimits,
) -> bool | None:
    """Return exact status for a resolved, unshadowed true/false eval command."""
    executable = _static_eval_executable(command, limits=limits)
    if (
        executable is None
        or executable.name not in {"false", "true"}
        or not executable.literal_status_available
        or command.asynchronous
        or (not executable.bypasses_functions and executable.name in command.active_function_names)
    ):
        return None
    status = executable.name == "true"
    if executable.negated:
        status = not status
    return status


def _annotate_static_eval_control(
    commands: tuple[_StaticEvalCommand, ...],
    *,
    limits: TaintLimits,
) -> tuple[_StaticEvalCommand, ...]:
    """Annotate exact eval commands with nested if-branch reachability."""
    controls: list[_StaticEvalControl] = []
    annotated: list[_StaticEvalCommand] = []

    def enclosing_status() -> bool | None:
        status: bool | None = True
        for frame in controls:
            if frame.phase in {"body", "else"}:
                status = _static_status_and(status, frame.body_status)
        return status

    for command in commands:
        head = command.words[0] if command.words else None
        if head == "if" and command.keyword_eligible[0]:
            parent = enclosing_status()
            controls.append(
                _StaticEvalControl(
                    parent_status=parent,
                    current_test_status=_static_eval_literal_status(command, limits=limits),
                )
            )
            annotated.append(replace(command, execution_status=parent))
            continue
        if controls and head == "then" and command.keyword_eligible[0]:
            frame = controls[-1]
            frame.body_status = _static_status_and(
                frame.parent_status,
                _static_status_and(
                    _static_status_not(frame.prior_branch_status),
                    frame.current_test_status,
                ),
            )
            frame.phase = "body"
            annotated.append(replace(command, execution_status=frame.body_status))
            continue
        if controls and head == "elif" and command.keyword_eligible[0]:
            frame = controls[-1]
            frame.prior_branch_status = _static_status_or(
                frame.prior_branch_status,
                frame.current_test_status,
            )
            frame.current_test_status = _static_eval_literal_status(command, limits=limits)
            frame.phase = "test"
            annotated.append(
                replace(
                    command,
                    execution_status=_static_status_and(
                        frame.parent_status,
                        _static_status_not(frame.prior_branch_status),
                    ),
                )
            )
            continue
        if controls and head == "else" and command.keyword_eligible[0]:
            frame = controls[-1]
            frame.prior_branch_status = _static_status_or(
                frame.prior_branch_status,
                frame.current_test_status,
            )
            frame.body_status = _static_status_and(
                frame.parent_status,
                _static_status_not(frame.prior_branch_status),
            )
            frame.phase = "else"
            annotated.append(replace(command, execution_status=frame.body_status))
            continue
        if controls and head == "fi" and command.keyword_eligible[0]:
            frame = controls.pop()
            annotated.append(replace(command, execution_status=frame.parent_status))
            continue
        if controls and controls[-1].phase == "test":
            controls[-1].current_test_status = None
        annotated.append(replace(command, execution_status=enclosing_status()))
    return tuple(annotated)


def _static_assignment_word(word: str) -> _AssignmentEvidence | None:
    """Return one scalar assignment represented by an already-tokenized shell word."""
    name, separator, value = word.partition("=")
    append = name.endswith("+")
    if append:
        name = name[:-1]
    if not separator or not _static_variable_name(name):
        return None
    return _AssignmentEvidence(name, LiteralTransfer(value), append=append)


def _static_eval_assignment_prefix(word: str) -> bool:
    """Return whether an eval payload word is an assignment awaiting its value.

    Only an unquoted ``(`` lexes apart from the word before it, and bash reads that position as a
    compound assignment rather than a subshell. A quoted or escaped ``(`` stays inside the word,
    where it really is one scalar character, so this deliberately does not match it.
    """
    return word.endswith("=") and _static_variable_name(word[:-1].removesuffix("+"))


def _static_eval_element_assignment(word: str) -> bool:
    """Return whether an already-tokenized eval payload word writes one array element.

    ``NAME[subscript]=`` is not a variable name, so ``_static_assignment_word`` declines it and
    the word becomes an executable the analysis then finds no evidence for. The write is real, so
    it fails closed instead.
    """
    name, separator, _ = word.partition("=")
    if not separator:
        return False
    target, bracket, subscript = name.removesuffix("+").partition("[")
    return bool(bracket) and subscript.endswith("]") and _static_variable_name(target)


def _static_eval_assignment_content(
    source_word: str,
    *,
    limits: TaintLimits,
) -> ContentExpr | None:
    """Lower one static eval assignment RHS with its second-pass quote semantics."""
    quote: str | None = None
    index = 0
    while index < len(source_word):
        character = source_word[index]
        if quote is not None:
            if character == quote:
                quote = None
            elif character == "\\" and quote == '"' and index + 1 < len(source_word):
                index += 1
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character == "=":
            name = source_word[:index]
            if name.endswith("+"):
                name = name[:-1]
            if not _static_variable_name(name):
                return None
            rhs = _eval_reparse_content(LiteralTransfer(source_word[index + 1 :]), limits)
            return _lower_eval_assignment_operand(rhs, 0, scoped=False)
        index += 1
    return None


def _static_eval_mutations(  # noqa: PLR0912, PLR0915
    command: _CommandEvidence,
    *,
    limits: TaintLimits,
) -> tuple[tuple[_StaticEvalAssignment, ...], tuple[str, ...]]:
    """Return exact scalar assignments and unsets from bounded static eval input."""
    assignments: list[_StaticEvalAssignment] = []
    unsets: list[str] = []
    aliases: dict[str, str] = {}

    def route_assignment(
        mutation: _StaticEvalAssignment,
    ) -> _StaticEvalAssignment:
        assignment = mutation.assignment
        if assignment.nameref_target is not None:
            if assignment.nameref_target:
                aliases[assignment.name] = assignment.nameref_target
            else:
                aliases.pop(assignment.name, None)
            return mutation
        target = assignment.name
        visited: set[str] = set()
        while target in aliases:
            if target in visited:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-nameref.assignment-cycle",
                        "shell eval nameref cycle cannot be represented",
                    )
                )
            visited.add(target)
            target = aliases[target]
        if target == assignment.name:
            return mutation
        return replace(mutation, assignment=replace(assignment, name=target))

    for parsed in _static_eval_commands(command, limits=limits):
        if parsed.execution_status is False:
            continue
        if parsed.array_compound:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-array.compound-assignment",
                    "shell eval array assignment cannot be represented",
                )
            )
        nested_eval_executable = _static_eval_executable(parsed, limits=limits)
        if (
            command.execution_status is not False
            and nested_eval_executable is not None
            and nested_eval_executable.name == "eval"
            and not parsed.asynchronous
            and (
                nested_eval_executable.bypasses_functions
                or nested_eval_executable.name not in parsed.active_function_names
            )
        ):
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval.nested-eval-state", "shell nested eval state cannot be represented"
                )
            )
        words = parsed.words
        index = 0
        while index < len(words) and parsed.keyword_eligible[index]:
            word = words[index]
            if word != "!" and word not in _STATIC_EVAL_MUTATION_PREFIXES:
                break
            index += 1
            if word == "time":
                while index < len(words) and words[index] == "-p":
                    index += 1
        prefix_assignments: list[_StaticEvalAssignment] = []
        while index < len(words):
            if _static_eval_element_assignment(words[index]):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-array.element-assignment-operand",
                        "shell eval array assignment cannot be represented",
                    )
                )
            assignment = _static_assignment_word(words[index])
            if assignment is None:
                break
            prefix_assignments.append(
                _StaticEvalAssignment(
                    assignment,
                    eval_content=_static_eval_assignment_content(
                        parsed.source_words[index], limits=limits
                    ),
                )
            )
            index += 1
        if index >= len(words):
            assignments.extend(route_assignment(item) for item in prefix_assignments)
            continue
        while index < len(words) and words[index] in {"builtin", "command"}:
            wrapper = words[index]
            index += 1
            if wrapper == "builtin":
                if index < len(words) and words[index] == "--":
                    index += 1
                continue
            while index < len(words):
                option = words[index]
                if option == "--":
                    index += 1
                    break
                if option.startswith("-") and option != "-":
                    index += 1
                    continue
                break
        if index >= len(words):
            continue
        executable = words[index]
        if executable in {"declare", "export", "local", "readonly", "typeset"}:
            if executable == "local" and command.function_context_id is None:
                continue
            options = tuple(
                word
                for word in words[index + 1 :]
                if word.startswith(("-", "+")) and word not in {"-", "+"}
            )
            if any(
                set(option[1:]) & _STATIC_EVAL_ARRAY_DECLARATION_FLAGS
                for option in options
                if option.startswith("-")
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-array.declaration-builtin",
                        "shell eval array assignment cannot be represented",
                    )
                )
            force_global = executable in {"declare", "typeset"} and any(
                option.startswith("-") and "g" in option[1:] for option in options
            )
            # Outside a function, `declare`/`typeset` (like a bare assignment) create a plain
            # global variable -- only inside a function do they shadow the caller with a local
            # one. `local` itself is already gated on function context above (it is a bash error
            # otherwise, so no mutation is recorded at all); mirror that gate here so `declare`/
            # `typeset` at the top level are not mislabeled local (issue #117 follow-up).
            local = (
                executable in {"declare", "local", "typeset"}
                and not force_global
                and command.function_context_id is not None
            )
            nameref_action: bool | None = None
            for option in options:
                if "n" in option[1:] and executable in {"declare", "local", "typeset"}:
                    nameref_action = option.startswith("-")
            for word_index in range(index + 1, len(words)):
                word = words[word_index]
                if _static_eval_element_assignment(word):
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.eval-array.element-assignment-word",
                            "shell eval array assignment cannot be represented",
                        )
                    )
                assignment = _static_assignment_word(word)
                if assignment is not None:
                    if nameref_action is False:
                        aliases.pop(assignment.name, None)
                    if nameref_action:
                        target = (
                            assignment.content.text
                            if isinstance(assignment.content, LiteralTransfer)
                            else ""
                        )
                        if not _static_variable_name(target):
                            raise _TaintLimitExceeded(
                                GuardRefusal(
                                    "taint.eval-nameref.non-static-target",
                                    "shell eval nameref target cannot be represented",
                                )
                            )
                        assignment = replace(
                            assignment,
                            content=VariableRef(target),
                            nameref_target=target,
                        )
                    mutation = _StaticEvalAssignment(
                        assignment,
                        local=local,
                        force_global=force_global,
                        eval_content=_static_eval_assignment_content(
                            parsed.source_words[word_index], limits=limits
                        ),
                    )
                    assignments.append(route_assignment(mutation))
                elif (
                    nameref_action is False
                    and _static_variable_name(word)
                    and not word.startswith(("-", "+"))
                ):
                    aliases.pop(word, None)
        elif executable == "unset":
            operands = words[index + 1 :]
            flags = ""
            operand_start = 0
            for option in operands:
                if option == "--":
                    operand_start += 1
                    break
                if not option.startswith("-") or option == "-":
                    break
                flags += option[1:]
                operand_start += 1
            if "f" in flags and "v" not in flags:
                # `unset -f` removes only a function; the variable survives.
                continue
            if "n" in flags:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-nameref.unset-with-flag",
                        "shell eval nameref unset cannot be represented",
                    )
                )
            unsets.extend(word for word in operands[operand_start:] if _static_variable_name(word))
    return tuple(assignments), tuple(dict.fromkeys(unsets))


def _eval_assignment_transfers(
    command: _CommandEvidence,
) -> tuple[_StaticEvalAssignment, ...]:
    """Retain one dynamic eval assignment as a caller-state transfer."""
    transfers: list[_StaticEvalAssignment] = []
    for executable in _builtin_eval_candidates(command):
        if executable.argv_index is None:
            continue
        arguments = command.argv[executable.argv_index + 1 :]
        if len(arguments) != 1 or not isinstance(arguments[0].content, Concat):
            continue
        parts = arguments[0].content.parts
        if not parts or not isinstance(parts[0], LiteralTransfer):
            continue
        name, separator, literal_value = parts[0].text.partition("=")
        append = name.endswith("+")
        if append:
            name = name[:-1]
        if not separator or not _static_variable_name(name):
            continue
        transfers.append(
            _StaticEvalAssignment(
                _AssignmentEvidence(
                    name,
                    concat(LiteralTransfer(literal_value), *parts[1:]),
                    append=append,
                )
            )
        )
    return tuple(transfers)


def _static_eval_command_names(
    command: _CommandEvidence,
    *,
    limits: TaintLimits,
) -> tuple[str, ...]:
    """Return exact simple-command heads executed by bounded static eval input."""
    names: list[str] = []
    for parsed in _static_eval_commands(command, limits=limits):
        executable = _static_eval_executable(parsed, limits=limits)
        if executable is not None and not executable.bypasses_functions:
            names.append(executable.name)
    return tuple(dict.fromkeys(names))


def _unset_function_names(command: _CommandEvidence) -> tuple[str, ...]:
    """Return exact function names removed by a literal ``unset -f`` command."""
    executable_index = _builtin_executable_index(command, "unset")
    if executable_index is None:
        return ()
    function_only = False
    options_enabled = True
    names: list[str] = []
    for argument in command.argv[executable_index + 1 :]:
        if options_enabled and argument.literal == "--" and not argument.dynamic:
            options_enabled = False
            continue
        if (
            options_enabled
            and not argument.dynamic
            and argument.literal.startswith("-")
            and argument.literal != "-"
        ):
            flags = argument.literal[1:]
            function_only = "f" in flags and "v" not in flags
            continue
        if function_only and not argument.dynamic:
            names.append(argument.literal)
    return tuple(names)


def _contextualize_evidence(  # noqa: PLR0912, PLR0915
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits,
) -> _ShellTaintEvidence:
    """Bind command-local first-pass variable reads before solving global flow tables."""
    if not evidence.scopes:
        return evidence
    evidence = replace(
        evidence,
        commands=tuple(
            replace(
                command,
                builtin_assignments=(),
                builtin_unsets=(),
                builtin_local=False,
                builtin_force_global=False,
                builtin_dynamic_options=False,
                unknown_builtin_content=None,
                unsupported_builtin_write=False,
            )
            if command.function_context_id is None
            and _builtin_executable_index(command, "local") is not None
            else command
            for command in evidence.commands
        ),
    )
    contextualization_edges = 0

    def charge_edges(amount: int = 1) -> None:
        nonlocal contextualization_edges
        contextualization_edges += amount
        if contextualization_edges > limits.max_edges:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.contextualization.edge-limit", "shell taint edge limit exceeded"
                )
            )

    environments, _parents = _scope_environment_ids(evidence.scopes)
    command_environments, execution_parents, _lastpipe = _execution_environment_ids(evidence)
    exact_values_by_environment: dict[tuple[int | None, int], dict[str, str]] = {}
    exact_values_before: dict[int, dict[str, str]] = {}

    def exact_state_for(context: int | None, environment: int) -> dict[str, str]:
        key = (context, environment)
        cached = exact_values_by_environment.get(key)
        if cached is not None:
            return cached
        parent = execution_parents.get(environment)
        inherited = dict(exact_state_for(context, parent)) if parent is not None else {}
        charge_edges(len(inherited))
        exact_values_by_environment[key] = inherited
        return inherited

    def exact_literal(expression: ContentExpr, values: Mapping[str, str]) -> str | None:
        memo: dict[int, str | None] = {}
        scheduled: set[int] = set()
        pending = [(expression, False)]
        while pending:
            current, expanded = pending.pop()
            current_id = id(current)
            if current_id in memo:
                continue
            if not expanded:
                if current_id in scheduled:
                    continue
                scheduled.add(current_id)
                charge_edges()
                pending.append((current, True))
                if isinstance(current, Choice | Concat):
                    pending.extend((part, False) for part in reversed(current.parts))
                continue
            if isinstance(current, LiteralTransfer):
                value: str | None = current.text
            elif isinstance(current, VariableRef):
                value = values.get(_unscoped_variable_name(current.name))
            elif isinstance(current, Concat):
                parts = tuple(memo.get(id(part)) for part in current.parts)
                value = (
                    None
                    if any(part is None for part in parts)
                    else "".join(part for part in parts if part is not None)
                )
            elif isinstance(current, Choice):
                alternatives = {memo.get(id(part)) for part in current.parts}
                value = alternatives.pop() if len(alternatives) == 1 else None
            else:
                value = None
            memo[current_id] = value
        return memo[id(expression)]

    def apply_exact_assignment(
        assignment: _AssignmentEvidence,
        values: dict[str, str],
        *,
        conditional: bool,
    ) -> None:
        name = _unscoped_variable_name(assignment.name)
        if conditional:
            values.pop(name, None)
            return
        value = exact_literal(assignment.content, values)
        if (
            assignment.conditional
            and name in values
            and (not assignment.assign_if_null or values[name])
        ):
            return
        if value is None:
            values.pop(name, None)
        elif assignment.append:
            prior = values.get(name)
            if prior is None:
                values.pop(name, None)
            else:
                values[name] = prior + value
        else:
            values[name] = value

    def exact_eval_program(
        command: _CommandEvidence,
        values: Mapping[str, str],
    ) -> str | None:
        for executable in _builtin_eval_candidates(command):
            if executable.argv_index is None:
                continue
            arguments: list[str] = []
            for argument in _eval_argument_ports(command, executable.argv_index):
                value = exact_literal(argument.content, values)
                if value is None:
                    break
                arguments.append(value)
            else:
                return " ".join(arguments)
        return None

    # Only a call site reads the lexical snapshot, and a call site needs a defined function, so
    # a body without one never pays for the per-command copy.
    defines_functions = any(
        command.defines_function_context_id is not None for command in evidence.commands
    )
    resolved_commands: list[_CommandEvidence] = []
    for command in evidence.commands:
        context = command.function_context_id
        environment = command_environments[command.command_id]
        exact_values = exact_state_for(context, environment)
        if defines_functions:
            exact_values_before[command.command_id] = dict(exact_values)
            charge_edges(len(exact_values))
        eval_candidates = bool(_builtin_eval_candidates(command))
        # The eval view only differs from the lexical state when this command carries its own
        # definite assignments, so the copy is owed only then.
        if eval_candidates and command.definite_assignments:
            eval_values = dict(exact_values)
            charge_edges(len(eval_values))
            for assignment in command.definite_assignments:
                apply_exact_assignment(assignment, eval_values, conditional=False)
        else:
            eval_values = exact_values

        resolved_program = exact_eval_program(command, eval_values) if eval_candidates else None
        resolved = (
            replace(command, resolved_eval_program=resolved_program)
            if resolved_program is not None
            else command
        )

        status = (
            None
            if context is not None
            and command.function_effect_conditional
            and command.execution_status is True
            else command.execution_status
        )
        eval_assignments, eval_unsets = (
            _static_eval_mutations(resolved, limits=limits) if status is not False else ((), ())
        )
        # issue #117: a recovered eval assignment used to feed only this command's own
        # exact-literal table below, so it reached AD-18's "a later sink observes it" promise
        # only for another eval reading that table. `_build_flow_definitions` lowers
        # `resolved.assignments` for every sink, so threading the plain (non-local,
        # non-nameref-aliasing) mutations onto it puts an eval-replayed assignment on the same
        # footing as an authored one. Local declarations and nameref aliasing stay on the
        # exact-table-only path -- routing them soundly needs function-scope and alias-target
        # bookkeeping this lowering does not have, so leaving them alone extends a pre-existing,
        # narrower gap rather than introducing a new one.
        lowered_eval_assignments = tuple(
            mutation.assignment
            for mutation in eval_assignments
            if not mutation.local
            and mutation.assignment.nameref_target is None
            and not mutation.assignment.nameref_unset
        )
        if lowered_eval_assignments:
            resolved = replace(
                resolved, assignments=(*resolved.assignments, *lowered_eval_assignments)
            )
        resolved_commands.append(resolved)

        if status is False:
            continue
        conditional = status is None
        assignment_only = not command.argv or command.executable.argv_index is None
        assignments = (
            (*command.definite_assignments, *command.builtin_assignments)
            if assignment_only
            else command.builtin_assignments
        )
        for assignment in (
            *assignments,
            *(mutation.assignment for mutation in eval_assignments),
        ):
            apply_exact_assignment(assignment, exact_values, conditional=conditional)
        unset_names, unknown_unset = _unset_action(command)
        if unknown_unset or command.unknown_builtin_content is not None:
            exact_values.clear()
        for name in (*unset_names, *eval_unsets):
            exact_values.pop(name, None)

    evidence = replace(evidence, commands=tuple(resolved_commands))

    conditional_execution = {
        command.command_id: (
            None
            if command.execution_status is True
            and command.function_context_id is not None
            and command.function_effect_conditional
            else command.execution_status
        )
        for command in evidence.commands
    }

    active_locals: dict[int, set[str]] = {}
    definitely_set_locals: dict[int, set[str]] = {}
    command_locals: dict[int, frozenset[str]] = {}
    command_set_locals: dict[int, frozenset[str]] = {}
    for command in evidence.commands:
        context = command.function_context_id
        names = active_locals.setdefault(context, set()) if context is not None else set()
        set_names = (
            definitely_set_locals.setdefault(context, set()) if context is not None else set()
        )
        command_locals[command.command_id] = frozenset(names)
        command_set_locals[command.command_id] = frozenset(set_names)
        if context is not None and command.builtin_local:
            names.update(assignment.name for assignment in command.builtin_assignments)
            names.update(command.builtin_unsets)
            set_names.update(assignment.name for assignment in command.builtin_assignments)
            set_names.difference_update(command.builtin_unsets)
        if context is not None:
            set_names.update(
                _unscoped_variable_name(assignment.name)
                for assignment in command.definite_assignments
                if _unscoped_variable_name(assignment.name) in names
            )
            set_names.update(
                _unscoped_variable_name(assignment.name)
                for assignment in command.builtin_assignments
                if _unscoped_variable_name(assignment.name) in names
            )
            unset_names, unknown_unset = _unset_action(command)
            set_names.difference_update(name for name in unset_names if name in names)
            if unknown_unset:
                set_names.clear()

    active_definitions_by_environment: dict[int, dict[str, int]] = {}
    active_definitions_before: dict[int, dict[str, int]] = {}
    scope_kinds = {scope.scope_id: scope.kind for scope in evidence.scopes}
    scope_parents = {scope.scope_id: scope.parent_scope_id for scope in evidence.scopes}
    ambiguous_definition_scope_ancestor: dict[int, bool] = {}

    def registers_within_ambiguous_definition_scope(container_scope_id: int) -> bool:
        """Return whether a command's scope has a repeating, ``case``, or ``if`` ancestor."""
        cached = ambiguous_definition_scope_ancestor.get(container_scope_id)
        if cached is not None:
            return cached
        visited: set[int] = set()
        current: int | None = container_scope_id
        result = False
        while current is not None and current not in visited:
            visited.add(current)
            if scope_kinds.get(current) in _AMBIGUOUS_DEFINITION_SCOPE_KINDS:
                result = True
                break
            current = scope_parents.get(current)
        charge_edges(len(visited))
        ambiguous_definition_scope_ancestor[container_scope_id] = result
        return result

    def active_definitions_for(environment: int) -> dict[str, int]:
        cached = active_definitions_by_environment.get(environment)
        if cached is not None:
            return cached
        parent = execution_parents.get(environment)
        inherited = dict(active_definitions_for(parent)) if parent is not None else {}
        active_definitions_by_environment[environment] = inherited
        return inherited

    for command in evidence.commands:
        environment = command_environments[command.command_id]
        active_definitions = active_definitions_for(environment)
        active_definitions_before[command.command_id] = dict(active_definitions)
        charge_edges(len(active_definitions))
        status = conditional_execution[command.command_id]
        if status is True:
            if (
                command.defines_function_name is not None
                and command.defines_function_context_id is not None
            ):
                active_definitions[command.defines_function_name] = (
                    command.defines_function_context_id
                )
            for name in _unset_function_names(command):
                active_definitions.pop(name, None)
        elif status is None and registers_within_ambiguous_definition_scope(
            command.container_scope_id
        ):
            # Ambiguous status means the body may or may not run, so an unset or redefinition
            # here may or may not take effect -- popping or overwriting on that uncertainty would
            # be fail-open. Only adding a definition that was not already active is safe: a call
            # site can always reach this definition (the body ran) even when it cannot prove the
            # body ran, and an existing definition survives regardless of whether this ambiguous
            # body executed.
            if (
                command.defines_function_name is not None
                and command.defines_function_context_id is not None
            ):
                active_definitions.setdefault(
                    command.defines_function_name,
                    command.defines_function_context_id,
                )

    definitions_by_name: dict[str, list[tuple[int, int]]] = {}
    for command in evidence.commands:
        if (
            command.defines_function_name is not None
            and command.defines_function_context_id is not None
        ):
            definitions_by_name.setdefault(command.defines_function_name, []).append(
                (command.command_id, command.defines_function_context_id)
            )
    function_contexts = {
        context
        for command in evidence.commands
        for context in (command.defines_function_context_id,)
        if context is not None
    }
    direct_callers: dict[int, set[int]] = {context: set() for context in function_contexts}
    call_commands: list[tuple[_CommandEvidence, int]] = []

    def called_names(command: _CommandEvidence) -> tuple[str, ...]:
        if command.defines_function_context_id is not None:
            return ()
        names: list[str] = []
        if command.executable.name is not None:
            names.append(command.executable.name)
        names.extend(
            name
            for name in _static_eval_command_names(command, limits=limits)
            if name in definitions_by_name
        )
        return tuple(dict.fromkeys(names))

    def callees_for(command: _CommandEvidence) -> tuple[int, ...]:
        callees: list[int] = []
        for name in called_names(command):
            definitions = definitions_by_name.get(name, ())
            if command.function_context_id is None:
                active = active_definitions_before[command.command_id].get(name)
                if active is not None:
                    callees.append(active)
            else:
                callees.extend(context for _definition_id, context in definitions)
            charge_edges(len(definitions))
        return tuple(dict.fromkeys(callees))

    for command in evidence.commands:
        for callee in callees_for(command):
            charge_edges()
            call_commands.append((command, callee))
            if command.function_context_id is not None:
                direct_callers.setdefault(callee, set()).add(command.function_context_id)

    preliminary_call_sites: dict[int, list[_CommandEvidence]] = {}
    reachable_contexts: set[int] = set()
    changed = True
    while changed:
        changed = False
        for command, callee in call_commands:
            caller = command.function_context_id
            if (
                conditional_execution[command.command_id] is False
                or (caller is not None and caller not in reachable_contexts)
                or callee in reachable_contexts
            ):
                continue
            reachable_contexts.add(callee)
            charge_edges()
            changed = True
    for command, callee in call_commands:
        caller = command.function_context_id
        if (
            callee in reachable_contexts
            and conditional_execution[command.command_id] is not False
            and (caller is None or caller in reachable_contexts)
        ):
            preliminary_call_sites.setdefault(callee, []).append(command)

    commands_by_id = {command.command_id: command for command in evidence.commands}
    loop_binding_expressions_by_context: dict[int, list[ContentExpr]] = {}
    for scope in evidence.scopes:
        if scope.binding_command_id is None or not scope.loop_bindings:
            continue
        anchor = commands_by_id.get(scope.binding_command_id)
        if anchor is None or anchor.function_context_id is None:
            continue
        loop_binding_expressions_by_context.setdefault(anchor.function_context_id, []).extend(
            binding.content for binding in scope.loop_bindings
        )

    positional_references_by_context: dict[int, frozenset[str]] = {}
    for context in function_contexts:
        positional_names: set[str] = set()
        for command in evidence.commands:
            if command.function_context_id != context:
                continue
            expressions: list[ContentExpr] = [
                *(argument.content for argument in command.argv),
                *(assignment.content for assignment in command.assignments),
                *(assignment.content for assignment in command.definite_assignments),
                *(assignment.content for assignment in command.builtin_assignments),
            ]
            if command.unknown_builtin_content is not None:
                expressions.append(command.unknown_builtin_content)
            eval_assignments, _eval_unsets = _static_eval_mutations(command, limits=limits)
            expressions.extend(
                mutation.eval_content
                for mutation in eval_assignments
                if mutation.eval_content is not None
            )
            for expression in expressions:
                positional_names.update(_expression_function_positionals(expression))
        for expression in loop_binding_expressions_by_context.get(context, ()):
            positional_names.update(_expression_function_positionals(expression))
        positional_references_by_context[context] = frozenset(positional_names)

    function_names_by_context = {
        command.defines_function_context_id: command.defines_function_name
        for command in evidence.commands
        if command.defines_function_context_id is not None
        and command.defines_function_name is not None
    }

    def positional_set_operands(
        command: _CommandEvidence,
    ) -> tuple[tuple[_ArgPort, ...] | None, bool]:
        executable_index = _builtin_executable_index(command, "set")
        if executable_index is None:
            return None, False
        arguments = command.argv[executable_index + 1 :]
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument.dynamic:
                return None, True
            literal = argument.literal
            if literal in {"--", "-"}:
                operands = arguments[index + 1 :]
                return (operands if operands or literal == "--" else None), False
            if literal.startswith(("-", "+")) and literal not in {"-", "+"}:
                if "o" in literal[1:] and index + 1 < len(arguments):
                    index += 1
                    if arguments[index].dynamic:
                        return None, True
                index += 1
                continue
            return arguments[index:], False
        return None, False

    def positional_mutation_kind(command: _CommandEvidence) -> str | None:
        if _builtin_executable_index(command, "shift") is not None:
            return "shift"
        operands, unknown = positional_set_operands(command)
        if operands is not None or unknown:
            return "set"
        return None

    positional_mutating_contexts = {
        context
        for command in evidence.commands
        for context in (command.function_context_id,)
        if context is not None and positional_mutation_kind(command) is not None
    }

    def positional_call_arguments(
        command: _CommandEvidence,
        callee: int,
    ) -> tuple[_ArgPort, ...] | None:
        executable_index = command.executable.argv_index
        if executable_index is None or command.executable.name != function_names_by_context.get(
            callee
        ):
            return None
        return command.argv[executable_index + 1 :]

    def positional_call_contents(
        command: _CommandEvidence,
        callee: int,
    ) -> dict[str, ContentExpr]:
        names = positional_references_by_context.get(callee, ())
        if not names:
            return {}
        arguments = positional_call_arguments(command, callee)
        if arguments is None:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.unsupported-call-arguments",
                    "unsupported function positional argument",
                )
            )
        if any(argument.dynamic for argument in arguments):
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.dynamic-call-argument",
                    "dynamic function positional argument",
                )
            )
        joined = concat(
            *(
                part
                for index, argument in enumerate(arguments)
                for part in (
                    *((LiteralTransfer(" "),) if index else ()),
                    argument.content,
                )
            )
        )
        bindings: dict[str, ContentExpr] = {}
        for name in names:
            if name in {"@", "*", _QUOTED_FUNCTION_POSITIONAL_STAR}:
                bindings[name] = joined
                continue
            argument_index = _function_positional_index(name, len(arguments))
            bindings[name] = (
                arguments[argument_index].content
                if argument_index is not None
                else LiteralTransfer("")
            )
        charge_edges(len(bindings))
        return bindings

    def exact_positional_arguments(values: Mapping[str, str]) -> list[str]:
        arguments: list[str] = []
        index = 1
        while str(index) in values:
            arguments.append(values[str(index)])
            index += 1
        charge_edges(len(arguments))
        return arguments

    def refresh_exact_positionals(
        values: dict[str, str],
        context: int,
        *,
        default_ifs: bool,
    ) -> None:
        arguments = exact_positional_arguments(values)
        joined = " ".join(arguments)
        values["@"] = joined
        values["*"] = joined
        if _QUOTED_FUNCTION_POSITIONAL_STAR not in positional_references_by_context.get(
            context,
            (),
        ):
            return
        if "IFS" not in values:
            if not default_ifs:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-positional.dynamic-ifs", "dynamic function positional IFS"
                    )
                )
            values["IFS"] = " "
        values[_QUOTED_FUNCTION_POSITIONAL_STAR] = values["IFS"][:1].join(arguments)

    def replace_exact_positionals(
        values: dict[str, str],
        arguments: list[str],
        context: int,
    ) -> None:
        for name in tuple(values):
            if _is_function_positional_parameter(name):
                del values[name]
        values.update((str(index), value) for index, value in enumerate(arguments, start=1))
        refresh_exact_positionals(values, context, default_ifs=True)

    def bind_exact_positionals(
        command: _CommandEvidence,
        callee: int,
        values: dict[str, str],
    ) -> None:
        if not positional_references_by_context.get(callee):
            return
        arguments = positional_call_arguments(command, callee)
        if arguments is None:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.unsupported-bind-arguments",
                    "unsupported function positional argument",
                )
            )
        if any(argument.dynamic for argument in arguments):
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.dynamic-bind-argument",
                    "dynamic function positional argument",
                )
            )
        exact_arguments: list[str] = []
        for argument in arguments:
            value = exact_literal(argument.content, values)
            if value is None:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-positional.unresolved-bind-value",
                        "dynamic function positional argument",
                    )
                )
            exact_arguments.append(value)
        replace_exact_positionals(values, exact_arguments, callee)

    def apply_exact_positional_mutation(
        command: _CommandEvidence,
        values: dict[str, str],
        context: int,
    ) -> None:
        kind = positional_mutation_kind(command)
        if kind is None:
            return
        executable_index = _builtin_executable_index(command, kind)
        if executable_index is None:
            return
        arguments = exact_positional_arguments(values)
        operands = command.argv[executable_index + 1 :]
        if kind == "shift":
            if len(operands) > 1 or any(operand.dynamic for operand in operands):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-positional.exact-shift-dynamic-operand",
                        "dynamic function positional mutation",
                    )
                )
            amount_text = operands[0].literal if operands else "1"
            if not amount_text.isdigit():
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-positional.exact-shift-non-numeric",
                        "dynamic function positional mutation",
                    )
                )
            digits = amount_text.lstrip("0") or "0"
            maximum = str(len(arguments))
            if len(digits) > len(maximum) or (len(digits) == len(maximum) and digits > maximum):
                return
            amount = int(digits)
            replace_exact_positionals(values, arguments[amount:], context)
            return
        set_operands, unknown = positional_set_operands(command)
        if unknown or set_operands is None:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.function-positional.exact-set-unknown-operands",
                    "dynamic function positional mutation",
                )
            )
        exact_arguments = []
        for operand in set_operands:
            if operand.dynamic:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-positional.exact-set-dynamic-operand",
                        "dynamic function positional mutation",
                    )
                )
            value = exact_literal(operand.content, values)
            if value is None:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-positional.exact-set-unresolved-value",
                        "dynamic function positional mutation",
                    )
                )
            exact_arguments.append(value)
        replace_exact_positionals(values, exact_arguments, context)

    def exact_call_values(
        command: _CommandEvidence,
        caller_values: Mapping[str, str],
        callee: int,
    ) -> dict[str, str]:
        context = command.function_context_id
        values = dict(caller_values) if context is not None else {}
        lexical = exact_values_before[command.command_id]
        for name in command_locals[command.command_id]:
            if name not in lexical:
                values.pop(name, None)
        values.update(lexical)
        for assignment in command.definite_assignments:
            apply_exact_assignment(assignment, values, conditional=False)
        bind_exact_positionals(command, callee, values)
        charge_edges(len(values))
        return values

    def exact_variant_identity(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(values.items()))

    exact_entry_variants: dict[int, tuple[dict[str, str], ...]] = dict.fromkeys(
        function_contexts,
        (),
    )
    exact_entry_updates = 0
    changed = True
    while changed:
        changed = False
        for context in reachable_contexts:
            variants = {
                exact_variant_identity(values): dict(values)
                for values in exact_entry_variants[context]
            }
            for command in preliminary_call_sites.get(context, ()):
                caller = command.function_context_id
                caller_variants = ({},) if caller is None else exact_entry_variants.get(caller, ())
                for caller_values in caller_variants:
                    values = exact_call_values(command, caller_values, context)
                    variants.setdefault(exact_variant_identity(values), values)
            if len(variants) > limits.max_alternatives:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.contextualization.entry-alternative-limit",
                        "shell taint alternative limit exceeded",
                    )
                )
            candidate = tuple(variants.values())
            if tuple(map(exact_variant_identity, candidate)) == tuple(
                map(exact_variant_identity, exact_entry_variants[context])
            ):
                continue
            exact_entry_variants[context] = candidate
            exact_entry_updates += 1
            if exact_entry_updates > limits.max_fixed_point_updates:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.contextualization.entry-fixed-point-limit",
                        "shell taint contextualization fixed-point update limit exceeded",
                    )
                )
            changed = True

    exact_function_base_environments = {
        command.defines_function_context_id: command_environments[command.command_id]
        for command in evidence.commands
        if command.defines_function_context_id is not None
    }

    exact_effects: dict[int, tuple[_ExactFunctionEffect, ...]] = dict.fromkeys(
        function_contexts,
        (),
    )
    exact_effect_unknown: set[int] = set()

    exact_effect_updates = 0
    changed = True
    while changed:
        changed = False
        for context in function_contexts:
            effects: list[_ExactFunctionEffect] = []
            local_values: dict[str, str] = {}
            local_contents: dict[str, ContentExpr] = {}
            effect_positional_arguments: tuple[ContentExpr, ...] | None = None
            effect_shift_offset = 0
            unknown = False

            def effect_content(
                expression: ContentExpr,
                positional_arguments: tuple[ContentExpr, ...] | None,
                shift_offset: int,
            ) -> ContentExpr:
                return _substitute_function_positionals(
                    expression,
                    positional_arguments,
                    shift_offset,
                )

            def apply_effect_positional_mutation(
                command: _CommandEvidence,
                *,
                effect_context: int = context,
            ) -> None:
                nonlocal effect_positional_arguments, effect_shift_offset
                kind = positional_mutation_kind(command)
                if kind is None:
                    return
                executable_index = _builtin_executable_index(command, kind)
                if executable_index is None:
                    return
                operands = command.argv[executable_index + 1 :]
                if kind == "shift":
                    if len(operands) > 1 or any(operand.dynamic for operand in operands):
                        raise _TaintLimitExceeded(
                            GuardRefusal(
                                "taint.function-positional.effect-shift-dynamic-operand",
                                "dynamic function positional mutation",
                            )
                        )
                    amount_text = operands[0].literal if operands else "1"
                    if not amount_text.isdigit() or len(amount_text) > _MAX_BRACE_INTEGER_DIGITS:
                        raise _TaintLimitExceeded(
                            GuardRefusal(
                                "taint.function-positional.effect-shift-non-numeric",
                                "dynamic function positional mutation",
                            )
                        )
                    amount = int(amount_text)
                    if effect_positional_arguments is None:
                        argument_counts: set[int] = set()
                        for call in preliminary_call_sites.get(effect_context, ()):
                            arguments = positional_call_arguments(call, effect_context)
                            if arguments is None or any(argument.dynamic for argument in arguments):
                                raise _TaintLimitExceeded(
                                    GuardRefusal(
                                        "taint.function-positional.effect-shift-unknown-arguments",
                                        "dynamic function positional mutation",
                                    )
                                )
                            argument_counts.add(len(arguments))
                        if argument_counts and all(
                            count >= effect_shift_offset + amount for count in argument_counts
                        ):
                            effect_shift_offset += amount
                        elif any(
                            count >= effect_shift_offset + amount for count in argument_counts
                        ):
                            raise _TaintLimitExceeded(
                                GuardRefusal(
                                    "taint.function-positional.effect-shift-underflow",
                                    "dynamic function positional mutation",
                                )
                            )
                    elif amount <= len(effect_positional_arguments):
                        effect_positional_arguments = effect_positional_arguments[amount:]
                    return
                set_operands, dynamic = positional_set_operands(command)
                if dynamic or set_operands is None:
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.function-positional.effect-set-unknown-operands",
                            "dynamic function positional mutation",
                        )
                    )
                effect_positional_arguments = tuple(operand.content for operand in set_operands)
                effect_shift_offset = 0

            def apply_local_assignment(
                assignment: _AssignmentEvidence,
                exact_values: dict[str, str],
                contents: dict[str, ContentExpr],
                *,
                conditional: bool,
            ) -> None:
                name = _unscoped_variable_name(assignment.name)
                content = _substitute_local_contents(
                    assignment.content,
                    contents,
                    limits,
                )
                resolved = replace(assignment, content=content)
                if conditional:
                    contents.pop(name, None)
                elif assignment.append:
                    prior = contents.get(name)
                    contents[name] = concat(prior, content) if prior is not None else content
                else:
                    contents[name] = content
                apply_exact_assignment(
                    resolved,
                    exact_values,
                    conditional=conditional,
                )

            for command in evidence.commands:
                if (
                    command.function_context_id != context
                    or command_environments[command.command_id]
                    != exact_function_base_environments.get(context)
                    or conditional_execution[command.command_id] is False
                ):
                    continue
                status = conditional_execution[command.command_id]
                assignment_only = not command.argv or command.executable.argv_index is None
                assignments = (
                    *(
                        (assignment, False)
                        for assignment in command.definite_assignments
                        if assignment_only
                    ),
                    *(
                        (
                            assignment,
                            command.builtin_local and not command.builtin_force_global,
                        )
                        for assignment in command.builtin_assignments
                    ),
                )
                eval_assignments, eval_unsets = _static_eval_mutations(command, limits=limits)
                eval_assignments = (*eval_assignments, *_eval_assignment_transfers(command))
                for assignment, local in assignments:
                    resolved_effect_assignment = replace(
                        assignment,
                        content=effect_content(
                            assignment.content,
                            effect_positional_arguments,
                            effect_shift_offset,
                        ),
                    )
                    name = _unscoped_variable_name(resolved_effect_assignment.name)
                    if local or name in command_locals[command.command_id]:
                        apply_local_assignment(
                            resolved_effect_assignment,
                            local_values,
                            local_contents,
                            conditional=status is None,
                        )
                    else:
                        resolved_assignment = replace(
                            resolved_effect_assignment,
                            content=_substitute_local_contents(
                                resolved_effect_assignment.content,
                                local_contents,
                                limits,
                            ),
                        )
                        value = exact_literal(resolved_assignment.content, local_values)
                        effects.append(
                            _ExactFunctionEffect(
                                assignment=(
                                    replace(
                                        resolved_assignment,
                                        content=LiteralTransfer(value),
                                    )
                                    if value is not None
                                    else resolved_assignment
                                ),
                                optional=status is None,
                            )
                        )
                for mutation in eval_assignments:
                    name = _unscoped_variable_name(mutation.assignment.name)
                    local = not mutation.force_global and (
                        mutation.local or name in command_locals[command.command_id]
                    )
                    assignment = (
                        replace(
                            mutation.assignment,
                            content=effect_content(
                                mutation.eval_content,
                                effect_positional_arguments,
                                effect_shift_offset,
                            ),
                        )
                        if mutation.eval_content is not None
                        else replace(
                            mutation.assignment,
                            content=effect_content(
                                mutation.assignment.content,
                                effect_positional_arguments,
                                effect_shift_offset,
                            ),
                        )
                    )
                    if local:
                        apply_local_assignment(
                            assignment,
                            local_values,
                            local_contents,
                            conditional=status is None,
                        )
                    else:
                        assignment = replace(
                            assignment,
                            content=_substitute_local_contents(
                                assignment.content,
                                local_contents,
                                limits,
                            ),
                        )
                        value = exact_literal(assignment.content, local_values)
                        effects.append(
                            _ExactFunctionEffect(
                                assignment=(
                                    replace(
                                        assignment,
                                        content=LiteralTransfer(value),
                                    )
                                    if value is not None
                                    else assignment
                                ),
                                optional=status is None,
                            )
                        )
                unset_names, unknown_unset = _unset_action(command)
                for name in (*unset_names, *eval_unsets):
                    if name in command_locals[command.command_id]:
                        local_values.pop(name, None)
                        local_contents.pop(name, None)
                    else:
                        effects.append(
                            _ExactFunctionEffect(
                                unset_name=name,
                                optional=status is None,
                            )
                        )
                for name in command.builtin_unsets:
                    local_values.pop(name, None)
                    local_contents.pop(name, None)
                unknown = unknown or unknown_unset or command.unknown_builtin_content is not None
                for callee in callees_for(command):
                    if callee == context:
                        unknown = True
                    else:
                        callee_values = dict(local_values)
                        for assignment in command.definite_assignments:
                            resolved_assignment = replace(
                                assignment,
                                content=_substitute_local_contents(
                                    effect_content(
                                        assignment.content,
                                        effect_positional_arguments,
                                        effect_shift_offset,
                                    ),
                                    local_contents,
                                    limits,
                                ),
                            )
                            apply_exact_assignment(
                                resolved_assignment,
                                callee_values,
                                conditional=False,
                            )
                        bind_exact_positionals(command, callee, callee_values)
                        for effect in exact_effects.get(callee, ()):
                            assignment = effect.assignment
                            if assignment is not None:
                                assignment = replace(
                                    assignment,
                                    content=_substitute_local_contents(
                                        assignment.content,
                                        local_contents,
                                        limits,
                                    ),
                                )
                                value = exact_literal(assignment.content, callee_values)
                                if value is not None:
                                    assignment = replace(
                                        assignment,
                                        content=LiteralTransfer(value),
                                    )
                                apply_exact_assignment(
                                    assignment,
                                    callee_values,
                                    conditional=effect.optional,
                                )
                            elif effect.unset_name is not None:
                                callee_values.pop(effect.unset_name, None)
                            effects.append(
                                replace(effect, assignment=assignment)
                                if assignment is not effect.assignment
                                else effect
                            )
                        unknown = unknown or callee in exact_effect_unknown
                apply_effect_positional_mutation(command)
                if len(effects) > limits.max_edges:
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.contextualization.effect-edge-limit",
                            "shell taint edge limit exceeded",
                        )
                    )
            frozen_effects = tuple(effects)
            if frozen_effects != exact_effects[context]:
                exact_effects[context] = frozen_effects
                exact_effect_updates += 1
                changed = True
            if unknown and context not in exact_effect_unknown:
                exact_effect_unknown.add(context)
                exact_effect_updates += 1
                changed = True
            if exact_effect_updates > limits.max_fixed_point_updates:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.contextualization.effect-fixed-point-limit",
                        "shell taint contextualization fixed-point update limit exceeded",
                    )
                )

    call_time_values = {
        context: [dict(values) for values in variants]
        for context, variants in exact_entry_variants.items()
    }
    exact_positionals_before: dict[int, dict[str, set[str]]] = {}
    call_time_resolved_commands: list[_CommandEvidence] = []
    for command in evidence.commands:
        context = command.function_context_id
        if context is None:
            # Positional binding and ``set``/``shift`` mutation are modeled for function contexts
            # only. A body that rewrites ``$@`` at top level would otherwise leave ``$1`` external
            # and non-authored, so `set -- "$M"; "$1"` has to fail closed the same way the
            # function form already does.
            if positional_mutation_kind(command) is not None:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.top-level-positional.dynamic-mutation",
                        "dynamic top-level positional mutation",
                    )
                )
            call_time_resolved_commands.append(command)
            continue
        states = call_time_values.setdefault(context, [])
        programs: set[str] = set()
        unresolved_program = False
        next_states: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
        for values in states:
            eval_values = dict(values)
            for assignment in command.definite_assignments:
                apply_exact_assignment(assignment, eval_values, conditional=False)
            refresh_exact_positionals(
                eval_values,
                context,
                default_ifs=False,
            )
            positional_values = exact_positionals_before.setdefault(
                command.command_id,
                {},
            )
            for name, value in eval_values.items():
                if _is_function_positional_parameter(name):
                    positional_values.setdefault(name, set()).add(value)
                    charge_edges()
            charge_edges(len(eval_values))
            program = exact_eval_program(command, eval_values)
            if program is not None:
                programs.add(program)
            elif _builtin_eval_candidates(command):
                # One entry variant resolved exactly and another did not. Publishing only the
                # resolved ones makes the set look authoritative downstream, which drops the
                # unresolved (and possibly tainted) payload instead of falling back to the raw
                # eval arguments.
                unresolved_program = True
            resolved_variant = (
                replace(
                    command,
                    resolved_eval_program=program,
                    resolved_eval_programs=(),
                )
                if program is not None
                else command
            )

            updated = dict(values)
            if (
                command_environments[command.command_id]
                == exact_function_base_environments.get(context)
                and conditional_execution[command.command_id] is not False
            ):
                status = conditional_execution[command.command_id]
                assignment_only = not command.argv or command.executable.argv_index is None
                assignments = (
                    (*command.definite_assignments, *command.builtin_assignments)
                    if assignment_only
                    else command.builtin_assignments
                )
                eval_assignments, eval_unsets = _static_eval_mutations(
                    resolved_variant, limits=limits
                )
                for assignment in (
                    *assignments,
                    *(mutation.assignment for mutation in eval_assignments),
                ):
                    apply_exact_assignment(assignment, updated, conditional=status is None)
                for name in command.builtin_unsets:
                    updated.pop(name, None)
                    if (
                        name == "IFS"
                        and command.builtin_local
                        and _QUOTED_FUNCTION_POSITIONAL_STAR
                        in positional_references_by_context.get(context, ())
                    ):
                        updated["IFS"] = " "
                unset_names, unknown_unset = _unset_action(command)
                if unknown_unset or command.unknown_builtin_content is not None:
                    updated.clear()
                for name in (*unset_names, *eval_unsets):
                    updated.pop(name, None)
                    if (
                        name == "IFS"
                        and _QUOTED_FUNCTION_POSITIONAL_STAR
                        in positional_references_by_context.get(context, ())
                    ):
                        updated["IFS"] = " "
                for callee in callees_for(command):
                    if callee in exact_effect_unknown:
                        updated.clear()
                    callee_values = exact_call_values(command, updated, callee)
                    for effect in exact_effects.get(callee, ()):
                        assignment = effect.assignment
                        if assignment is not None:
                            value = exact_literal(assignment.content, callee_values)
                            if value is not None:
                                assignment = replace(
                                    assignment,
                                    content=LiteralTransfer(value),
                                )
                            apply_exact_assignment(
                                assignment,
                                updated,
                                conditional=effect.optional,
                            )
                            apply_exact_assignment(
                                assignment,
                                callee_values,
                                conditional=effect.optional,
                            )
                        elif effect.unset_name is not None:
                            updated.pop(effect.unset_name, None)
                            callee_values.pop(effect.unset_name, None)
                apply_exact_positional_mutation(command, updated, context)
            next_states.setdefault(exact_variant_identity(updated), updated)
        if len(next_states) > limits.max_alternatives:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.contextualization.state-alternative-limit",
                    "shell taint alternative limit exceeded",
                )
            )
        call_time_values[context] = list(next_states.values())
        if not programs:
            programs.update(_static_eval_programs(command))
        resolved = (
            replace(
                command,
                resolved_eval_program=None,
                resolved_eval_programs=tuple(sorted(programs)),
            )
            if programs and not unresolved_program
            else command
        )
        call_time_resolved_commands.append(resolved)

    evidence = replace(evidence, commands=tuple(call_time_resolved_commands))
    exact_positional_prefixes = {
        command_id: {
            name: tuple(LiteralTransfer(value) for value in sorted(values))
            for name, values in by_name.items()
        }
        for command_id, by_name in exact_positionals_before.items()
    }
    direct_callers = {context: set() for context in function_contexts}
    call_commands = []
    for command in evidence.commands:
        for callee in callees_for(command):
            charge_edges()
            call_commands.append((command, callee))
            if command.function_context_id is not None:
                direct_callers.setdefault(callee, set()).add(command.function_context_id)

    dynamic_callers = {context: set(callers) for context, callers in direct_callers.items()}
    changed = True
    while changed:
        changed = False
        for callers in dynamic_callers.values():
            inherited = {
                ancestor
                for caller in tuple(callers)
                for ancestor in dynamic_callers.get(caller, ())
            }
            if not inherited.issubset(callers):
                charge_edges(len(inherited - callers))
                callers.update(inherited)
                changed = True
    caller_locals_by_context: dict[int, dict[str, tuple[int, ...]]] = {}
    for context, callers in dynamic_callers.items():
        names = {name for caller in callers for name in active_locals.get(caller, ())}
        caller_locals_by_context[context] = {
            name: tuple(
                sorted(caller for caller in callers if name in active_locals.get(caller, ()))
            )
            for name in names
        }
        charge_edges(
            sum(
                len(callers_for_name)
                for callers_for_name in caller_locals_by_context[context].values()
            )
        )
    call_sites_by_context: dict[int, list[_CommandEvidence]] = {}
    for command, callee in call_commands:
        call_sites_by_context.setdefault(callee, []).append(command)

    function_base_environments = {
        command.defines_function_context_id: command_environments[command.command_id]
        for command in evidence.commands
        if command.defines_function_context_id is not None
    }

    def persistent_function_effect(command: _CommandEvidence) -> bool:
        context = command.function_context_id
        return (
            context is not None
            and conditional_execution[command.command_id] is not False
            and command_environments[command.command_id] == function_base_environments.get(context)
        )

    compound_bindings_by_command: dict[int, list[_AssignmentEvidence]] = {}
    for scope in evidence.scopes:
        if scope.binding_command_id is not None:
            compound_bindings_by_command.setdefault(scope.binding_command_id, []).extend(
                scope.loop_bindings
            )

    effect_names: dict[int, set[str]] = {context: set() for context in function_contexts}
    effect_unknown: set[int] = set()
    for command in evidence.commands:
        context = command.function_context_id
        if context is None or not persistent_function_effect(command):
            continue
        local_names = command_locals[command.command_id]
        assignment_only = not command.argv or command.executable.argv_index is None
        assignments = (
            (*command.definite_assignments, *command.builtin_assignments)
            if assignment_only
            else command.builtin_assignments
        )
        eval_assignments, eval_unsets = _static_eval_mutations(command, limits=limits)
        assignments = (
            *assignments,
            *command.assignments,
            *compound_bindings_by_command.get(command.command_id, ()),
            *(
                mutation.assignment
                for mutation in eval_assignments
                if not mutation.local or mutation.force_global
            ),
        )
        effect_names[context].update(
            _unscoped_variable_name(assignment.name)
            for assignment in assignments
            if _unscoped_variable_name(assignment.name) not in local_names
            and not command.builtin_local
        )
        unset_names, unknown_unset = _unset_action(command)
        effect_names[context].update(
            name for name in (*unset_names, *eval_unsets) if name not in local_names
        )
        if unknown_unset or command.unknown_builtin_content is not None:
            effect_unknown.add(context)

    changed = True
    while changed:
        changed = False
        for call, callee in call_commands:
            caller = call.function_context_id
            if caller is None or not persistent_function_effect(call):
                continue
            inherited_names = effect_names.get(callee, ())
            missing = set(inherited_names) - effect_names.setdefault(caller, set())
            if missing:
                charge_edges(len(missing))
                effect_names[caller].update(missing)
                changed = True
            if callee in effect_unknown and caller not in effect_unknown:
                effect_unknown.add(caller)
                changed = True

    global_values_by_environment: dict[
        int,
        dict[str, tuple[_CommandEvidence, ContentExpr]],
    ] = {}
    global_set_by_environment: dict[int, set[str]] = {}
    global_unset_by_environment: dict[int, set[str]] = {}
    global_values_before: dict[
        int,
        dict[str, tuple[_CommandEvidence, ContentExpr]],
    ] = {}
    global_set_before: dict[int, frozenset[str]] = {}
    global_unset_before: dict[int, frozenset[str]] = {}
    # Only function call sites read these snapshots. Copying the global state for every command
    # would cost one entry per live variable per command, so a body of plain assignments would
    # exhaust the edge budget quadratically in the number of distinct names.
    snapshot_command_ids = {
        command.command_id for sites in call_sites_by_context.values() for command in sites
    }

    def global_state_for(
        environment: int,
    ) -> tuple[
        dict[str, tuple[_CommandEvidence, ContentExpr]],
        set[str],
        set[str],
    ]:
        cached = global_values_by_environment.get(environment)
        if cached is not None:
            return (
                cached,
                global_set_by_environment[environment],
                global_unset_by_environment[environment],
            )
        parent = execution_parents.get(environment)
        if parent is None:
            values: dict[str, tuple[_CommandEvidence, ContentExpr]] = {}
            definitely_set: set[str] = set()
            definitely_unset: set[str] = set()
        else:
            parent_values, parent_set, parent_unset = global_state_for(parent)
            values = dict(parent_values)
            definitely_set = set(parent_set)
            definitely_unset = set(parent_unset)
        global_values_by_environment[environment] = values
        global_set_by_environment[environment] = definitely_set
        global_unset_by_environment[environment] = definitely_unset
        return values, definitely_set, definitely_unset

    def invalidate_global(
        name: str,
        values: dict[str, tuple[_CommandEvidence, ContentExpr]],
        definitely_set: set[str],
        definitely_unset: set[str],
    ) -> None:
        values.pop(name, None)
        definitely_set.discard(name)
        definitely_unset.discard(name)

    def apply_global_assignment(  # noqa: PLR0913
        assignment: _AssignmentEvidence,
        command: _CommandEvidence,
        values: dict[str, tuple[_CommandEvidence, ContentExpr]],
        definitely_set: set[str],
        definitely_unset: set[str],
        *,
        conditional: bool,
    ) -> None:
        name = _unscoped_variable_name(assignment.name)
        if conditional:
            invalidate_global(name, values, definitely_set, definitely_unset)
            return
        if assignment.append:
            prior = values.get(name)
            if (
                prior is not None
                and isinstance(prior[1], LiteralTransfer)
                and isinstance(assignment.content, LiteralTransfer)
            ):
                content: ContentExpr = LiteralTransfer(prior[1].text + assignment.content.text)
            elif name in definitely_unset:
                content = assignment.content
            else:
                invalidate_global(name, values, definitely_set, definitely_unset)
                return
            values[name] = (command, content)
            definitely_set.add(name)
            definitely_unset.discard(name)
            return
        if assignment.conditional:
            if name in definitely_set:
                prior = values.get(name)
                if (
                    not assignment.assign_if_null
                    or prior is None
                    or not isinstance(prior[1], LiteralTransfer)
                    or prior[1].text
                ):
                    return
            elif name not in definitely_unset:
                invalidate_global(name, values, definitely_set, definitely_unset)
                return
        values[name] = (command, assignment.content)
        definitely_set.add(name)
        definitely_unset.discard(name)

    for command in evidence.commands:
        environment = command_environments[command.command_id]
        global_values, global_set, global_unset = global_state_for(environment)
        if command.command_id in snapshot_command_ids:
            global_values_before[command.command_id] = dict(global_values)
            global_set_before[command.command_id] = frozenset(global_set)
            global_unset_before[command.command_id] = frozenset(global_unset)
            charge_edges(len(global_values) + len(global_set) + len(global_unset))
        if command.function_context_id is not None:
            continue
        status = conditional_execution[command.command_id]
        if status is False:
            continue
        conditional = status is None

        for assignment in (
            *command.assignments,
            *compound_bindings_by_command.get(command.command_id, ()),
        ):
            if assignment.conditional:
                apply_global_assignment(
                    assignment,
                    command,
                    global_values,
                    global_set,
                    global_unset,
                    conditional=conditional,
                )
        assignment_only = not command.argv or command.executable.argv_index is None
        assignments = (
            (*command.definite_assignments, *command.builtin_assignments)
            if assignment_only
            else command.builtin_assignments
        )
        eval_assignments, eval_unsets = _static_eval_mutations(command, limits=limits)
        for assignment in (
            *assignments,
            *(mutation.assignment for mutation in eval_assignments),
        ):
            apply_global_assignment(
                assignment,
                command,
                global_values,
                global_set,
                global_unset,
                conditional=conditional,
            )
        unset_names, unknown_unset = _unset_action(command)
        if unknown_unset or command.unknown_builtin_content is not None:
            global_values.clear()
            global_set.clear()
            global_unset.clear()
        for name in (*unset_names, *eval_unsets):
            if conditional:
                invalidate_global(name, global_values, global_set, global_unset)
                continue
            global_values.pop(name, None)
            global_set.discard(name)
            global_unset.add(name)
        for callee in callees_for(command):
            if callee in effect_unknown:
                global_values.clear()
                global_set.clear()
                global_unset.clear()
                continue
            for name in effect_names.get(callee, ()):
                invalidate_global(name, global_values, global_set, global_unset)

    entry_set_by_context: dict[int, frozenset[str]] = {}
    entry_unset_by_context: dict[int, frozenset[str]] = {}
    for context, sites in call_sites_by_context.items():
        entry_sets = [
            {
                *(
                    _unscoped_variable_name(assignment.name)
                    for assignment in command.definite_assignments
                ),
                *command_set_locals[command.command_id],
                *(
                    name
                    for name in global_set_before[command.command_id]
                    if command.function_context_id is None
                    and name not in command_locals[command.command_id]
                ),
            }
            for command in sites
        ]
        entry_unsets = [
            {
                name
                for name in global_unset_before[command.command_id]
                if command.function_context_id is None
                and name not in command_locals[command.command_id]
                and all(
                    _unscoped_variable_name(assignment.name) != name
                    for assignment in command.definite_assignments
                )
            }
            for command in sites
        ]
        charge_edges(sum(map(len, entry_sets)) + sum(map(len, entry_unsets)))
        entry_set_by_context[context] = (
            frozenset.intersection(*(frozenset(names) for names in entry_sets))
            if entry_sets
            else frozenset()
        )
        entry_unset_by_context[context] = (
            frozenset.intersection(*(frozenset(names) for names in entry_unsets))
            if entry_unsets
            else frozenset()
        )

    def scope_command_content(
        command: _CommandEvidence,
        expression: ContentExpr,
        *,
        prefixes: Mapping[str, tuple[ContentExpr, ...]] | None = None,
    ) -> ContentExpr:
        context = command.function_context_id
        active_prefixes = dict(
            prefixes if prefixes is not None else frozen_call_prefixes.get(context, {})
        )
        active_prefixes.update(exact_positional_prefixes.get(command.command_id, {}))
        return _scope_expression(
            _scope_function_variables(
                expression,
                context,
                command_locals[command.command_id],
                caller_locals_by_context.get(context, {}),
                active_prefixes,
            ),
            command_environments[command.command_id],
        )

    call_prefixes: dict[int, dict[str, list[ContentExpr]]] = {}
    reachable_call_pairs = {
        (command.command_id, context)
        for context, commands in preliminary_call_sites.items()
        for command in commands
    }
    for command, callee in call_commands:
        for assignment in command.definite_assignments:
            call_prefixes.setdefault(callee, {}).setdefault(
                _unscoped_variable_name(assignment.name),
                [],
            ).append(scope_command_content(command, assignment.content, prefixes={}))
        if (command.command_id, callee) not in reachable_call_pairs:
            continue
        if callee in positional_mutating_contexts:
            continue
        for name, content in positional_call_contents(command, callee).items():
            call_prefixes.setdefault(callee, {}).setdefault(name, []).append(
                scope_command_content(command, content, prefixes={})
            )
    frozen_call_prefixes = {
        context: {name: tuple(values) for name, values in by_name.items()}
        for context, by_name in call_prefixes.items()
    }
    entry_assignments_by_context: dict[int, tuple[_AssignmentEvidence, ...]] = {}
    for context, names in entry_set_by_context.items():
        assignments: list[_AssignmentEvidence] = []
        for name in names:
            values: list[ContentExpr] = []
            for site in call_sites_by_context.get(context, ()):
                prefix = next(
                    (
                        assignment
                        for assignment in reversed(site.definite_assignments)
                        if _unscoped_variable_name(assignment.name) == name
                    ),
                    None,
                )
                if prefix is not None:
                    values.append(scope_command_content(site, prefix.content, prefixes={}))
                    continue
                caller = site.function_context_id
                if caller is not None and name in command_set_locals[site.command_id]:
                    values.append(VariableRef(_scoped_variable_name(caller, name)))
                    continue
                global_value = global_values_before[site.command_id].get(name)
                if global_value is not None and name in global_set_before[site.command_id]:
                    writer, content = global_value
                    values.append(scope_command_content(writer, content, prefixes={}))
            if values:
                charge_edges(len(values))
                assignments.append(_AssignmentEvidence(name, choice(*values)))
        entry_assignments_by_context[context] = tuple(assignments)

    def scoped_destinations(
        command: _CommandEvidence,
        assignment: _AssignmentEvidence,
        *,
        force_global: bool = False,
        local_builtin: bool = False,
    ) -> tuple[str, ...]:
        name = _unscoped_variable_name(assignment.name)
        context = command.function_context_id
        if force_global or context is None:
            return (name,)
        if local_builtin or name in command_locals[command.command_id]:
            return (_scoped_variable_name(context, name),)
        if not persistent_function_effect(command):
            return (name,)
        destinations: set[str] = {
            _scoped_variable_name(caller, name)
            for caller in caller_locals_by_context.get(context, {}).get(name, ())
        }
        for site in call_sites_by_context.get(context, ()):
            caller = site.function_context_id
            if caller is not None and name in command_locals[site.command_id]:
                destinations.add(_scoped_variable_name(caller, name))
            else:
                destinations.add(name)
        charge_edges(len(destinations))
        return tuple(sorted(destinations or {name}))

    def contextualized_assignments(
        command: _CommandEvidence,
        assignments: tuple[_AssignmentEvidence, ...],
        *,
        force_global: bool = False,
        local_builtin: bool = False,
    ) -> tuple[_AssignmentEvidence, ...]:
        contextualized = tuple(
            replace(
                assignment,
                name=destination,
                content=scope_command_content(command, assignment.content),
            )
            for assignment in assignments
            for destination in scoped_destinations(
                command,
                assignment,
                force_global=force_global,
                local_builtin=local_builtin,
            )
        )
        charge_edges(len(contextualized))
        return contextualized

    commands = tuple(
        replace(
            command,
            argv=tuple(
                replace(
                    port,
                    content=scope_command_content(command, port.content),
                )
                for port in command.argv
            ),
            assignments=contextualized_assignments(command, command.assignments),
            definite_assignments=contextualized_assignments(
                command,
                command.definite_assignments,
            ),
            builtin_assignments=contextualized_assignments(
                command,
                command.builtin_assignments,
                force_global=command.builtin_force_global,
                local_builtin=command.builtin_local and not command.builtin_force_global,
            ),
            unknown_builtin_content=(
                scope_command_content(
                    command,
                    command.unknown_builtin_content,
                )
                if command.unknown_builtin_content is not None
                else None
            ),
            redirections=tuple(
                replace(
                    event,
                    target=ContentTarget(scope_command_content(command, event.target.content)),
                )
                if isinstance(event.target, ContentTarget)
                else event
                for event in command.redirections
            ),
            called_function_context_ids=callees_for(command),
            function_entry_definitely_set=tuple(
                sorted(entry_set_by_context.get(command.function_context_id, ()))
            ),
            function_entry_assignments=entry_assignments_by_context.get(
                command.function_context_id,
                (),
            ),
            function_entry_unsets=tuple(
                sorted(entry_unset_by_context.get(command.function_context_id, ()))
            ),
        )
        for command in evidence.commands
    )
    contextualized_by_id = {command.command_id: command for command in commands}

    compound_effects: dict[int, list[_AssignmentEvidence]] = {}
    original_commands = {command.command_id: command for command in evidence.commands}
    for scope in evidence.scopes:
        anchor = (
            original_commands.get(scope.binding_command_id)
            if scope.binding_command_id is not None
            else None
        )
        if (
            anchor is None
            or anchor.function_context_id is None
            or not persistent_function_effect(anchor)
        ):
            continue
        compound_effects.setdefault(anchor.function_context_id, []).extend(
            contextualized_assignments(anchor, scope.loop_bindings)
        )

    effect_assignments: dict[int, list[_AssignmentEvidence]] = {
        context: [] for context in function_contexts
    }
    effect_segments: dict[int, list[_AssignmentEvidence | int]] = {
        context: [] for context in function_contexts
    }
    effect_unsets: dict[int, list[str]] = {context: [] for context in function_contexts}
    called_contexts_by_command: dict[int, list[int]] = {}
    for call, callee in call_commands:
        called_contexts_by_command.setdefault(call.command_id, []).append(callee)
    for context in function_contexts:
        assignments = effect_assignments[context]
        segments = effect_segments[context]
        unsets = effect_unsets[context]
        for original in evidence.commands:
            if original.function_context_id != context or not persistent_function_effect(original):
                continue
            assignment_start = len(assignments)
            command = contextualized_by_id[original.command_id]
            assignment_only = not command.argv or command.executable.argv_index is None
            if assignment_only:
                assignments.extend(
                    assignment
                    for assignment in command.definite_assignments
                    if _scoped_variable_environment(assignment.name) != context
                )
            assignments.extend(
                assignment
                for assignment in command.builtin_assignments
                if _scoped_variable_environment(assignment.name) != context
            )
            eval_assignments, eval_unsets = _static_eval_mutations(original, limits=limits)
            for mutation in eval_assignments:
                assignments.extend(
                    assignment
                    for assignment in contextualized_assignments(
                        original,
                        (mutation.assignment,),
                        force_global=mutation.force_global,
                        local_builtin=mutation.local and not mutation.force_global,
                    )
                    if _scoped_variable_environment(assignment.name) != context
                )
            names, unknown = _unset_action(original)
            if unknown:
                names = (
                    *names,
                    *caller_locals_by_context.get(context, ()),
                )
            nameref_names, _unknown_nameref = _unset_nameref_action(original)
            assignments.extend(
                _AssignmentEvidence(
                    name,
                    LiteralTransfer(""),
                    nameref_unset=True,
                )
                for name in nameref_names
                if name not in command_locals[original.command_id]
            )
            names = (*names, *eval_unsets)
            unsets.extend(name for name in names if name not in command_locals[original.command_id])
            segments.extend(assignments[assignment_start:])
            segments.extend(called_contexts_by_command.get(original.command_id, ()))
        compound_start = len(assignments)
        assignments.extend(
            assignment
            for assignment in compound_effects.get(context, ())
            if _scoped_variable_environment(assignment.name) != context
        )
        segments.extend(assignments[compound_start:])

    changed = True
    while changed:
        changed = False
        for call, callee in call_commands:
            caller = call.function_context_id
            if caller is None or not persistent_function_effect(call):
                continue
            unsets = effect_unsets.setdefault(caller, [])
            for name in effect_unsets.get(callee, ()):
                if name in unsets:
                    continue
                unsets.append(name)
                charge_edges()
                changed = True

    ordered_effects_cache: dict[int, tuple[_AssignmentEvidence, ...]] = {}

    def ordered_effects(context: int) -> tuple[_AssignmentEvidence, ...]:
        cached = ordered_effects_cache.get(context)
        if cached is not None:
            return cached
        context_stack = [context]
        active_stack = [frozenset({context})]
        index_stack = [0]
        assignment_stack: list[list[_AssignmentEvidence]] = [[]]
        while context_stack:
            current = context_stack[-1]
            segments = effect_segments.get(current, ())
            index = index_stack[-1]
            if index >= len(segments):
                result = tuple(assignment_stack.pop())
                ordered_effects_cache[current] = result
                context_stack.pop()
                active_stack.pop()
                index_stack.pop()
                if not context_stack:
                    return result
                charge_edges(len(result))
                assignment_stack[-1].extend(result)
                continue
            segment = segments[index]
            index_stack[-1] += 1
            if isinstance(segment, _AssignmentEvidence):
                assignment_stack[-1].append(segment)
                continue
            nested = ordered_effects_cache.get(segment)
            if nested is None and segment in active_stack[-1]:
                nested = tuple(effect_assignments.get(segment, ()))
            if nested is not None:
                charge_edges(len(nested))
                assignment_stack[-1].extend(nested)
                continue
            if len(context_stack) - 1 >= limits.max_function_effect_depth:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.function-effects.depth-limit",
                        "shell taint function effect depth limit exceeded",
                    )
                )
            context_stack.append(segment)
            active_stack.append(active_stack[-1] | {segment})
            index_stack.append(0)
            assignment_stack.append([])
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.function-effects.unstructured-segment",
                "shell function effects cannot be structured",
            )
        )

    effects_by_context = {
        context: (
            ordered_effects(context),
            tuple(dict.fromkeys(effect_unsets.get(context, ()))),
        )
        for context in effect_assignments
    }

    def effects_for_call(
        command: _CommandEvidence,
    ) -> tuple[tuple[_AssignmentEvidence, ...], tuple[str, ...]]:
        assignments: list[_AssignmentEvidence] = []
        unsets: list[str] = []
        caller = command.function_context_id
        caller_locals = command_locals[command.command_id]
        for callee in command.called_function_context_ids:
            callee_assignments, callee_unsets = effects_by_context.get(callee, ((), ()))
            for assignment in callee_assignments:
                name = _unscoped_variable_name(assignment.name)
                target = _scoped_variable_environment(assignment.name)
                if assignment.nameref_unset:
                    assignments.append(
                        replace(
                            assignment,
                            name=(
                                _scoped_variable_name(caller, name)
                                if caller is not None and name in caller_locals
                                else name
                            ),
                        )
                    )
                    continue
                if target == caller or (target is None and name not in caller_locals):
                    assignments.append(assignment)
            unsets.extend(
                _scoped_variable_name(caller, name)
                if caller is not None and name in caller_locals
                else name
                for name in callee_unsets
            )
        return tuple(assignments), tuple(dict.fromkeys(unsets))

    commands_with_effects: list[_CommandEvidence] = []
    for command in commands:
        call_assignments, call_unsets = effects_for_call(command)
        charge_edges(len(call_assignments) + len(call_unsets))
        commands_with_effects.append(
            replace(
                command,
                function_effect_assignments=call_assignments,
                function_effect_unsets=call_unsets,
            )
        )
    commands = tuple(commands_with_effects)
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
                    name=destination,
                    content=_scope_expression(
                        _scope_function_variables(
                            binding.content,
                            anchor.function_context_id if anchor is not None else None,
                            (
                                command_locals[anchor.command_id]
                                if anchor is not None
                                else frozenset()
                            ),
                            caller_locals_by_context.get(
                                anchor.function_context_id if anchor is not None else None,
                                {},
                            ),
                            frozen_call_prefixes.get(
                                anchor.function_context_id if anchor is not None else None,
                                {},
                            ),
                        ),
                        environments.get(scope.scope_id, scope.scope_id),
                    ),
                )
                for binding in scope.loop_bindings
                for anchor in (
                    original_commands.get(scope.binding_command_id)
                    if scope.binding_command_id is not None
                    else None,
                )
                for destination in (
                    scoped_destinations(anchor, binding)
                    if anchor is not None
                    else (
                        _scoped_variable_name(
                            environments.get(scope.scope_id, scope.scope_id),
                            binding.name,
                        ),
                    )
                )
            ),
        )
        for scope in evidence.scopes
    )
    return replace(evidence, commands=commands, scopes=scopes)


def _build_flow_definitions(  # noqa: PLR0912, PLR0915
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits,
) -> tuple[_FlowDefinitions, dict[int, ContentExpr]]:
    """Lower typed shell evidence into fixed-point definitions and command stdin."""
    environments, _lexical_parents = _scope_environment_ids(evidence.scopes)
    command_environments, environment_parents, _lastpipe = _execution_environment_ids(evidence)
    command_paths = _command_scope_paths(evidence)
    scope_bindings = _scope_inherited_bindings(evidence)
    guarded_descriptors = _guarded_output_descriptors(evidence)
    command_scopes = {command.command_id: command.output_scope_id for command in evidence.commands}
    process_resources = {resource.resource_id: resource for resource in evidence.process_resources}
    input_descriptors = _InputDescriptorContext(
        scope_bindings=_scope_inherited_input_bindings(evidence, process_resources),
        guarded=_guarded_input_descriptors(evidence),
    )
    pipe_inputs = _pipe_inputs(evidence, input_descriptors)
    inputs = {
        command.command_id: _input_expression(
            command,
            pipe_inputs,
            process_resources,
            command_paths,
            input_descriptors,
        )
        for command in evidence.commands
    }
    variable_writes: list[_FlowWrite] = []
    resource_writes: list[_FlowWrite] = []
    stream_writes: list[_FlowWrite] = []
    scoped_variables = bool(evidence.scopes)
    has_eval = any(_builtin_eval_candidates(command) for command in evidence.commands)
    eval_dependency_names = (
        {
            name
            for command in evidence.commands
            for executable in _builtin_eval_candidates(command)
            for name in _eval_content_dependencies(
                _eval_arguments_raw(command, executable),
                limits,
            )
        }
        if any(command.unknown_builtin_content is not None for command in evidence.commands)
        else set()
    )
    variable_keys: set[str | int] = set()

    def append_variable_write(write: _FlowWrite) -> None:
        if len(variable_writes) >= limits.max_edges:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.flow-build.variable-write-edge-limit", "shell taint edge limit exceeded"
                )
            )
        if write.key not in variable_keys and len(variable_keys) >= limits.max_table_entries:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.flow-build.variable-key-table-limit",
                    "shell taint table entry limit exceeded",
                )
            )
        variable_writes.append(write)
        variable_keys.add(write.key)

    def visible_environments(environment: int) -> set[int]:
        visible: set[int] = set()
        current: int | None = environment
        while current is not None and current not in visible:
            visible.add(current)
            current = environment_parents.get(current)
        return visible

    for command in evidence.commands:
        if command.prune_unreachable_effects:
            stream_writes.append(_FlowWrite(command.output_scope_id, LiteralTransfer("")))
            continue
        origin_environment = command_environments[command.command_id]
        for target_environment in set(command_environments.values()):
            if origin_environment not in visible_environments(target_environment):
                continue
            for assignment in command.assignments:
                append_variable_write(
                    _FlowWrite(
                        (
                            _scoped_variable_name(target_environment, assignment.name)
                            if scoped_variables
                            else assignment.name
                        ),
                        assignment.content,
                        append=assignment.append,
                        read_ifs=assignment.read_ifs,
                        read_target_index=assignment.read_target_index,
                        read_target_count=assignment.read_target_count,
                        read_raw=assignment.read_raw,
                    )
                )
        if scoped_variables and has_eval and environment_parents.get(origin_environment) is None:
            for assignment in command.assignments:
                append_variable_write(
                    _FlowWrite(
                        assignment.name,
                        assignment.content,
                        append=assignment.append,
                        read_ifs=assignment.read_ifs,
                        read_target_index=assignment.read_target_index,
                        read_target_count=assignment.read_target_count,
                        read_raw=assignment.read_raw,
                    )
                )
        if command.builtin_local and command.function_context_id is not None:
            for assignment in command.builtin_assignments:
                append_variable_write(
                    _FlowWrite(
                        _scoped_variable_name(
                            command.function_context_id,
                            assignment.name,
                        ),
                        assignment.content,
                        append=assignment.append,
                        read_ifs=assignment.read_ifs,
                        read_target_index=assignment.read_target_index,
                        read_target_count=assignment.read_target_count,
                        read_raw=assignment.read_raw,
                    )
                )
        if (
            not command.builtin_local
            or command.function_context_id is None
            or command.builtin_dynamic_options
        ):
            for target_environment in set(command_environments.values()):
                if origin_environment not in visible_environments(target_environment):
                    continue
                for assignment in command.builtin_assignments:
                    append_variable_write(
                        _FlowWrite(
                            (
                                _scoped_variable_name(
                                    target_environment,
                                    (
                                        _unscoped_variable_name(assignment.name)
                                        if command.builtin_dynamic_options
                                        else assignment.name
                                    ),
                                )
                                if scoped_variables
                                else assignment.name
                            ),
                            assignment.content,
                            append=assignment.append,
                            read_ifs=assignment.read_ifs,
                            read_target_index=assignment.read_target_index,
                            read_target_count=assignment.read_target_count,
                            read_raw=assignment.read_raw,
                        )
                    )
        if command.unknown_builtin_content is not None:
            for name in eval_dependency_names:
                if (
                    command.builtin_local
                    and command.function_context_id is not None
                    and not command.builtin_dynamic_options
                    and _scoped_variable_environment(name) != command.function_context_id
                ):
                    continue
                append_variable_write(
                    _FlowWrite(
                        name,
                        choice(OutsideGap(), command.unknown_builtin_content),
                    )
                )
        output = _producer_stdout(command, inputs[command.command_id], limits, process_resources)
        stream_writes.append(_FlowWrite(command.output_scope_id, output))
        # A ``>&N`` inside a compound resolves against the descriptor that compound bound, so the
        # enclosing chain has to be visible here. Without it the write resolved to an unnamed
        # dynamic target and was dropped, laundering the content into a file read as unwritten.
        enclosing = command_paths[command.command_id]
        inherited_bindings = scope_bindings.get(enclosing[-1], {}) if enclosing else {}
        resource_writes.extend(
            _static_write_definitions(
                command.redirections,
                output,
                inherited_bindings,
                guarded_descriptors,
            )
        )
        # A write performed inside this command's own exact eval payload belongs at this point in
        # body order, so a payload truncation before an authored append keeps its side effect and
        # append accumulation stays sequenced the way AD-18 records (issue #146).
        resource_writes.extend(
            _static_eval_resource_writes(
                command,
                command_environments[command.command_id],
                inherited_bindings,
                guarded_descriptors,
                scoped=scoped_variables,
                limits=limits,
            )
        )

    lowering = _OutputLowering(command_scopes)
    target_environments = set(environments.values()) | set(command_environments.values())
    function_contexts = {
        command.function_context_id
        for command in evidence.commands
        if command.function_context_id is not None
    }
    visible_by_target = {
        target_environment: visible_environments(target_environment)
        for target_environment in target_environments
    }
    for scope in evidence.scopes:
        lexical_environment = environments.get(scope.scope_id, scope.scope_id)
        origin_environment = next(
            (
                command_environments[command.command_id]
                for command in evidence.commands
                if scope.scope_id in command_paths[command.command_id]
            ),
            environments.get(scope.scope_id, scope.scope_id),
        )
        for binding in scope.loop_bindings:
            binding_environment = _scoped_variable_environment(binding.name)
            if binding_environment not in function_contexts:
                continue
            append_variable_write(
                _FlowWrite(
                    binding.name,
                    _rescope_expression(
                        binding.content,
                        lexical_environment,
                        origin_environment,
                    ),
                    append=binding.append,
                )
            )
        for target_environment, visible in visible_by_target.items():
            if origin_environment not in visible:
                continue
            for binding in scope.loop_bindings:
                if _scoped_variable_environment(binding.name) in function_contexts:
                    continue
                key = _scoped_variable_name(
                    target_environment,
                    _unscoped_variable_name(binding.name),
                )
                append_variable_write(
                    _FlowWrite(
                        key,
                        _rescope_expression(
                            binding.content,
                            lexical_environment,
                            origin_environment,
                        ),
                        append=binding.append,
                    )
                )
        stream_writes.append(
            _FlowWrite(
                scope.scope_id,
                lowering.lower(scope.output, stream_writes),
                strip_trailing_newlines=scope.kind == "command_substitution",
            )
        )
        resource_writes.extend(
            _static_write_definitions(
                scope.redirections,
                StreamRef(scope.scope_id),
                scope_bindings.get(scope.parent_scope_id, {})
                if scope.parent_scope_id is not None
                else {},
                guarded_descriptors,
            )
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
    arguments = _eval_argument_ports(command, head_index)
    parts: list[ContentExpr] = []
    for index, argument in enumerate(arguments):
        if index:
            parts.append(LiteralTransfer(" "))
        parts.append(argument.content)
    return concat(*parts)


_EVAL_QUOTE_STATES = (None, "'", '"')
_EVAL_ANSI_OCTAL_BASE = 8
_EVAL_UNICODE_MAX = 0x10FFFF
_EVAL_SURROGATE_MIN = 0xD800
_EVAL_SURROGATE_MAX = 0xDFFF
_EVAL_SPECIAL_PARAMETERS = frozenset("@*#?-$!0123456789")


@dataclass(frozen=True, slots=True)
class _EvalSyntaxState:
    """One bounded eval parse state retained across symbolic variable writes."""

    summary: _TransferSummary
    quote: str | None
    brace_tokens: tuple[_ContentToken, ...] = ()
    brace_depth: int = 0
    applied_appends: int = 0
    local_variables: tuple[tuple[str, _ContentValue], ...] = ()
    environment_variables: tuple[tuple[str, _ContentValue], ...] = ()
    fixed_point_overrides: tuple[tuple[str, _ContentValue], ...] = ()
    definitely_set_variables: frozenset[str] = frozenset()
    parameter_text: str = ""
    conditional_assignments: tuple[_AssignmentEvidence, ...] = ()
    conditional_decisions: tuple[tuple[_SecondPassConditionalAssignment, bool], ...] = ()

    def __post_init__(self) -> None:
        """Enforce that a cleared literal projection always widens instead of vanishing.

        A summary whose ``literal_texts`` is empty without ``projection_incomplete`` set would
        make ``_project_read_value`` silently drop the alternative instead of widening it to the
        top of the projection lattice. ``_merge_eval_syntax_states`` is the one place that clears
        an eval-syntax state's ``literal_texts``, so this catches a future join that gets the
        pairing wrong (issue #140).
        """
        if not self.summary.literal_texts and not self.summary.projection_incomplete:
            raise _MalformedTaintEvidence(
                GuardRefusal(
                    "taint.eval-syntax.cleared-projection-without-widening",
                    "shell taint eval syntax state cleared literal projection without widening",
                )
            )


_EvalSyntaxValue: TypeAlias = frozenset[_EvalSyntaxState]  # noqa: UP040
_EvalSyntaxPrograms: TypeAlias = dict[  # noqa: UP040
    str | int,
    tuple[tuple[int, _FlowWrite], ...],
]
_EvalSyntaxTransitionKey: TypeAlias = tuple[str | int, _EvalSyntaxState]  # noqa: UP040
_EvalSyntaxSlot: TypeAlias = tuple[str | int, str | None]  # noqa: UP040


@dataclass(frozen=True, slots=True)
class _EvalSyntaxTransition:
    """One memoized transition value with the solved-table slots deriving it observed."""

    value: _EvalSyntaxValue
    observed: frozenset[_EvalSyntaxSlot]


@dataclass(slots=True)
class _EvalSyntaxContext:
    """Bounded tables shared while reparsing one family of eval expressions."""

    variables: dict[tuple[str | int, str | None], _EvalSyntaxValue]
    raw_variables: dict[str | int, _ContentValue]
    limits: TaintLimits
    programs: _EvalSyntaxPrograms
    environment_index: dict[int, tuple[tuple[str, _ContentValue], ...]] = field(
        default_factory=dict
    )
    transitions: dict[_EvalSyntaxTransitionKey, _EvalSyntaxTransition] = field(default_factory=dict)
    active_transitions: set[_EvalSyntaxTransitionKey] = field(default_factory=set)
    observations: list[set[_EvalSyntaxSlot]] = field(default_factory=list)
    variable_overlays: dict[
        tuple[int, int, int],
        tuple[
            tuple[tuple[str, _ContentValue], ...],
            tuple[tuple[str, _ContentValue], ...],
            tuple[tuple[str, _ContentValue], ...],
            Mapping[str | int, _ContentValue],
        ],
    ] = field(default_factory=dict)
    transition_updates: int = 0

    def __post_init__(self) -> None:
        """Index scoped values once instead of rescanning the solved table per write."""
        if not self.environment_index:
            self.environment_index = _eval_environment_index(self.raw_variables)

    def invalidate_transitions(self, slot: _EvalSyntaxSlot) -> None:
        """Discard only memoized transitions that observed one rewritten variable table slot.

        ``variables`` is read from exactly one place, the cycle-detected ``VariableRef`` branch of
        ``_eval_syntax_append``, and every other input to a replayed transition is immutable for
        the life of the pass. So an entry that never observed ``slot`` cannot have changed, and
        clearing the whole table instead made the ordered pass quadratic in its write count
        (issue #149).
        """
        stale = [key for key, transition in self.transitions.items() if slot in transition.observed]
        for key in stale:
            del self.transitions[key]

    def begin_observations(self) -> None:
        """Start recording which variable table slots the transition being derived observes."""
        self.observations.append(set())

    def end_observations(self) -> frozenset[_EvalSyntaxSlot]:
        """Finish one recording, propagating its slots to every enclosing derivation.

        A derivation that reuses a nested value depends on whatever that value depended on, so
        the slots have to travel outward. Losing them here would retain an enclosing entry that a
        write already invalidated, which is the one way this cache turns unsound rather than slow.
        """
        observed = self.observations.pop()
        for enclosing in self.observations:
            enclosing.update(observed)
        return frozenset(observed)

    def observe_slot(self, slot: _EvalSyntaxSlot) -> None:
        """Record one variable table read against every derivation in progress."""
        for frame in self.observations:
            frame.add(slot)

    def inherit_observations(self, observed: frozenset[_EvalSyntaxSlot]) -> None:
        """Carry a reused transition's recorded slots into every derivation in progress."""
        for frame in self.observations:
            frame.update(observed)

    def variables_for(self, state: _EvalSyntaxState) -> Mapping[str | int, _ContentValue]:
        """Return one cached layered variable view for token evaluation."""
        key = (
            id(state.local_variables),
            id(state.environment_variables),
            id(state.fixed_point_overrides),
        )
        cached = self.variable_overlays.get(key)
        if (
            cached is not None
            and cached[0] is state.local_variables
            and cached[1] is state.environment_variables
            and cached[2] is state.fixed_point_overrides
        ):
            return cached[3]
        if len(self.variable_overlays) >= self.limits.max_table_entries:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-syntax.variable-overlay-table-limit",
                    "shell taint eval syntax variable table limit exceeded",
                )
            )
        local_variables: dict[str | int, _ContentValue] = dict(state.local_variables)
        environment_variables: dict[str | int, _ContentValue] = dict(state.environment_variables)
        fixed_point_overrides: dict[str | int, _ContentValue] = dict(state.fixed_point_overrides)
        layered: ChainMap[str | int, _ContentValue] = ChainMap(
            local_variables,
            fixed_point_overrides,
            environment_variables,
            self.raw_variables,
        )
        self.variable_overlays[key] = (
            state.local_variables,
            state.environment_variables,
            state.fixed_point_overrides,
            layered,
        )
        return layered


def _eval_syntax_programs(writes: tuple[_FlowWrite, ...]) -> _EvalSyntaxPrograms:
    """Group indexed variable writes into source-ordered transition programs."""
    grouped: dict[str | int, list[tuple[int, _FlowWrite]]] = {}
    for write_index, write in enumerate(writes):
        grouped.setdefault(write.key, []).append((write_index, write))
    return {key: tuple(program) for key, program in grouped.items()}


def _eval_reparse_content(
    expression: ContentExpr,
    limits: TaintLimits,
) -> ContentExpr:
    """Interpret the minimal shell syntax that ``eval`` reparses from literal content."""
    branches = _eval_reparse_branches(expression, quote=None, depth=0, limits=limits)
    return choice(*(branch for branch, _quote in branches))


def _eval_reparse_branches(
    expression: ContentExpr,
    *,
    quote: str | None,
    depth: int,
    limits: TaintLimits,
) -> list[tuple[ContentExpr, str | None]]:
    """Reparse content while retaining quote state across symbolic expression boundaries."""
    if depth > limits.max_eval_reparse_depth:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-reparse.branch-depth-limit",
                "shell taint eval reparse depth limit exceeded",
            )
        )
    if isinstance(expression, LiteralTransfer):
        parsed, resulting_quote = _eval_reparse_literal(expression.text, quote, limits)
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
                    limits=limits,
                ):
                    expanded.append((concat(prefix, suffix), resulting_quote))
                    if len(expanded) > limits.max_eval_reparse_branches:
                        raise _TaintLimitExceeded(
                            GuardRefusal(
                                "taint.eval-reparse.expanded-branch-limit",
                                "shell taint eval reparse branch limit exceeded",
                            )
                        )
            branches = expanded
        return branches
    if isinstance(expression, Choice):
        branches = []
        for part in expression.parts:
            branches.extend(
                _eval_reparse_branches(
                    part,
                    quote=quote,
                    depth=depth + 1,
                    limits=limits,
                )
            )
            if len(branches) > limits.max_eval_reparse_branches:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-reparse.merged-branch-limit",
                        "shell taint eval reparse branch limit exceeded",
                    )
                )
        return branches
    return [(expression, quote)]


def _eval_reparse_tokens(
    text: str,
    quote: str | None,
    limits: TaintLimits,
    *,
    depth: int = 0,
) -> tuple[tuple[_ContentToken, ...], str | None]:
    """Tokenize one complete eval fragment without retaining malformed suffixes."""
    tokens, resulting_quote, _pending_parameter = _eval_reparse_tokens_streaming(
        text,
        quote,
        limits,
        depth=depth,
        defer_incomplete_parameter=False,
    )
    return tokens, resulting_quote


def _eval_reparse_tokens_streaming(  # noqa: PLR0912, PLR0915
    text: str,
    quote: str | None,
    limits: TaintLimits,
    *,
    depth: int = 0,
    defer_incomplete_parameter: bool,
) -> tuple[tuple[_ContentToken, ...], str | None, str]:
    """Tokenize eval text while retaining active brace and quote provenance."""
    if depth > limits.max_eval_reparse_depth:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-reparse.stream-depth-limit",
                "shell taint eval reparse depth limit exceeded",
            )
        )
    tokens: list[_ContentToken] = []
    index = 0

    def append_literal(text: str, *, brace_active: bool) -> None:
        tokens.extend(
            _ContentToken(LiteralTransfer(character), character, brace_active) for character in text
        )

    def append_expression(expression: ContentExpr) -> None:
        tokens.append(_ContentToken(expression, "", False))

    while index < len(text):
        character = text[index]
        if quote == "'":
            if character == "'":
                quote = None
            else:
                append_literal(character, brace_active=False)
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
            append_literal(decoded, brace_active=False)
            continue
        if character == "\\":
            if index + 1 >= len(text):
                if defer_incomplete_parameter:
                    return tuple(tokens), quote, text[index:]
                append_literal(character, brace_active=False)
                index += 1
                continue
            escaped = text[index + 1]
            if quote == '"' and escaped not in {"$", '"', "\\", "`", "\n"}:
                append_literal(f"{character}{escaped}", brace_active=False)
            elif escaped != "\n":
                append_literal(escaped, brace_active=False)
            index += 2
            continue
        if text.startswith("$(", index) and not text.startswith("$((", index):
            if (
                _eval_command_substitution_closing(text, index, limits=limits) is None
                and defer_incomplete_parameter
            ):
                return tuple(tokens), quote, text[index:]
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-reparse.dollar-command-substitution",
                    "shell taint eval command substitution cannot be bounded",
                )
            )
        if character == "`":
            if _eval_backtick_closing(text, index) is None and defer_incomplete_parameter:
                return tuple(tokens), quote, text[index:]
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-reparse.backquote-substitution",
                    "shell taint eval command substitution cannot be bounded",
                )
            )
        if character == "$":
            if index + 1 == len(text) and defer_incomplete_parameter:
                return tuple(tokens), quote, text[index:]
            if text.startswith("${", index):
                closing = _eval_parameter_closing(text, index, limits=limits)
                if closing is None:
                    if defer_incomplete_parameter:
                        return tuple(tokens), quote, text[index:]
                    append_expression(OutsideGap())
                    index = len(text)
                    continue
                contents = text[index + 2 : closing]
                append_expression(
                    _eval_parameter_content(
                        contents,
                        quote,
                        limits,
                        depth=depth + 1,
                    )
                )
                index = closing + 1
                continue
            name, end = _eval_parameter_name(text, index)
            if name is not None:
                append_expression(_SecondPassVariableRef(name))
                index = end
                continue
            if index + 1 < len(text) and text[index + 1] in _EVAL_SPECIAL_PARAMETERS:
                append_expression(OutsideGap())
                index += 2
                continue
        append_literal(character, brace_active=quote is None)
        index += 1
    return tuple(tokens), quote, ""


def _eval_reparse_literal(
    text: str,
    quote: str | None,
    limits: TaintLimits,
) -> tuple[ContentExpr, str | None]:
    """Reparse quote, escape, parameter, and bounded brace syntax."""
    tokens, resulting_quote = _eval_reparse_tokens(text, quote, limits)
    reparsed = tuple(_token_content(expanded) for expanded in _expand_braces(list(tokens), limits))
    return choice(*reparsed), resulting_quote


def _eval_command_substitution_closing(
    text: str,
    start: int,
    *,
    limits: TaintLimits,
    depth: int = 0,
) -> int | None:
    """Return the balanced closing parenthesis for one active eval-time ``$(...)``."""
    if depth > limits.max_eval_reparse_depth:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-reparse.closing-depth-limit",
                "shell taint eval command substitution cannot be bounded",
            )
        )
    index = start + 2
    parentheses = 1
    quote: str | None = None
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if quote == "'":
            quote = None if character == "'" else quote
            index += 1
            continue
        if character == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if character == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if text.startswith("$(", index) and not text.startswith("$((", index):
            nested_closing = _eval_command_substitution_closing(
                text,
                index,
                limits=limits,
                depth=depth + 1,
            )
            if nested_closing is None:
                return None
            index = nested_closing + 1
            continue
        if quote is None and character == "(":
            parentheses += 1
        elif quote is None and character == ")":
            parentheses -= 1
            if parentheses == 0:
                return index
        index += 1
    return None


def _eval_backtick_closing(text: str, start: int) -> int | None:
    """Return the unescaped closing backtick for one active legacy substitution."""
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "`":
            return index
        index += 1
    return None


def _eval_quoted_advance(text: str, index: int, quote: str) -> tuple[int, str | None]:
    """Advance one character inside an eval quote, honouring backslash only outside `'`.

    Args:
        text: The eval program text being scanned.
        index: The offset of the character to consume.
        quote: The active quote character.

    Returns:
        The next offset and the quote still in effect after the character.
    """
    character = text[index]
    if character == "\\" and quote != "'":
        return index + 2, quote
    return index + 1, (None if character == quote else quote)


def _eval_parameter_closing(text: str, start: int, *, limits: TaintLimits) -> int | None:
    """Return the balanced closing brace for an eval-time parameter expansion."""
    index = start + 2
    nested = 1
    quote: str | None = None
    while index < len(text):
        character = text[index]
        if quote is not None:
            index, quote = _eval_quoted_advance(text, index, quote)
            continue
        if character == "\\":
            index += 2
            continue
        if text.startswith("$(", index) and not text.startswith("$((", index):
            substitution_closing = _eval_command_substitution_closing(text, index, limits=limits)
            if substitution_closing is None:
                return None
            index = substitution_closing + 1
            continue
        if character == "`":
            substitution_closing = _eval_backtick_closing(text, index)
            if substitution_closing is None:
                return None
            index = substitution_closing + 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if text.startswith("${", index):
            nested += 1
            index += 2
            continue
        if character == "}":
            nested -= 1
            if nested == 0:
                return index
        index += 1
    return None


def _eval_parameter_content(  # noqa: PLR0911
    contents: str,
    quote: str | None,
    limits: TaintLimits,
    *,
    depth: int,
) -> ContentExpr:
    """Lower one balanced eval-time parameter expansion without dropping authored operands."""
    if _is_second_pass_positional_parameter(contents):
        return _SecondPassVariableRef(contents)
    if contents in _EVAL_SPECIAL_PARAMETERS:
        return OutsideGap()
    name_end = _eval_identifier_end(contents, 0)
    name = contents[:name_end]
    variable: ContentExpr = _SecondPassVariableRef(name) if name else OutsideGap()
    if name and name_end == len(contents):
        return variable

    operator: str | None = None
    operand_start = name_end
    for candidate in (":-", ":=", ":+", "-", "=", "+"):
        if contents.startswith(candidate, name_end):
            operator = candidate
            operand_start += len(candidate)
            break
    operand_text = contents[operand_start:]
    operand_tokens, _operand_quote = _eval_reparse_tokens(
        operand_text,
        quote,
        limits,
        depth=depth,
    )
    operand = _token_content(list(operand_tokens))
    if operator in {"=", ":="} and name:
        return _SecondPassConditionalAssignment(name, operand, operator == ":=")
    if operator in {"-", ":-"}:
        return choice(variable, operand)
    if operator in {"+", ":+"}:
        return choice(LiteralTransfer(""), operand)
    # An unmodeled transform (``#``, ``%``, ``/``, ``^``, ``,``, ``:off:len``, ``?``) still derives
    # its result from the parameter, so the variable has to stay beside the authored operand.
    # Dropping it made ``${M#x}`` inert even when ``M`` composed the marker.
    return choice(LiteralTransfer(""), concat(OutsideGap(), variable, operand, OutsideGap()))


def _eval_parameter_name(text: str, start: int) -> tuple[str | None, int]:
    """Return one simple eval parameter name without interpreting complex shell forms."""
    index = start + 1
    if index < len(text) and text[index] == "{":
        name_start = index + 1
        closing = text.find("}", name_start)
        if closing != -1:
            name = text[name_start:closing]
            if _is_second_pass_positional_parameter(name):
                return name, closing + 1
        name_end = _eval_identifier_end(text, name_start)
        if name_end > name_start and name_end < len(text) and text[name_end] == "}":
            return text[name_start:name_end], name_end + 1
        return None, start + 1
    if index < len(text) and _is_second_pass_positional_parameter(text[index]):
        return text[index], index + 1
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
    raise _TaintLimitExceeded(
        GuardRefusal(
            "taint.eval-ansi-c.unterminated-literal", "unterminated eval ANSI-C quoted literal"
        )
    )


def _eval_ansi_c_escape(text: str, start: int) -> tuple[str, int]:  # noqa: PLR0911
    """Decode the scanner-supported ANSI-C escape surface for eval reparsing."""
    if start >= len(text):
        return "\\", start
    character = text[start]
    if character in _ANSI_C_SIMPLE_ESCAPES:
        return _ANSI_C_SIMPLE_ESCAPES[character], start + 1
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
        uppercased = controlled.upper()
        value = (
            127
            if controlled == "?"
            else ord(uppercased if len(uppercased) == 1 else controlled) & 0x1F
        )
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
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-ansi-c.unrepresentable-codepoint",
                "eval ANSI-C escape cannot be represented",
            )
        )
    return chr(value)


def _eval_syntax_outside(
    quote: str | None,
    environment_variables: tuple[tuple[str, _ContentValue], ...] = (),
) -> _EvalSyntaxValue:
    """Keep unknown external text non-evidentiary without inventing quote syntax."""
    return frozenset(
        _EvalSyntaxState(
            summary,
            quote,
            environment_variables=environment_variables,
        )
        for summary in _OUTSIDE_VALUE
    )


_EvalSyntaxMergeKey: TypeAlias = tuple[  # noqa: UP040
    str | None,
    tuple[_ContentToken, ...],
    int,
    int,
    tuple[tuple[str, _ContentValue], ...],
    tuple[tuple[str, _ContentValue], ...],
    tuple[tuple[str, _ContentValue], ...],
    frozenset[str],
    str,
    tuple[_AssignmentEvidence, ...],
    tuple[tuple[_SecondPassConditionalAssignment, bool], ...],
    _DfaTransfer,
    _DfaTransfer,
    bool,
    _DfaTransfer,
    bool,
]


def _merge_eval_syntax_states(states: Iterable[_EvalSyntaxState]) -> _EvalSyntaxValue:
    """Merge equal eval-syntax behavior while retaining bounded literal alternatives.

    Mirrors ``_merge_content_summaries`` for ``_EvalSyntaxValue``. Without this, the join that
    accumulates alternatives for one eval-syntax variable was a bare set union over exact
    ``literal_texts``: a self-referential append composed a new, wholly distinct literal on
    every replay of the write history, so the join walked ``a``, ``ab``, ``abb``, and so on
    instead of collapsing states that behave identically except for that exact text (issue
    #140). States that share every field but the summary's literal projection collapse into one
    entry, widening to ``projection_incomplete`` once their combined literal alternatives pass
    ``_MAX_TRACKED_LITERAL_ALTERNATIVES`` instead of growing without bound.
    """
    merged: dict[_EvalSyntaxMergeKey, _EvalSyntaxState] = {}
    for state in states:
        summary = state.summary
        key: _EvalSyntaxMergeKey = (
            state.quote,
            state.brace_tokens,
            state.brace_depth,
            state.applied_appends,
            state.local_variables,
            state.environment_variables,
            state.fixed_point_overrides,
            state.definitely_set_variables,
            state.parameter_text,
            state.conditional_assignments,
            state.conditional_decisions,
            summary.full,
            summary.stripped,
            summary.newline_only,
            summary.first_record,
            summary.record_open,
        )
        prior = merged.get(key)
        if prior is None:
            merged[key] = state
            continue
        if prior == state:
            continue
        prior_summary = prior.summary
        literal_texts = prior_summary.literal_texts | summary.literal_texts
        projection_incomplete = (
            prior_summary.projection_incomplete
            or summary.projection_incomplete
            or len(literal_texts) > _MAX_TRACKED_LITERAL_ALTERNATIVES
        )
        merged[key] = replace(
            prior,
            summary=replace(
                prior_summary,
                literal_texts=(frozenset() if projection_incomplete else literal_texts),
                projection_opaque=(prior_summary.projection_opaque or summary.projection_opaque),
                projection_incomplete=projection_incomplete,
            ),
        )
    return frozenset(merged.values())


def _cap_eval_syntax(value: _EvalSyntaxValue, limits: TaintLimits) -> _EvalSyntaxValue:
    merged = _merge_eval_syntax_states(value)
    if len(merged) > limits.max_alternatives:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.alternative-limit",
                "shell taint eval syntax alternative limit exceeded",
            )
        )
    return merged


def _eval_environment_variables(
    environment: int | None,
    environment_index: dict[int, tuple[tuple[str, _ContentValue], ...]],
) -> tuple[tuple[str, _ContentValue], ...]:
    """Expose one scoped shell environment under second-pass parameter names."""
    if environment is None:
        return ()
    return environment_index.get(environment, ())


def _eval_environment_index(
    raw_variables: dict[str | int, _ContentValue],
) -> dict[int, tuple[tuple[str, _ContentValue], ...]]:
    """Group solved scoped variables once for all eval syntax writes."""
    grouped: dict[int, list[tuple[str, _ContentValue]]] = {}
    for name, value in raw_variables.items():
        environment = _scoped_variable_environment(name)
        if environment is None or not isinstance(name, str):
            continue
        grouped.setdefault(environment, []).append((_unscoped_variable_name(name), value))
    return {environment: tuple(sorted(variables)) for environment, variables in grouped.items()}


def _eval_syntax_set_local(
    state: _EvalSyntaxState,
    name: str,
    value: _ContentValue,
) -> _EvalSyntaxState:
    """Return one state with a deterministic branch-local variable binding."""
    variables = dict(state.local_variables)
    variables[name] = value
    return replace(
        state,
        local_variables=tuple(sorted(variables.items())),
        definitely_set_variables=state.definitely_set_variables | {name},
    )


def _eval_syntax_record_assignment(
    state: _EvalSyntaxState,
    assignment: _SecondPassConditionalAssignment,
    content: ContentExpr,
    limits: TaintLimits,
) -> _EvalSyntaxState:
    """Retain one taken eval assignment branch under the shared edge cap."""
    evidence = _AssignmentEvidence(assignment.name, content)
    assignments = (*state.conditional_assignments, evidence)
    if len(assignments) > limits.max_edges:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.record-assignment-edge-limit", "shell taint edge limit exceeded"
            )
        )
    return replace(state, conditional_assignments=assignments)


def _eval_syntax_record_decision(
    state: _EvalSyntaxState,
    assignment: _SecondPassConditionalAssignment,
    taken: bool,
    limits: TaintLimits,
) -> _EvalSyntaxState:
    """Retain one parameter-operator branch for correlated enclosing operands."""
    decisions = (*state.conditional_decisions, (assignment, taken))
    if len(decisions) > limits.max_edges:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.record-decision-edge-limit", "shell taint edge limit exceeded"
            )
        )
    return replace(state, conditional_decisions=decisions)


def _eval_syntax_resolve_conditionals(
    expression: ContentExpr,
    state: _EvalSyntaxState,
) -> ContentExpr:
    """Resolve nested default assignments according to this eval state's branch path."""
    if isinstance(expression, _SecondPassConditionalAssignment):
        decision = next(
            (
                taken
                for candidate, taken in reversed(state.conditional_decisions)
                if candidate == expression
            ),
            None,
        )
        if decision is False:
            return _SecondPassVariableRef(expression.name)
        operand = _eval_syntax_resolve_conditionals(expression.operand, state)
        if decision is True:
            return operand
        return choice(_SecondPassVariableRef(expression.name), operand)
    if isinstance(
        expression,
        LiteralTransfer
        | VariableRef
        | _SecondPassVariableRef
        | OutsideGap
        | ResourceRef
        | StreamRef,
    ):
        return expression
    if isinstance(expression, Choice):
        return choice(
            *(_eval_syntax_resolve_conditionals(part, state) for part in expression.parts)
        )
    return concat(*(_eval_syntax_resolve_conditionals(part, state) for part in expression.parts))


def _eval_syntax_assignment_branches(
    assignment: _SecondPassConditionalAssignment,
    state: _EvalSyntaxState,
    context: _EvalSyntaxContext,
) -> tuple[bool, bool]:
    """Return whether retained-value and assignment branches remain reachable."""
    if assignment.name not in state.definitely_set_variables:
        return True, True
    existing = context.variables_for(state).get(assignment.name)
    if existing is None:
        return True, True
    if not assignment.assign_if_null:
        return True, False
    return (any(summary != _EPSILON for summary in existing), _EPSILON in existing)


def _eval_syntax_merge_locals(
    left: tuple[tuple[str, _ContentValue], ...],
    right: tuple[tuple[str, _ContentValue], ...],
) -> tuple[tuple[str, _ContentValue], ...]:
    """Merge sequential eval-local writes, with later syntax taking precedence."""
    variables = dict(left)
    variables.update(right)
    return tuple(sorted(variables.items()))


def _eval_syntax_merge_assignments(
    left: tuple[_AssignmentEvidence, ...],
    right: tuple[_AssignmentEvidence, ...],
    limits: TaintLimits,
) -> tuple[_AssignmentEvidence, ...]:
    """Join sequential taken assignment branches without exceeding discovery bounds."""
    assignments = (*left, *right)
    if len(assignments) > limits.max_edges:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.merge-assignment-edge-limit", "shell taint edge limit exceeded"
            )
        )
    return assignments


def _eval_syntax_merge_decisions(
    left: tuple[tuple[_SecondPassConditionalAssignment, bool], ...],
    right: tuple[tuple[_SecondPassConditionalAssignment, bool], ...],
    limits: TaintLimits,
) -> tuple[tuple[_SecondPassConditionalAssignment, bool], ...]:
    """Join sequential parameter branch decisions under the shared edge cap."""
    decisions = (*left, *right)
    if len(decisions) > limits.max_edges:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.merge-decision-edge-limit", "shell taint edge limit exceeded"
            )
        )
    return decisions


def _eval_syntax_token(  # noqa: PLR0911, PLR0912
    state: _EvalSyntaxState,
    token: _ContentToken,
    context: _EvalSyntaxContext,
) -> _EvalSyntaxValue:
    """Append one eval token while retaining an unmatched active brace operand."""
    limits = context.limits
    if isinstance(token.expression, _SecondPassConditionalAssignment):
        retain_existing, take_assignment = _eval_syntax_assignment_branches(
            token.expression,
            state,
            context,
        )
        retained: _EvalSyntaxValue = frozenset()
        if retain_existing:
            retained = frozenset(
                _eval_syntax_record_decision(
                    after,
                    token.expression,
                    False,
                    limits,
                )
                for after in _eval_syntax_token(
                    state,
                    _ContentToken(_SecondPassVariableRef(token.expression.name), "", False),
                    context,
                )
            )
        assigned_with_bindings: _EvalSyntaxValue = frozenset()
        if take_assignment:
            assigned = _eval_syntax_token(
                state,
                _ContentToken(token.expression.operand, "", False),
                context,
            )
            assigned_with_bindings = frozenset(
                _eval_syntax_record_decision(
                    _eval_syntax_record_assignment(
                        bound,
                        token.expression,
                        _eval_syntax_resolve_conditionals(
                            token.expression.operand,
                            bound,
                        ),
                        limits,
                    ),
                    token.expression,
                    True,
                    limits,
                )
                for after in assigned
                for summary in _evaluate_with_tables(
                    token.expression.operand,
                    context.variables_for(after),
                    {},
                    {},
                    limits,
                )
                for bound in (
                    _eval_syntax_set_local(
                        after,
                        token.expression.name,
                        frozenset({summary}),
                    ),
                )
            )
        return _cap_eval_syntax(retained | assigned_with_bindings, limits)
    if isinstance(token.expression, Choice):
        return _cap_eval_syntax(
            frozenset(
                after
                for part in token.expression.parts
                for after in _eval_syntax_token(
                    state,
                    _ContentToken(part, "", False),
                    context,
                )
            ),
            limits,
        )
    if isinstance(token.expression, Concat):
        value: _EvalSyntaxValue = frozenset({state})
        for part in token.expression.parts:
            value = _cap_eval_syntax(
                frozenset(
                    after
                    for current in value
                    for after in _eval_syntax_token(
                        current,
                        _ContentToken(part, "", False),
                        context,
                    )
                ),
                limits,
            )
        return value
    if not state.brace_tokens and _active_character(token, "{"):
        return frozenset(
            {
                replace(
                    state,
                    brace_tokens=(token,),
                    brace_depth=1,
                )
            }
        )
    if state.brace_tokens:
        brace_tokens = (*state.brace_tokens, token)
        if len(brace_tokens) > limits.max_expression_nodes:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-syntax.brace-token-node-limit",
                    "shell taint expression node limit exceeded",
                )
            )
        brace_depth = state.brace_depth
        if _active_character(token, "{"):
            brace_depth += 1
            if brace_depth > limits.max_brace_depth:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-syntax.brace-depth-limit",
                        "shell taint brace expansion depth limit exceeded",
                    )
                )
        elif _active_character(token, "}"):
            brace_depth -= 1
        if brace_depth:
            return frozenset(
                {
                    replace(
                        state,
                        brace_tokens=brace_tokens,
                        brace_depth=brace_depth,
                    )
                }
            )
        expanded_words = _expand_braces(list(brace_tokens), limits)
        value = frozenset(
            replace(
                state,
                summary=state.summary.compose(after),
                brace_tokens=(),
                brace_depth=0,
            )
            for expanded in expanded_words
            for after in _evaluate_with_tables(
                _token_content(expanded),
                context.variables_for(state),
                {},
                {},
                limits,
            )
        )
        return _cap_eval_syntax(value, limits)
    value = frozenset(
        replace(state, summary=state.summary.compose(after))
        for after in _evaluate_with_tables(
            token.expression,
            context.variables_for(state),
            {},
            {},
            limits,
        )
    )
    return _cap_eval_syntax(value, limits)


_SECOND_PASS_ACTIVE_CHARACTERS = frozenset("\\'\"$`{}")


def _second_pass_inert(value: _ContentValue) -> bool:
    """Return whether eval's second parse would leave every alternative's text unchanged."""
    for alternative in value:
        if alternative.projection_incomplete:
            return False
        if any(
            character in _SECOND_PASS_ACTIVE_CHARACTERS
            for literal_text in alternative.literal_texts
            for character in literal_text
        ):
            return False
    return True


def _eval_syntax_variable_transition(
    name: str | int,
    state: _EvalSyntaxState,
    context: _EvalSyntaxContext,
    *,
    depth: int,
) -> _EvalSyntaxValue:
    """Replay one variable's authored writes against an actual incoming eval state."""
    fixed_point_overrides = dict(state.fixed_point_overrides)
    snapshot = fixed_point_overrides.get(name)
    raw = context.raw_variables.get(name)
    if (
        snapshot is not None
        and snapshot != raw
        and not _marker_capable(snapshot)
        and not state.parameter_text
        and _second_pass_inert(snapshot)
    ):
        return _cap_eval_syntax(
            frozenset(
                replace(state, summary=state.summary.compose(summary)) for summary in snapshot
            ),
            context.limits,
        )
    key = (name, state)
    cached = context.transitions.get(key)
    if cached is not None:
        context.inherit_observations(cached.observed)
        return cached.value
    if key in context.active_transitions:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.transition-reentry",
                "shell taint eval syntax fixed-point update limit exceeded",
            )
        )
    program = context.programs.get(name)
    if program is None:
        if state.parameter_text:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-syntax.unbounded-parameter-program",
                    "shell taint eval parameter expansion cannot be bounded",
                )
            )
        return _eval_syntax_token(
            state,
            _ContentToken(OutsideGap(), "", False),
            context,
        )
    context.active_transitions.add(key)
    context.begin_observations()
    try:
        value = _replay_eval_syntax_program(state, program, context, depth=depth)
    finally:
        observed = context.end_observations()
        context.active_transitions.remove(key)
    if len(context.transitions) >= context.limits.max_table_entries:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.transition-table-limit",
                "shell taint eval syntax transition table limit exceeded",
            )
        )
    context.transitions[key] = _EvalSyntaxTransition(value, observed)
    return value


def _replay_eval_syntax_program(
    state: _EvalSyntaxState,
    program: tuple[tuple[int, _FlowWrite], ...],
    context: _EvalSyntaxContext,
    *,
    depth: int,
) -> _EvalSyntaxValue:
    """Widen one variable's write program to a fixed point from one incoming eval state."""
    value: _EvalSyntaxValue = frozenset()
    changed = True
    while changed:
        changed = False
        for write_index, write in program:
            if write.append:
                base = value
                if not base:
                    base = _eval_syntax_token(
                        state,
                        _ContentToken(OutsideGap(), "", False),
                        context,
                    )
                produced = frozenset(
                    after
                    for current in base
                    for after in _eval_syntax_append_write(
                        write.expression,
                        current,
                        write_index,
                        context,
                        depth=depth + 1,
                    )
                )
            else:
                produced = _eval_syntax_append(
                    write.expression,
                    state,
                    context,
                    depth=depth + 1,
                )
            widened = _cap_eval_syntax(value | produced, context.limits)
            if widened == value:
                continue
            value = widened
            context.transition_updates += 1
            if context.transition_updates > context.limits.max_fixed_point_updates:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-syntax.replay-fixed-point-limit",
                        "shell taint eval syntax fixed-point update limit exceeded",
                    )
                )
            changed = True
    return value


def _eval_syntax_append(
    expression: ContentExpr,
    state: _EvalSyntaxState,
    context: _EvalSyntaxContext,
    *,
    depth: int = 0,
) -> _EvalSyntaxValue:
    """Append authored eval syntax to one bounded streaming parse state."""
    if depth > context.limits.max_eval_reparse_depth:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-syntax.append-depth-limit",
                "shell taint eval reparse depth limit exceeded",
            )
        )
    if isinstance(expression, LiteralTransfer):
        tokens, resulting_quote, pending_parameter = _eval_reparse_tokens_streaming(
            f"{state.parameter_text}{expression.text}",
            state.quote,
            context.limits,
            defer_incomplete_parameter=True,
        )
        value: _EvalSyntaxValue = frozenset({replace(state, parameter_text="")})
        for token in tokens:
            value = _cap_eval_syntax(
                frozenset(
                    after
                    for current in value
                    for after in _eval_syntax_token(
                        current,
                        token,
                        context,
                    )
                ),
                context.limits,
            )
        return frozenset(
            replace(
                current,
                quote=resulting_quote,
                parameter_text=pending_parameter,
            )
            for current in value
        )
    if isinstance(expression, VariableRef):
        transition_key = (expression.name, state)
        if transition_key in context.active_transitions:
            if state.brace_tokens or state.parameter_text:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-syntax.append-transition-reentry",
                        "shell taint eval syntax fixed-point update limit exceeded",
                    )
                )
            slot = (expression.name, state.quote)
            context.observe_slot(slot)
            suffixes = context.variables.get(slot, _eval_syntax_outside(state.quote))
            return _cap_eval_syntax(
                frozenset(
                    replace(
                        state,
                        summary=state.summary.compose(suffix.summary),
                        quote=suffix.quote,
                        brace_tokens=suffix.brace_tokens,
                        brace_depth=suffix.brace_depth,
                        applied_appends=state.applied_appends | suffix.applied_appends,
                        parameter_text=suffix.parameter_text,
                        local_variables=_eval_syntax_merge_locals(
                            state.local_variables,
                            suffix.local_variables,
                        ),
                        definitely_set_variables=(
                            state.definitely_set_variables | suffix.definitely_set_variables
                        ),
                        conditional_assignments=_eval_syntax_merge_assignments(
                            state.conditional_assignments,
                            suffix.conditional_assignments,
                            context.limits,
                        ),
                        conditional_decisions=_eval_syntax_merge_decisions(
                            state.conditional_decisions,
                            suffix.conditional_decisions,
                            context.limits,
                        ),
                    )
                    for suffix in suffixes
                ),
                context.limits,
            )
        return _eval_syntax_variable_transition(
            expression.name,
            state,
            context,
            depth=depth + 1,
        )
    if isinstance(expression, OutsideGap | ResourceRef | StreamRef):
        if state.parameter_text:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-syntax.append-unbounded-expansion",
                    "shell taint eval parameter expansion cannot be bounded",
                )
            )
        return _eval_syntax_token(
            state,
            _ContentToken(expression, "", False),
            context,
        )
    if isinstance(expression, Choice):
        value = frozenset(
            alternative
            for part in expression.parts
            for alternative in _eval_syntax_append(
                part,
                state,
                context,
                depth=depth + 1,
            )
        )
        return _cap_eval_syntax(value, context.limits)
    if not isinstance(expression, Concat):
        # Returning the outside value here would discard the incoming state, including a marker
        # prefix already accumulated, and read as "no marker". An unhandled node type is missing
        # evidence, so it has to fail closed like every other unrepresentable shape.
        raise _MalformedTaintEvidence(
            GuardRefusal(
                "taint.eval-syntax.append-non-concat",
                "shell taint eval syntax expression cannot be structured",
            )
        )
    value = frozenset({state})
    for part in _coalesced_eval_parts(expression.parts):
        value = _cap_eval_syntax(
            frozenset(
                after
                for current in value
                for after in _eval_syntax_append(
                    part,
                    current,
                    context,
                    depth=depth + 1,
                )
            ),
            context.limits,
        )
    return value


def _eval_syntax_append_write(
    expression: ContentExpr,
    state: _EvalSyntaxState,
    write_index: int,
    context: _EvalSyntaxContext,
    *,
    depth: int = 0,
) -> _EvalSyntaxValue:
    """Apply one append write at most once to each fixed-point derivation."""
    write_mask = 1 << write_index
    if state.applied_appends & write_mask:
        return frozenset()
    return frozenset(
        replace(after, applied_appends=after.applied_appends | write_mask)
        for after in _eval_syntax_append(
            expression,
            state,
            context,
            depth=depth,
        )
    )


def _eval_syntax_expression(  # noqa: PLR0913
    expression: ContentExpr,
    quote: str | None,
    context: _EvalSyntaxContext,
    *,
    depth: int = 0,
    environment: int | None = None,
    environment_variables: tuple[tuple[str, _ContentValue], ...] | None = None,
    fixed_point_overrides: tuple[tuple[str, _ContentValue], ...] = (),
    definitely_set_variables: frozenset[str] = frozenset(),
) -> _EvalSyntaxValue:
    """Evaluate eval syntax from one empty bounded streaming parse state."""
    return _eval_syntax_append(
        expression,
        _EvalSyntaxState(
            _EPSILON,
            quote,
            environment_variables=(
                _eval_environment_variables(environment, context.environment_index)
                if environment_variables is None
                else environment_variables
            ),
            fixed_point_overrides=fixed_point_overrides,
            definitely_set_variables=definitely_set_variables,
        ),
        context,
        depth=depth,
    )


def _finalize_eval_syntax(
    state: _EvalSyntaxState,
    context: _EvalSyntaxContext,
) -> _ContentValue:
    """Treat a still-unmatched brace buffer as literal text at the eval sink."""
    if not state.brace_tokens:
        return frozenset({state.summary})
    return frozenset(
        state.summary.compose(after)
        for after in _evaluate_with_tables(
            _token_content(list(state.brace_tokens)),
            context.variables_for(state),
            {},
            {},
            context.limits,
        )
    )


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


def _ordered_eval_syntax_variables(
    writes: tuple[_FlowWrite, ...],
    raw_variables: dict[str | int, _ContentValue],
    limits: TaintLimits,
    solved_variables: dict[tuple[str | int, str | None], _EvalSyntaxValue],
) -> dict[tuple[str | int, str | None], _EvalSyntaxValue]:
    """Apply writes once in source order for eval's second parsing pass.

    The ordered result accumulates separately from the table a cycle-detected read falls back on.
    That fallback is ``_solve_eval_syntax_variables``' solved table, which is fixed for the whole
    pass, so a memoized transition stays valid across writes instead of being invalidated by every
    one of them (issue #149). Feeding the fallback a half-built ordered table also read
    not-yet-written slots as the empty set, which drops an alternative rather than widening it.
    Append accumulation still seeds from the ordered value, which is the order-sensitive behavior
    AD-18 records.
    """
    variables = {
        (write.key, quote): frozenset() for write in writes for quote in _EVAL_QUOTE_STATES
    }
    context = _EvalSyntaxContext(
        dict(solved_variables),
        raw_variables,
        limits,
        _eval_syntax_programs(writes),
    )
    for write_index, write in enumerate(writes):
        environment = _scoped_variable_environment(write.key)
        environment_variables = _eval_environment_variables(
            environment,
            context.environment_index,
        )
        for quote in _EVAL_QUOTE_STATES:
            if write.append:
                base = variables[(write.key, quote)] or _eval_syntax_outside(
                    quote,
                    environment_variables,
                )
                value = _cap_eval_syntax(
                    frozenset(
                        after
                        for current in base
                        for after in _eval_syntax_append_write(
                            write.expression,
                            current,
                            write_index,
                            context,
                        )
                    ),
                    limits,
                )
            else:
                value = _eval_syntax_expression(
                    write.expression,
                    quote,
                    context,
                    environment=environment,
                )
            variables[(write.key, quote)] = value
    return variables


def _solve_eval_syntax_variables(
    writes: tuple[_FlowWrite, ...],
    raw_variables: dict[str | int, _ContentValue],
    limits: TaintLimits,
) -> dict[tuple[str | int, str | None], _EvalSyntaxValue]:
    """Solve quote-sensitive variable text only for eval's second parsing pass."""
    variables = {
        (write.key, quote): frozenset() for write in writes for quote in _EVAL_QUOTE_STATES
    }
    context = _EvalSyntaxContext(
        variables,
        raw_variables,
        limits,
        _eval_syntax_programs(writes),
    )
    updates = 0
    changed = True
    while changed:
        changed = False
        for write_index, write in enumerate(writes):
            environment = _scoped_variable_environment(write.key)
            environment_variables = _eval_environment_variables(
                environment,
                context.environment_index,
            )
            for quote in _EVAL_QUOTE_STATES:
                prior = variables[(write.key, quote)]
                if write.append:
                    base = prior or _eval_syntax_outside(
                        quote,
                        environment_variables,
                    )
                    value = _cap_eval_syntax(
                        frozenset(
                            after
                            for current in base
                            for after in _eval_syntax_append_write(
                                write.expression,
                                current,
                                write_index,
                                context,
                            )
                        ),
                        limits,
                    )
                else:
                    value = _eval_syntax_expression(
                        write.expression,
                        quote,
                        context,
                        environment=environment,
                    )
                widened = _cap_eval_syntax(prior | value, limits)
                if widened == prior:
                    continue
                variables[(write.key, quote)] = widened
                context.invalidate_transitions((write.key, quote))
                updates += 1
                if updates > limits.max_fixed_point_updates:
                    raise _TaintLimitExceeded(
                        GuardRefusal(
                            "taint.eval-syntax.solve-fixed-point-limit",
                            "shell taint eval syntax fixed-point update limit exceeded",
                        )
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


def _eval_content_dependencies(
    expression: ContentExpr,
    limits: TaintLimits,
) -> set[str]:
    """Collect variable names that can enter eval syntax from one authored expression."""
    names: set[str] = set()
    pending = [_eval_reparse_content(expression, limits)]
    while pending:
        current = pending.pop()
        if isinstance(current, VariableRef):
            names.add(current.name)
        elif isinstance(current, Choice | Concat | _SecondPassConditionalAssignment):
            pending.extend(current.parts)
    return names


def _lower_eval_assignment_operand(
    expression: ContentExpr,
    environment: int,
    *,
    scoped: bool,
) -> ContentExpr:
    """Bind eval-time parameter reads while lowering one persisted assignment value."""
    if isinstance(expression, _SecondPassVariableRef):
        return VariableRef(
            _scoped_variable_name(environment, expression.name) if scoped else expression.name
        )
    if isinstance(expression, _SecondPassConditionalAssignment):
        variable = VariableRef(
            _scoped_variable_name(environment, expression.name) if scoped else expression.name
        )
        return choice(
            variable,
            _lower_eval_assignment_operand(
                expression.operand,
                environment,
                scoped=scoped,
            ),
        )
    if isinstance(
        expression,
        LiteralTransfer | VariableRef | OutsideGap | ResourceRef | StreamRef,
    ):
        return expression
    if isinstance(expression, Choice):
        return choice(
            *(
                _lower_eval_assignment_operand(part, environment, scoped=scoped)
                for part in expression.parts
            )
        )
    return concat(
        *(
            _lower_eval_assignment_operand(part, environment, scoped=scoped)
            for part in expression.parts
        )
    )


def _environment_inherits(
    origin: int,
    target: int,
    environment_parents: dict[int, int | None],
) -> bool:
    """Return whether writes in ``origin`` are visible from ``target``."""
    current: int | None = target
    visited: set[int] = set()
    while current is not None and current not in visited:
        if current == origin:
            return True
        visited.add(current)
        current = environment_parents.get(current)
    return False


def _wrapped_builtin_index(command: _CommandEvidence, name: str) -> int | None:
    """Resolve exact nested ``command``/``builtin`` wrappers around one builtin."""
    index = 0
    wrapped = False
    while index < len(command.argv):
        argument = command.argv[index]
        if argument.dynamic:
            return None
        if argument.literal == name:
            return index if wrapped else None
        if argument.literal == "builtin":
            wrapped = True
            index += 1
            if index < len(command.argv) and command.argv[index].literal == "--":
                index += 1
            continue
        if argument.literal != "command":
            return None
        wrapped = True
        index += 1
        while index < len(command.argv):
            option = command.argv[index]
            if option.dynamic:
                return None
            if option.literal == "--":
                index += 1
                break
            if option.literal.startswith("-") and option.literal != "-":
                flags = option.literal[1:]
                if "v" in flags or "V" in flags or set(flags) - {"p"}:
                    return None
                index += 1
                continue
            break
    return None


def _builtin_executable_index(command: _CommandEvidence, name: str) -> int | None:
    """Return the argv index for one statically resolved builtin invocation."""
    direct_index = next(
        (
            candidate.argv_index
            for candidate in _iter_executable_evidence(command.executable)
            if candidate.name == name
            and candidate.literal == name
            and not candidate.external_lookup
            and candidate.argv_index is not None
        ),
        None,
    )
    return direct_index if direct_index is not None else _wrapped_builtin_index(command, name)


def _shopt_executable_index(command: _CommandEvidence) -> int | None:
    """Return the argv index for a statically resolved ``shopt`` invocation."""
    return _builtin_executable_index(command, "shopt")


def _static_variable_name(value: str) -> bool:
    """Return whether ``value`` is an exact portable shell variable name."""
    return (
        bool(value)
        and (value[0].isascii() and (value[0].isalpha() or value[0] == "_"))
        and all(
            character.isascii() and (character.isalnum() or character == "_")
            for character in value[1:]
        )
    )


def _unset_action(command: _CommandEvidence) -> tuple[tuple[str, ...], bool]:
    """Return exact variable unsets plus whether an unknown target may be unset."""
    executable_index = _builtin_executable_index(command, "unset")
    if executable_index is None:
        return (), False
    names: list[str] = []
    unknown = False
    options_enabled = True
    function_only = False
    for argument in command.argv[executable_index + 1 :]:
        if options_enabled and argument.dynamic:
            unknown = True
            continue
        if options_enabled and argument.literal == "--":
            options_enabled = False
            continue
        if options_enabled and argument.literal.startswith("-") and argument.literal != "-":
            flags = argument.literal[1:]
            if set(flags) - {"f", "n", "v"}:
                unknown = True
            function_only = "f" in flags and "v" not in flags
            continue
        if function_only:
            continue
        if argument.dynamic or not _static_variable_name(argument.literal):
            unknown = True
        else:
            names.append(argument.literal)
    return tuple(names), unknown


def _unset_nameref_action(command: _CommandEvidence) -> tuple[tuple[str, ...], bool]:
    """Return exact ``unset -n`` aliases plus whether their removal is ambiguous."""
    executable_index = _builtin_executable_index(command, "unset")
    if executable_index is None:
        return (), False
    names: list[str] = []
    unknown = False
    options_enabled = True
    nameref_only = False
    for argument in command.argv[executable_index + 1 :]:
        if options_enabled and argument.dynamic:
            unknown = True
            continue
        if options_enabled and argument.literal == "--":
            options_enabled = False
            continue
        if options_enabled and argument.literal.startswith("-") and argument.literal != "-":
            flags = argument.literal[1:]
            if set(flags) - {"f", "n", "v"} or ("f" in flags and "n" in flags):
                unknown = True
            nameref_only = nameref_only or "n" in flags
            continue
        if argument.dynamic or not _static_variable_name(argument.literal):
            unknown = True
        else:
            names.append(argument.literal)
    if not nameref_only:
        return (), False
    return tuple(names), unknown


def _shopt_lastpipe_action(command: _CommandEvidence) -> bool | None:  # noqa: PLR0912
    """Return one authored transition that may change Bash ``lastpipe``."""
    executable_index = _shopt_executable_index(command)
    if executable_index is None:
        return None
    mode: bool | None = None
    saw_set = False
    saw_unset = False
    names: list[_ArgPort] = []
    dynamic_option = False
    for argument in command.argv[executable_index + 1 :]:
        if argument.dynamic:
            if mode is None:
                dynamic_option = True
            else:
                names.append(argument)
        elif argument.literal.startswith("-") and argument.literal != "-":
            if "s" in argument.literal[1:]:
                mode = True
                saw_set = True
            if "u" in argument.literal[1:]:
                mode = False
                saw_unset = True
        else:
            names.append(argument)
    if saw_set and saw_unset:
        return None
    mentions_lastpipe = any(argument.literal == "lastpipe" for argument in names)
    dynamic_name = any(argument.dynamic for argument in names)
    if dynamic_option and (not names or mentions_lastpipe or dynamic_name):
        return True
    if mode is True and (mentions_lastpipe or dynamic_name):
        return True
    if mode is False and mentions_lastpipe:
        return None if dynamic_name else False
    return None


@dataclass(frozen=True, slots=True)
class _ShellExecutionContext:
    """One persistent shell environment introduced by asynchronous or pipeline execution."""

    kind: str
    context_id: int
    scope_id: int | None
    conditional_on_lastpipe: bool = False
    # Whether this pipe's consumer contains a ``read`` writing a value from stdin (issue #118).
    # The body-wide ``lastpipe`` override in ``_execution_environment_ids`` only applies to this
    # narrow shape, matching the issue's cheap interim rather than the full CFG may-analysis.
    pipeline_read: bool = False


def _control_command_ranges(
    evidence: _ShellTaintEvidence,
) -> tuple[tuple[int, int], ...]:
    """Return source-index ranges belonging to control compounds."""
    stack: list[tuple[str, int]] = []
    ranges: list[tuple[int, int]] = []
    openers = {
        "if": "fi",
        "for": "done",
        "select": "done",
        "while": "done",
        "until": "done",
    }
    for index, command in enumerate(evidence.commands):
        head = command.argv[0].literal if command.argv else None
        if head in openers:
            stack.append((openers[head], index))
        elif stack and head == stack[-1][0]:
            _closing, start = stack.pop()
            ranges.append((start, index))
        elif head == "esac":
            start = index
            while start > 0 and evidence.commands[start - 1].conditionally_executed:
                start -= 1
            ranges.append((start, index))
    return tuple(ranges)


def _command_scope_paths(
    evidence: _ShellTaintEvidence,
) -> dict[int, tuple[int, ...]]:
    """Return each command's structured scope ancestry from outermost to innermost."""
    scopes = {scope.scope_id: scope for scope in evidence.scopes}
    paths: dict[int, tuple[int, ...]] = {}
    for command in evidence.commands:
        reverse_path: list[int] = []
        current: int | None = command.container_scope_id
        visited: set[int] = set()
        while current is not None and current not in visited:
            visited.add(current)
            reverse_path.append(current)
            scope = scopes.get(current)
            current = scope.parent_scope_id if scope is not None else None
        paths[command.command_id] = tuple(reversed(reverse_path))
    return paths


def _command_execution_contexts(  # noqa: PLR0912
    evidence: _ShellTaintEvidence,
) -> dict[int, tuple[_ShellExecutionContext, ...]]:
    """Index stable isolated shell contexts containing each command."""
    scopes = {scope.scope_id: scope for scope in evidence.scopes}
    paths = _command_scope_paths(evidence)
    commands_by_scope: dict[int, set[int]] = {}
    for command_id, path in paths.items():
        for scope_id in path:
            commands_by_scope.setdefault(scope_id, set()).add(command_id)

    contexts: dict[int, dict[tuple[str, int], _ShellExecutionContext]] = {
        command.command_id: {} for command in evidence.commands
    }
    output_commands = {command.output_scope_id: command.command_id for command in evidence.commands}
    output_scopes = {command.command_id: command.output_scope_id for command in evidence.commands}
    commands_by_id = {command.command_id: command for command in evidence.commands}

    def is_pipeline_read(command_id: int) -> bool:
        # Identify the ``read`` builtin through ``executable`` identity rather than
        # ``builtin_assignments[*].from_stdin``: ``_resolve_builtin_writer_evidence`` routes that
        # flag to ``False`` once a read is projected, and this predicate must stay correct across
        # every stage that re-derives execution environments (issue #118), including after that
        # routing has already run.
        command = commands_by_id.get(command_id)
        if command is None:
            return False
        return any(
            executable.name == "read"
            and executable.literal == "read"
            and not executable.external_lookup
            for executable in _iter_executable_evidence(command.executable)
        )

    def add(command_id: int, context: _ShellExecutionContext) -> None:
        key = (context.kind, context.context_id)
        prior = contexts[command_id].get(key)
        if (
            prior is None
            or (prior.conditional_on_lastpipe and not context.conditional_on_lastpipe)
            or (prior.scope_id is None and context.scope_id is not None)
        ):
            contexts[command_id][key] = context

    for pipe in evidence.pipes:
        producer_command = output_commands.get(pipe.producer_scope_id)
        if producer_command is not None:
            add(
                producer_command,
                _ShellExecutionContext("pipeline", pipe.producer_scope_id, None),
            )
        else:
            for command_id in commands_by_scope.get(pipe.producer_scope_id, ()):
                add(
                    command_id,
                    _ShellExecutionContext(
                        "pipeline",
                        pipe.producer_scope_id,
                        pipe.producer_scope_id,
                    ),
                )
        if pipe.consumer_command_id is not None:
            add(
                pipe.consumer_command_id,
                _ShellExecutionContext(
                    "pipeline",
                    output_scopes[pipe.consumer_command_id],
                    None,
                    conditional_on_lastpipe=True,
                    pipeline_read=is_pipeline_read(pipe.consumer_command_id),
                ),
            )
        elif pipe.consumer_scope_id is not None:
            consumer_command_ids = commands_by_scope.get(pipe.consumer_scope_id, ())
            consumer_pipeline_read = any(
                is_pipeline_read(command_id) for command_id in consumer_command_ids
            )
            for command_id in consumer_command_ids:
                add(
                    command_id,
                    _ShellExecutionContext(
                        "pipeline",
                        pipe.consumer_scope_id,
                        pipe.consumer_scope_id,
                        conditional_on_lastpipe=True,
                        pipeline_read=consumer_pipeline_read,
                    ),
                )

    for command in evidence.commands:
        if not command.isolated_execution:
            continue
        context_id = (
            command.isolated_context_id
            if command.isolated_context_id is not None
            else command.output_scope_id
        )
        add(
            command.command_id,
            _ShellExecutionContext(
                "asynchronous",
                context_id,
                context_id if context_id in scopes else None,
            ),
        )

    for start, end in _control_command_ranges(evidence):
        command_ids = tuple(evidence.commands[index].command_id for index in range(start, end + 1))
        common_path = list(paths[command_ids[0]])
        for command_id in command_ids[1:]:
            path = paths[command_id]
            common_length = 0
            while (
                common_length < len(common_path)
                and common_length < len(path)
                and common_path[common_length] == path[common_length]
            ):
                common_length += 1
            del common_path[common_length:]
        anchor_scope_id = common_path[-1] if common_path else None
        direct_contexts = {
            key: context
            for command_id in command_ids
            for key, context in contexts[command_id].items()
            if context.scope_id is None
        }
        for command_id in command_ids:
            for context in direct_contexts.values():
                add(command_id, replace(context, scope_id=anchor_scope_id))

    ordered: dict[int, tuple[_ShellExecutionContext, ...]] = {}
    for command in evidence.commands:
        path = paths[command.command_id]
        depths = {scope_id: depth for depth, scope_id in enumerate(path)}
        ordered[command.command_id] = tuple(
            sorted(
                contexts[command.command_id].values(),
                key=lambda context: (
                    depths.get(context.scope_id, len(path)),
                    context.kind,
                    context.context_id,
                ),
            )
        )
    return ordered


def _execution_environment_ids(  # noqa: PLR0912, PLR0915
    evidence: _ShellTaintEvidence,
) -> tuple[dict[int, int], dict[int, int | None], dict[int, bool]]:
    """Return persistent command environments and their source-ordered lastpipe state.

    ``enabled()`` below is a single source-order forward pass, memoized per environment, so it
    mismodels a pipeline reached through a function call site or a loop back edge whose actual
    ``shopt -s lastpipe`` takes effect after the pipeline's textual position but before its
    runtime execution (issue #118). Rather than the full CFG may-analysis the issue describes,
    this applies the issue's cheap interim narrowly: a pipe whose last stage is a ``read``
    writing from stdin (``context.pipeline_read``) is treated as running in the current shell,
    and therefore persisting its write, whenever ``shopt -s lastpipe`` appears anywhere in the
    body -- not just when source order already proves it enabled. Every other conditional-on-
    lastpipe context (eval, or any other last-stage command) keeps the exact source-order state.
    """
    lexical_environments, lexical_parents = _scope_environment_ids(evidence.scopes)
    environment_parents = dict(lexical_parents)
    contexts = _command_execution_contexts(evidence)
    paths = _command_scope_paths(evidence)
    used_environments = set(lexical_environments.values())
    next_environment = -1
    context_environments: dict[tuple[int, str, int], int] = {}
    consumer_isolation: dict[tuple[int, str, int], bool] = {}
    enabled_by_environment: dict[int, bool] = {}
    conditional_commands = _control_conditional_commands(evidence)
    lastpipe_enabled_anywhere = any(
        _shopt_lastpipe_action(command) is True for command in evidence.commands
    )

    def allocate(parent: int, context: _ShellExecutionContext) -> int:
        nonlocal next_environment
        key = (parent, context.kind, context.context_id)
        cached = context_environments.get(key)
        if cached is not None:
            return cached
        while next_environment in used_environments:
            next_environment -= 1
        environment = next_environment
        next_environment -= 1
        used_environments.add(environment)
        context_environments[key] = environment
        environment_parents[environment] = parent
        return environment

    def enabled(environment: int) -> bool:
        if environment in enabled_by_environment:
            return enabled_by_environment[environment]
        parent = environment_parents.get(environment)
        inherited = enabled(parent) if parent is not None else False
        enabled_by_environment[environment] = inherited
        return inherited

    command_environments: dict[int, int] = {}
    lastpipe_states: dict[int, bool] = {}
    for command in evidence.commands:
        path = paths[command.command_id]
        by_depth: dict[int, list[_ShellExecutionContext]] = {}
        depths = {scope_id: depth for depth, scope_id in enumerate(path)}
        for context in contexts[command.command_id]:
            by_depth.setdefault(depths.get(context.scope_id, len(path)), []).append(context)

        environment: int | None = None
        prior_lexical: int | None = None
        for depth, scope_id in enumerate(path):
            lexical = lexical_environments.get(scope_id, scope_id)
            if environment is None:
                environment = lexical
            elif lexical != prior_lexical:
                environment_parents[lexical] = environment
                environment = lexical
            prior_lexical = lexical
            for context in by_depth.get(depth, ()):
                if context.conditional_on_lastpipe:
                    key = (environment, context.kind, context.context_id)
                    body_wide = lastpipe_enabled_anywhere and context.pipeline_read
                    isolated = consumer_isolation.setdefault(
                        key, not (enabled(environment) or body_wide)
                    )
                    if not isolated:
                        continue
                environment = allocate(environment, context)
        if environment is None:
            environment = command.container_scope_id
            environment_parents.setdefault(environment, None)
        for context in by_depth.get(len(path), ()):
            if context.conditional_on_lastpipe:
                key = (environment, context.kind, context.context_id)
                body_wide = lastpipe_enabled_anywhere and context.pipeline_read
                isolated = consumer_isolation.setdefault(
                    key, not (enabled(environment) or body_wide)
                )
                if not isolated:
                    continue
            environment = allocate(environment, context)

        command_environments[command.command_id] = environment
        lastpipe_states[command.command_id] = enabled(environment)
        action = _shopt_lastpipe_action(command)
        if action is None:
            continue
        conditional = command.conditionally_executed or command.command_id in conditional_commands
        if action:
            enabled_by_environment[environment] = True
        elif not conditional:
            enabled_by_environment[environment] = False
    return command_environments, environment_parents, lastpipe_states


def _lastpipe_states(evidence: _ShellTaintEvidence) -> dict[int, bool]:
    """Return whether authored option state may enable ``lastpipe`` per command."""
    _command_environments, _environment_parents, states = _execution_environment_ids(evidence)
    return states


def _control_conditional_commands(evidence: _ShellTaintEvidence) -> frozenset[int]:
    """Return commands whose surrounding shell control body may not execute."""
    conditional: set[int] = set()
    control_stack: list[str] = []
    openers = {
        "if": "fi",
        "for": "done",
        "select": "done",
        "while": "done",
        "until": "done",
        "case": "esac",
    }
    closers = frozenset(openers.values())
    for command in evidence.commands:
        head = command.argv[0].literal if command.argv else None
        if control_stack:
            conditional.add(command.command_id)
        if head in openers:
            control_stack.append(openers[head])
        elif head in closers and control_stack and head == control_stack[-1]:
            control_stack.pop()
    return frozenset(conditional)


def _append_eval_conditional_writes(  # noqa: PLR0913
    lowered: list[_FlowWrite],
    assignment: _AssignmentEvidence,
    origin_environment: int,
    target_environments: set[int],
    environment_parents: dict[int, int | None],
    *,
    scoped_variables: bool,
    limits: TaintLimits,
) -> None:
    """Append bounded scope-visible definitions for one eval-time assignment."""
    scoped_content = _lower_eval_assignment_operand(
        assignment.content,
        origin_environment,
        scoped=scoped_variables,
    )
    for target_environment in target_environments:
        if not _environment_inherits(
            origin_environment,
            target_environment,
            environment_parents,
        ):
            continue
        key = (
            _scoped_variable_name(target_environment, assignment.name)
            if scoped_variables
            else assignment.name
        )
        lowered.append(_FlowWrite(key, scoped_content))
    if scoped_variables and environment_parents.get(origin_environment) is None:
        lowered.append(
            _FlowWrite(
                assignment.name,
                _lower_eval_assignment_operand(
                    assignment.content,
                    origin_environment,
                    scoped=False,
                ),
            )
        )
    if len(lowered) > limits.max_edges:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.eval-conditional-writes.edge-limit", "shell taint edge limit exceeded"
            )
        )


def _eval_conditional_variable_writes(
    evidence: _ShellTaintEvidence,
    assignments_by_command: dict[int, tuple[_AssignmentEvidence, ...]],
    limits: TaintLimits,
) -> tuple[_FlowWrite, ...]:
    """Lower taken eval-time assignment branches into scope-visible flow writes."""
    command_environments, environment_parents, _lastpipe = _execution_environment_ids(evidence)
    scoped_variables = bool(evidence.scopes)
    target_environments = set(command_environments.values())

    lowered: list[_FlowWrite] = []
    lowered_keys: set[tuple[int, str, tuple[str | int | bool, ...]]] = set()
    for command in evidence.commands:
        origin_environment = command_environments[command.command_id]
        for assignment in assignments_by_command.get(command.command_id, ()):
            identity = (
                origin_environment,
                assignment.name,
                _expression_identity(assignment.content),
            )
            if identity in lowered_keys:
                continue
            lowered_keys.add(identity)
            _append_eval_conditional_writes(
                lowered,
                assignment,
                origin_environment,
                target_environments,
                environment_parents,
                scoped_variables=scoped_variables,
                limits=limits,
            )
    return tuple(lowered)


@dataclass(slots=True)
class _EvalDiscoveryBudget:
    """Shared work accounting across iterative eval side-effect discovery."""

    limits: TaintLimits
    work: int = 0
    updates: int = 0

    def charge_work(self, amount: int = 1) -> None:
        self.work += amount
        if self.work > self.limits.max_expression_nodes:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-discovery.work-limit", "shell taint expression node limit exceeded"
                )
            )

    def charge_update(self, amount: int = 1) -> None:
        self.updates += amount
        if self.updates > self.limits.max_fixed_point_updates:
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.eval-discovery.update-limit",
                    "shell taint fixed-point update limit exceeded",
                )
            )


@dataclass(frozen=True, slots=True)
class _EvalCommandEnvironment:
    """Possible values and definitely-set names visible to one command-time eval."""

    variables: tuple[tuple[str, _ContentValue], ...]
    definitely_set: frozenset[str]
    fixed_point_overrides: tuple[tuple[str, _ContentValue], ...] = ()


def _eval_command_environments(  # noqa: PLR0912, PLR0913, PLR0915
    evidence: _ShellTaintEvidence,
    raw_variables: dict[str | int, _ContentValue],
    assignments_by_command: dict[int, tuple[_AssignmentEvidence, ...]],
    command_environments: dict[int, int],
    environment_parents: dict[int, int | None],
    limits: TaintLimits,
) -> dict[int, _EvalCommandEnvironment]:
    """Replay definite authored writes in source order without importing future values."""
    values: dict[int, dict[str, _ContentValue]] = {}
    definitely_set: dict[int, set[str]] = {}
    evaluation_layers: dict[int, dict[str | int, _ContentValue]] = {}
    function_values: dict[int, dict[str, _ContentValue]] = {}
    function_set: dict[int, set[str]] = {}
    function_shadows: dict[int, set[str]] = {}
    function_entry_overrides: dict[int, set[str]] = {}
    function_unknown: set[int] = set()
    unknown_environments: set[int] = set()
    function_contexts = {
        command.function_context_id
        for command in evidence.commands
        if command.function_context_id is not None
    }
    scope_environments, _scope_parents = _scope_environment_ids(evidence.scopes)
    compound_assignments: dict[int, list[_AssignmentEvidence]] = {}
    source_order_override_keys: set[str] = set()
    for scope in evidence.scopes:
        if scope.binding_command_id is None:
            continue
        origin_environment = command_environments.get(scope.binding_command_id)
        if origin_environment is None:
            continue
        lexical_environment = scope_environments.get(scope.scope_id, scope.scope_id)
        for assignment in scope.loop_bindings:
            target_environment = _scoped_variable_environment(assignment.name)
            name = (
                assignment.name
                if target_environment in function_contexts
                else _scoped_variable_name(
                    origin_environment,
                    _unscoped_variable_name(assignment.name),
                )
            )
            compound_assignments.setdefault(scope.binding_command_id, []).append(
                replace(
                    assignment,
                    name=name,
                    content=_rescope_expression(
                        assignment.content,
                        lexical_environment,
                        origin_environment,
                    ),
                )
            )
            source_order_override_keys.add(name)

    eval_shadow_names: dict[int, frozenset[str]] = {}
    for command in evidence.commands:
        names: set[str] = set()
        for executable in _builtin_eval_candidates(command):
            reparsed = _eval_reparse_content(_eval_arguments_raw(command, executable), limits)
            pending = [reparsed]
            while pending:
                current_expression = pending.pop()
                if isinstance(current_expression, VariableRef):
                    names.add(current_expression.name)
                elif isinstance(current_expression, _SecondPassConditionalAssignment):
                    names.add(current_expression.name)
                    pending.append(current_expression.operand)
                elif isinstance(current_expression, Choice | Concat):
                    pending.extend(current_expression.parts)
        eval_shadow_names[command.command_id] = frozenset(names)

    def ensure(
        environment: int,
    ) -> tuple[
        dict[str, _ContentValue],
        set[str],
        dict[str | int, _ContentValue],
    ]:
        if environment in values:
            return (
                values[environment],
                definitely_set[environment],
                evaluation_layers[environment],
            )
        parent = environment_parents.get(environment)
        if parent is None:
            inherited_values: dict[str, _ContentValue] = {}
            inherited_set: set[str] = set()
        else:
            parent_values, parent_set, _parent_layer = ensure(parent)
            inherited_values = dict(parent_values)
            inherited_set = set(parent_set)
        values[environment] = inherited_values
        definitely_set[environment] = inherited_set
        layer: dict[str | int, _ContentValue] = {
            key: value
            for name, value in inherited_values.items()
            for key in (name, _scoped_variable_name(environment, name))
        }
        evaluation_layers[environment] = layer
        return inherited_values, inherited_set, layer

    def evaluate_assignment(
        assignment: _AssignmentEvidence,
        layer: dict[str | int, _ContentValue],
    ) -> _ContentValue:
        return _evaluate_with_tables(
            assignment.content,
            ChainMap(layer, raw_variables),
            {},
            {},
            limits,
        )

    def apply_assignment(  # noqa: PLR0913
        assignment: _AssignmentEvidence,
        environment: int,
        current: dict[str, _ContentValue],
        current_set: set[str],
        layer: dict[str | int, _ContentValue],
        *,
        definite: bool,
        evaluation_layer: dict[str | int, _ContentValue] | None = None,
    ) -> None:
        name = _unscoped_variable_name(assignment.name)
        value = evaluate_assignment(
            assignment,
            evaluation_layer if evaluation_layer is not None else layer,
        )
        prior = current.get(name)
        if assignment.append:
            value = _compose_values(prior or _OUTSIDE_VALUE, value)
        elif not definite:
            value = _join_values(prior or _OUTSIDE_VALUE, value)
        current[name] = _cap_value(value, limits)
        layer[name] = current[name]
        layer[_scoped_variable_name(environment, name)] = current[name]
        if definite:
            current_set.add(name)

    def apply_parameter_assignment(  # noqa: PLR0913
        assignment: _AssignmentEvidence,
        environment: int,
        current: dict[str, _ContentValue],
        current_set: set[str],
        layer: dict[str | int, _ContentValue],
        *,
        definite: bool,
        evaluation_layer: dict[str | int, _ContentValue] | None = None,
    ) -> None:
        """Apply one expansion side effect only when its parameter branch is reachable."""
        name = _unscoped_variable_name(assignment.name)
        prior = current.get(name)
        if (
            assignment.conditional
            and name in current_set
            and (not assignment.assign_if_null or (prior is not None and _EPSILON not in prior))
        ):
            return
        uncertain = (
            assignment.conditional
            and prior is not None
            and (
                name not in current_set
                or (assignment.assign_if_null and any(summary != _EPSILON for summary in prior))
            )
        )
        apply_assignment(
            assignment,
            environment,
            current,
            current_set,
            layer,
            definite=definite and not uncertain,
            evaluation_layer=evaluation_layer,
        )
        if definite:
            current_set.add(name)

    def apply_routed_side_effect(  # noqa: PLR0913
        assignment: _AssignmentEvidence,
        *,
        function_context: int | None,
        environment: int,
        current: dict[str, _ContentValue],
        current_set: set[str],
        layer: dict[str | int, _ContentValue],
        persistent_values: dict[str, _ContentValue],
        persistent_set: set[str],
        persistent_layer: dict[str | int, _ContentValue],
        definite: bool,
    ) -> None:
        target_environment = _scoped_variable_environment(assignment.name)
        name = _unscoped_variable_name(assignment.name)
        if target_environment in function_contexts:
            target_values = function_values.setdefault(target_environment, {})
            target_set = function_set.setdefault(target_environment, set())
            function_shadows.setdefault(target_environment, set()).add(name)
            apply_parameter_assignment(
                assignment,
                target_environment,
                target_values,
                target_set,
                layer,
                definite=definite,
                evaluation_layer=layer,
            )
            if target_environment == function_context:
                if name in target_values:
                    current[name] = target_values[name]
                    layer[name] = target_values[name]
                    layer[_scoped_variable_name(environment, name)] = target_values[name]
                    layer[assignment.name] = target_values[name]
                if name in target_set:
                    current_set.add(name)
            return
        apply_parameter_assignment(
            assignment,
            environment,
            persistent_values,
            persistent_set,
            persistent_layer,
            definite=definite,
            evaluation_layer=layer,
        )
        if current is not persistent_values:
            if name in persistent_values:
                current[name] = persistent_values[name]
                layer[name] = persistent_values[name]
                layer[_scoped_variable_name(environment, name)] = persistent_values[name]
            if name in persistent_set:
                current_set.add(name)

    snapshots: dict[int, _EvalCommandEnvironment] = {}
    for command in evidence.commands:
        environment = command_environments[command.command_id]
        persistent_values, persistent_set, persistent_layer = ensure(environment)
        status = command.execution_status
        conditional = status is not True or (
            command.function_context_id is not None
            and (command.function_effect_conditional or command.conditionally_executed)
        )
        assignment_only = not command.argv or command.executable.argv_index is None
        temporary_prefix = bool(command.definite_assignments and not assignment_only)
        if not conditional and not temporary_prefix:
            current = persistent_values
            current_set = persistent_set
            layer = persistent_layer
        else:
            current = dict(persistent_values)
            current_set = set(persistent_set)
            layer = dict(persistent_layer)
        function_context = command.function_context_id
        entry_overrides: set[str] = set()
        if function_context is not None:
            local_values = function_values.setdefault(function_context, {})
            local_set = function_set.setdefault(function_context, set())
            local_shadows = function_shadows.setdefault(function_context, set())
            entry_overrides = function_entry_overrides.setdefault(function_context, set())
            for name in local_shadows:
                current.pop(name, None)
                current_set.discard(name)
                layer.pop(name, None)
                layer.pop(_scoped_variable_name(environment, name), None)
            if function_context in function_unknown:
                current_set.clear()
            for name, value in local_values.items():
                current[name] = value
                layer[name] = value
                layer[_scoped_variable_name(environment, name)] = value
                layer[_scoped_variable_name(function_context, name)] = value
            current_set.update(local_set)
        for name in command.function_entry_unsets:
            if name in entry_overrides:
                continue
            current.pop(name, None)
            current_set.discard(name)
            layer.pop(name, None)
            layer.pop(_scoped_variable_name(environment, name), None)
        for assignment in command.function_entry_assignments:
            if _unscoped_variable_name(assignment.name) in entry_overrides:
                continue
            apply_assignment(
                assignment,
                environment,
                current,
                current_set,
                layer,
                definite=True,
            )
        current_set.update(
            name for name in command.function_entry_definitely_set if name not in entry_overrides
        )

        for assignment in compound_assignments.get(command.command_id, ()):
            apply_routed_side_effect(
                assignment,
                function_context=function_context,
                environment=environment,
                current=current,
                current_set=current_set,
                layer=layer,
                persistent_values=persistent_values,
                persistent_set=persistent_set,
                persistent_layer=persistent_layer,
                definite=not conditional,
            )
        for assignment in command.assignments:
            if assignment.conditional:
                apply_routed_side_effect(
                    assignment,
                    function_context=function_context,
                    environment=environment,
                    current=current,
                    current_set=current_set,
                    layer=layer,
                    persistent_values=persistent_values,
                    persistent_set=persistent_set,
                    persistent_layer=persistent_layer,
                    definite=not conditional,
                )
        for assignment in command.definite_assignments:
            apply_assignment(
                assignment,
                environment,
                current,
                current_set,
                layer,
                definite=True,
            )
        snapshot_values = dict(current)
        fixed_point_overrides: dict[str, _ContentValue] = {}
        function_entry_names = {
            *command.function_entry_unsets,
            *(
                _unscoped_variable_name(assignment.name)
                for assignment in command.function_entry_assignments
            ),
        }
        for key in eval_shadow_names[command.command_id]:
            name = _unscoped_variable_name(key)
            if key not in source_order_override_keys and name not in function_entry_names:
                continue
            if (
                name not in current
                and (environment in unknown_environments or function_context in function_unknown)
                and name not in function_entry_names
            ):
                continue
            target_environment = _scoped_variable_environment(key)
            if target_environment in function_contexts:
                fixed_point_overrides[key] = (
                    current.get(name, _OUTSIDE_VALUE)
                    if name in function_entry_names
                    else function_values.get(
                        target_environment,
                        {},
                    ).get(name, _OUTSIDE_VALUE)
                )
            else:
                fixed_point_overrides[key] = current.get(name, _OUTSIDE_VALUE)
        snapshots[command.command_id] = _EvalCommandEnvironment(
            tuple(sorted(snapshot_values.items())),
            frozenset(current_set),
            tuple(sorted(fixed_point_overrides.items())),
        )
        if status is False:
            continue
        if assignment_only:
            for assignment in command.definite_assignments:
                name = _unscoped_variable_name(assignment.name)
                local_target = function_context is not None and (
                    _scoped_variable_environment(assignment.name) == function_context
                    or name in function_shadows[function_context]
                )
                if local_target:
                    function_shadows[function_context].add(name)
                    apply_assignment(
                        assignment,
                        function_context,
                        function_values[function_context],
                        function_set[function_context],
                        layer,
                        definite=True,
                    )
                elif conditional:
                    apply_assignment(
                        assignment,
                        environment,
                        persistent_values,
                        persistent_set,
                        persistent_layer,
                        definite=False,
                    )
        unset_names, unknown_unset_target = _unset_action(command)
        _eval_assignments, eval_unsets = _static_eval_mutations(command, limits=limits)
        unset_names = (*unset_names, *eval_unsets)
        if unknown_unset_target:
            persistent_set.clear()
            if function_context is not None:
                function_unknown.add(function_context)
                function_set[function_context].clear()
        for name in unset_names:
            if function_context is not None and name in function_shadows[function_context]:
                function_values[function_context].pop(name, None)
                function_set[function_context].discard(name)
                continue
            persistent_set.discard(name)
            if conditional:
                continue
            persistent_values.pop(name, None)
            persistent_layer.pop(name, None)
            persistent_layer.pop(_scoped_variable_name(environment, name), None)
        if command.unknown_builtin_content is not None:
            persistent_set.clear()
            if function_context is not None:
                function_unknown.add(function_context)
                function_set[function_context].clear()
            else:
                unknown_environments.add(environment)
        if command.builtin_local and function_context is not None:
            for name in command.builtin_unsets:
                function_shadows[function_context].add(name)
                function_values[function_context].pop(name, None)
                function_set[function_context].discard(name)
        for assignment in command.builtin_assignments:
            name = _unscoped_variable_name(assignment.name)
            local_target = (
                function_context is not None
                and not command.builtin_force_global
                and (
                    command.builtin_local
                    or _scoped_variable_environment(assignment.name) == function_context
                    or name in function_shadows[function_context]
                )
            )
            if local_target:
                function_shadows[function_context].add(name)
                apply_assignment(
                    assignment,
                    function_context,
                    function_values[function_context],
                    function_set[function_context],
                    layer,
                    definite=True,
                )
            if not local_target or command.builtin_dynamic_options:
                apply_assignment(
                    assignment,
                    environment,
                    persistent_values,
                    persistent_set,
                    persistent_layer,
                    definite=not conditional and not command.builtin_dynamic_options,
                    evaluation_layer=layer,
                )
        for assignment in assignments_by_command.get(command.command_id, ()):
            apply_assignment(
                assignment,
                environment,
                persistent_values,
                persistent_set,
                persistent_layer,
                definite=False,
                evaluation_layer=layer,
            )
            source_order_override_keys.add(
                _scoped_variable_name(
                    environment,
                    _unscoped_variable_name(assignment.name),
                )
            )
        for assignment in command.function_effect_assignments:
            target_environment = _scoped_variable_environment(assignment.name)
            name = _unscoped_variable_name(assignment.name)
            if target_environment in function_contexts:
                function_shadows.setdefault(target_environment, set()).add(name)
                apply_parameter_assignment(
                    assignment,
                    target_environment,
                    function_values.setdefault(target_environment, {}),
                    function_set.setdefault(target_environment, set()),
                    layer,
                    definite=not conditional,
                    evaluation_layer=layer,
                )
                source_order_override_keys.add(assignment.name)
            else:
                apply_parameter_assignment(
                    assignment,
                    environment,
                    persistent_values,
                    persistent_set,
                    persistent_layer,
                    definite=not conditional,
                    evaluation_layer=layer,
                )
                source_order_override_keys.add(_scoped_variable_name(environment, name))
        # ``function_effect_unsets`` is a flat list that does not preserve its position among the
        # ordered effect assignments, and it is applied after all of them. An ``unset`` that the
        # body performs *before* assigning the same name would therefore erase the live value, so
        # a name the same effect set also assigns keeps its assignment.
        effect_assigned_names = {
            assignment.name for assignment in command.function_effect_assignments
        }
        for target in command.function_effect_unsets:
            if target in effect_assigned_names:
                continue
            target_environment = _scoped_variable_environment(target)
            name = _unscoped_variable_name(target)
            if target_environment in function_contexts:
                function_shadows.setdefault(target_environment, set()).add(name)
                function_values.setdefault(target_environment, {}).pop(name, None)
                function_set.setdefault(target_environment, set()).discard(name)
                source_order_override_keys.add(target)
            else:
                persistent_values.pop(name, None)
                persistent_set.discard(name)
                persistent_layer.pop(name, None)
                persistent_layer.pop(_scoped_variable_name(environment, name), None)
                source_order_override_keys.add(_scoped_variable_name(environment, name))
        if function_context is not None:
            entry_overrides.update(
                _unscoped_variable_name(assignment.name)
                for assignment in compound_assignments.get(command.command_id, ())
            )
            entry_overrides.update(
                _unscoped_variable_name(assignment.name)
                for assignment in command.assignments
                if assignment.conditional
            )
            if assignment_only:
                entry_overrides.update(
                    _unscoped_variable_name(assignment.name)
                    for assignment in command.definite_assignments
                )
            entry_overrides.update(command.builtin_unsets)
            entry_overrides.update(
                _unscoped_variable_name(assignment.name)
                for assignment in command.builtin_assignments
            )
            entry_overrides.update(unset_names)
            entry_overrides.update(
                _unscoped_variable_name(assignment.name)
                for assignment in assignments_by_command.get(command.command_id, ())
            )
            entry_overrides.update(
                _unscoped_variable_name(assignment.name)
                for assignment in command.function_effect_assignments
            )
            entry_overrides.update(
                _unscoped_variable_name(target) for target in command.function_effect_unsets
            )
            if unknown_unset_target or command.unknown_builtin_content is not None:
                entry_overrides.update(command.function_entry_unsets)
                entry_overrides.update(command.function_entry_definitely_set)
                entry_overrides.update(
                    _unscoped_variable_name(assignment.name)
                    for assignment in command.function_entry_assignments
                )
    return snapshots


def _eval_taken_assignments(
    commands: tuple[_CommandEvidence, ...],
    context: _EvalSyntaxContext,
    budget: _EvalDiscoveryBudget,
    command_environments: dict[int, _EvalCommandEnvironment],
) -> dict[int, tuple[_AssignmentEvidence, ...]]:
    """Collect branch-correlated assignments reached by the streaming eval parser."""
    assignments_by_command: dict[int, tuple[_AssignmentEvidence, ...]] = {}
    for command in commands:
        assignments: list[_AssignmentEvidence] = []
        seen: set[tuple[str, bool, bool, bool, bool, tuple[str | int | bool, ...]]] = set()
        for executable in _builtin_eval_candidates(command):
            raw = _eval_arguments_raw(command, executable)
            budget.charge_work(_expression_nodes(raw))
            environment = command_environments[command.command_id]
            states = _eval_syntax_expression(
                raw,
                None,
                context,
                environment_variables=environment.variables,
                fixed_point_overrides=environment.fixed_point_overrides,
                definitely_set_variables=environment.definitely_set,
            )
            budget.charge_work(len(states))
            for state in states:
                for assignment in state.conditional_assignments:
                    identity = _assignment_identity(assignment)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    assignments.append(assignment)
                    budget.charge_work()
        assignments_by_command[command.command_id] = tuple(assignments)
    return assignments_by_command


def _reachable_eval_variable_writes(
    commands: tuple[_CommandEvidence, ...],
    writes: tuple[_FlowWrite, ...],
    limits: TaintLimits,
    *,
    budget: _EvalDiscoveryBudget | None = None,
    dependency_cache: dict[tuple[str | int | bool, ...], frozenset[str]] | None = None,
) -> tuple[_FlowWrite, ...]:
    """Retain only variable definitions reachable from exact builtin eval argument content."""
    cache = dependency_cache if dependency_cache is not None else {}

    def dependencies(expression: ContentExpr) -> frozenset[str]:
        identity = _expression_identity(expression)
        cached = cache.get(identity)
        if cached is not None:
            return cached
        if budget is not None:
            budget.charge_work(_expression_nodes(expression))
        found = frozenset(_eval_content_dependencies(expression, limits))
        if budget is not None:
            budget.charge_work(len(found))
        cache[identity] = found
        return found

    names: set[str] = set()
    for command in commands:
        for executable in _builtin_eval_candidates(command):
            names.update(dependencies(_eval_arguments_raw(command, executable)))
    writes_by_key: dict[str, list[_FlowWrite]] = {}
    for write in writes:
        if isinstance(write.key, str):
            writes_by_key.setdefault(write.key, []).append(write)
    pending = list(names)
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if budget is not None:
            budget.charge_work()
        for write in writes_by_key.get(name, ()):
            found = dependencies(write.expression)
            pending.extend(found - names)
            names.update(found)
    return tuple(write for write in writes if isinstance(write.key, str) and write.key in names)


def _solve_eval_conditional_flow(
    evidence: _ShellTaintEvidence,
    definitions: _FlowDefinitions,
    commands: tuple[_CommandEvidence, ...],
    limits: TaintLimits,
) -> tuple[
    _FlowDefinitions,
    _SolvedFlow,
    tuple[_FlowWrite, ...],
    dict[tuple[str | int, str | None], _EvalSyntaxValue],
    dict[int, _EvalCommandEnvironment],
]:
    """Iteratively persist taken eval assignments and re-solve scoped variable flow."""
    solved = _solve_flow_definitions(definitions, limits=limits)
    budget = _EvalDiscoveryBudget(limits)
    dependency_cache: dict[tuple[str | int | bool, ...], frozenset[str]] = {}
    known_writes = {_flow_write_identity(write) for write in definitions.variable_writes}
    command_environments, environment_parents, _lastpipe = _execution_environment_ids(evidence)
    assignments_by_command: dict[int, tuple[_AssignmentEvidence, ...]] = {}
    while True:
        eval_writes = _reachable_eval_variable_writes(
            commands,
            definitions.variable_writes,
            limits,
            budget=budget,
            dependency_cache=dependency_cache,
        )
        syntax_variables = _solve_eval_syntax_variables(
            eval_writes,
            solved.variables,
            limits,
        )
        command_snapshots = _eval_command_environments(
            evidence,
            solved.variables,
            assignments_by_command,
            command_environments,
            environment_parents,
            limits,
        )
        discovered_assignments = _eval_taken_assignments(
            commands,
            _EvalSyntaxContext(
                syntax_variables,
                solved.variables,
                limits,
                _eval_syntax_programs(eval_writes),
            ),
            budget,
            command_snapshots,
        )
        candidates = _eval_conditional_variable_writes(
            evidence,
            discovered_assignments,
            limits,
        )
        new_writes = tuple(
            write for write in candidates if _flow_write_identity(write) not in known_writes
        )
        if not new_writes:
            final_snapshots = _eval_command_environments(
                evidence,
                solved.variables,
                discovered_assignments,
                command_environments,
                environment_parents,
                limits,
            )
            return definitions, solved, eval_writes, syntax_variables, final_snapshots
        budget.charge_update(len(new_writes))
        known_writes.update(_flow_write_identity(write) for write in new_writes)
        assignments_by_command = discovered_assignments
        definitions = replace(
            definitions,
            variable_writes=(*definitions.variable_writes, *new_writes),
        )
        solved = _solve_flow_definitions(definitions, limits=limits)


def _eval_sink_marker_capable(
    command: _CommandEvidence,
    context: _EvalSyntaxContext,
    environment: _EvalCommandEnvironment,
    *,
    limits: TaintLimits,
) -> bool:
    """Return whether any builtin eval candidate reparses an authored marker flow."""
    for executable in _builtin_eval_candidates(command):
        exact_programs = _static_eval_programs(command)
        raw_programs = (
            tuple(
                LiteralTransfer(_strip_active_shell_comments(program)) for program in exact_programs
            )
            if exact_programs
            else (_eval_arguments_raw(command, executable),)
        )
        for raw in raw_programs:
            if _marker_capable(
                _evaluate_with_tables(
                    raw,
                    ChainMap(dict(environment.fixed_point_overrides), context.raw_variables),
                    {},
                    {},
                    context.limits,
                )
            ):
                return True
            if any(
                summary.full.entries[_DFA_START][1]
                for state in _eval_syntax_expression(
                    raw,
                    None,
                    context,
                    environment_variables=environment.variables,
                    fixed_point_overrides=environment.fixed_point_overrides,
                    definitely_set_variables=environment.definitely_set,
                )
                for summary in _finalize_eval_syntax(
                    state,
                    context,
                )
            ):
                return True
        if _static_eval_prefix_sink_marker_capable(command, context, environment, limits=limits):
            return True
    return False


def _static_eval_prefix_sink_marker_capable(  # noqa: PLR0912
    command: _CommandEvidence,
    context: _EvalSyntaxContext,
    environment: _EvalCommandEnvironment,
    *,
    limits: TaintLimits,
) -> bool:
    """Check exact eval sinks under temporary leading assignment environments."""
    outer_variables: Mapping[str | int, _ContentValue] = ChainMap(
        dict(environment.fixed_point_overrides),
        context.raw_variables,
    )
    for parsed in _static_eval_commands(command, limits=limits):
        index = 0
        prefix_variables: dict[str | int, _ContentValue] = {}
        while index < len(parsed.words):
            assignment = _static_assignment_word(parsed.words[index])
            if assignment is None:
                break
            content = _static_eval_assignment_content(parsed.source_words[index], limits=limits)
            if content is None:
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.eval-prefix.unrepresentable-content",
                        "shell taint eval command prefix cannot be represented",
                    )
                )
            prefix_variables[assignment.name] = _evaluate_with_tables(
                content,
                ChainMap(prefix_variables, outer_variables),
                {},
                {},
                context.limits,
            )
            index += 1
        if not prefix_variables or index >= len(parsed.words):
            continue
        while index < len(parsed.words) and parsed.words[index] in {"builtin", "command"}:
            index += 1
            if index < len(parsed.words) and parsed.words[index] == "--":
                index += 1
        if index >= len(parsed.words):
            continue
        sink_text: str | None = None
        executable = parsed.words[index]
        if executable == "eval":
            sink_text = " ".join(parsed.words[index + 1 :])
        elif executable in {"source", "."}:
            if index + 1 < len(parsed.words):
                sink_text = parsed.words[index + 1]
        elif _normalized_shell_head(executable) in _SHELL_HEADS:
            argv = tuple(_ArgPort(word, LiteralTransfer(word)) for word in parsed.words)
            selection = _select_shell_source(argv, index)
            if selection.kind is _ShellSourceKind.COMMAND and selection.argv_index is not None:
                sink_text = parsed.words[selection.argv_index]
        if sink_text is None:
            continue
        sink = _lower_eval_assignment_operand(
            _eval_reparse_content(LiteralTransfer(sink_text), context.limits),
            0,
            scoped=False,
        )
        if _marker_capable(
            _evaluate_with_tables(
                sink,
                ChainMap(prefix_variables, outer_variables),
                {},
                {},
                context.limits,
            )
        ):
            return True
    return False


def _script_port_expression(port: _ArgPort, stdin: ContentExpr) -> ContentExpr:
    """Return a static script resource reference or an external gap."""
    if port.process_resource_id is not None:
        return OutsideGap()
    # An unquoted glob such as ``ta*.sh`` resolves to whatever file(s) match at run time, not to
    # its own literal text, so it is treated the same as a dynamic port: an unresolvable name
    # rather than an exact (and almost certainly nonexistent) resource key.
    key = normalize_static_resource(
        port.literal, dynamic=port.dynamic or port.active_argv_expansion
    )
    if key is None:
        return OutsideGap()
    if key in _STDIN_DEVICE_PATHS:
        return choice(stdin, ResourceRef(key))
    return ResourceRef(key)


def _shell_script_source_expression(
    port: _ArgPort,
    process_resources: dict[int, _ProcessResourceEvidence],
    stdin: ContentExpr,
) -> ContentExpr:
    """Return the source expression when one shell port becomes a script operand."""
    if port.process_resource_id is not None:
        return _process_resource_input(port.process_resource_id, process_resources)
    return _script_port_expression(port, stdin)


def _source_operand_index(command: _CommandEvidence, argv_index: int) -> int:
    """Return the argv index of a ``source``/``.`` candidate's operand, honoring ``--``.

    ``source -- ./task.sh`` is ordinary Bash: ``--`` ends option parsing for the builtin and
    ``./task.sh`` is the file named. Without skipping it, the word immediately after the
    executable is ``--`` itself, so the operand lookup misses the real target entirely and the
    sourced file's content never reaches either sink check below.

    Args:
        command: The command evidence being inspected.
        argv_index: The argv index of the resolved ``source``/``.`` executable.

    Returns:
        The argv index of the operand, past a leading literal ``--`` when present.
    """
    index = argv_index + 1
    if (
        index < len(command.argv)
        and not command.argv[index].dynamic
        and command.argv[index].literal == "--"
    ):
        index += 1
    return index


def _trap_action_index(command: _CommandEvidence, argv_index: int) -> int | None:
    """Return the argv index of a ``trap`` action, or None when the command registers none.

    ``help trap`` specifies the grammar as ``trap [-lp] [[ARG] SIGNAL_SPEC ...]``: ARG is read and
    executed when the shell receives the named signal, and an ``EXIT`` action runs when the shell
    exits. Deferring expansion to that moment is exactly what made the action invisible here --
    nothing dispatched a sink for the literal ``trap`` builtin, so ``A=doc-;
    trap '${A}lattice reconcile' EXIT`` certified while Bash 5.2 executed the marker on exit.

    Three spellings register nothing and are excluded rather than over-refused. ``-l`` and ``-p``
    make the builtin print instead of register. A literal ``-`` as ARG resets the trap to its
    default. An empty ARG ignores the signal, and that case needs no special handling because empty
    text composes no marker.

    A dynamic word in the option position is treated as the action rather than skipped, since this
    analysis cannot tell an option from an action it cannot read, and reading one word too early
    can only add a refusal.

    Args:
        command: The command evidence being inspected.
        argv_index: The argv index of the resolved ``trap`` executable.

    Returns:
        The argv index of the action word, or None when this command registers no action.
    """
    index = argv_index + 1
    while index < len(command.argv):
        port = command.argv[index]
        if port.dynamic:
            return index
        literal = port.literal
        if literal == "--":
            index += 1
            break
        if literal.startswith("-") and literal[1:] and set(literal[1:]) <= {"l", "p"}:
            # The builtin prints its traps rather than registering one, so nothing is deferred.
            return None
        break
    if index >= len(command.argv):
        return None
    port = command.argv[index]
    if not port.dynamic and port.literal == "-":
        # ``trap - EXIT`` restores the default disposition and registers no action.
        return None
    return index


def _unresolved_head_sinks(
    command: _CommandEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    """Return the sinks a command whose head resolved to no name still executes.

    The resolver names nothing when no argv word carries literal text, because there is no text
    for it to read: ``A=doc-; B=lattice; "$A$B"`` resolves to no executable at all. That is not
    the same as running nothing, and treating it as such made the command contribute no sink and
    certified a body Bash runs the marker in. It also made the issue #131 head-sink rule depend on
    argument shape rather than on the head: ``X=doc-; "${X}lattice"`` refuses because ``lattice``
    is literal text the resolver can name, and ``A=doc-; B=lattice; "$A$B" x`` refuses because the
    literal ``x`` makes the selection ambiguous and reinstates the head, while the fully composed
    spelling of the same invocation certified.

    An unresolved head is handled here exactly as an ambiguous one is: the head word is a sink
    because Bash executes the command name after expansion, and the head may itself be a shell, so
    the option grammar is read from argv index zero as well. AD-17 rejects reading an unresolved
    head as inert, and this is that rule applied on the sink side.

    A command with no argv word runs nothing at all -- a bare assignment such as ``A=doc-`` is the
    common case -- so it keeps contributing no sink.

    Args:
        command: The command whose head resolved to no name.
        stdin: The command's finalized standard input expression.
        process_resources: Process substitution evidence for operand resolution.

    Returns:
        The sink expressions this command can execute.
    """
    if not command.argv:
        return ()
    unresolved = tuple(port.content for port in command.argv[:1] if port.dynamic)
    return _shell_source_sinks(command, 0, stdin, process_resources, unresolved)


def _candidate_sink_expressions(  # noqa: PLR0911, PLR0912
    command: _CommandEvidence,
    executable: _ExecutableEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
) -> tuple[ContentExpr, ...]:
    """Return conservative sink expressions for one resolved executable candidate."""
    if executable.argv_index is None or executable.name is None:
        return _unresolved_head_sinks(command, stdin, process_resources)
    name = executable.name
    literal = executable.literal
    direct_sinks: tuple[ContentExpr, ...] = ()
    if literal is not None and "/" in literal:
        key = normalize_static_resource(literal, dynamic=False)
        if key is not None:
            direct_sinks = (ResourceRef(key),)
    if executable.argv_index < len(command.argv) and not _resolves_to_marker_name(name):
        head_port = command.argv[executable.argv_index]
        if head_port.dynamic:
            # Bash executes the command name after expansion, so a head composed across a
            # variable boundary is an execution sink. A fully literal head needs no such check
            # because AD-17's per-word marker rule already covers it, and adding one would
            # refuse every certified invocation. A head that already resolves to a marker name,
            # such as ``"$RUNNER_TEMP/venv/bin/doc-lattice"``, is a recognized invocation that
            # the resolver certifies, so it stays outside this check for the same reason.
            direct_sinks = (head_port.content, *direct_sinks)
    if name == "eval" and literal == "eval" and not executable.external_lookup:
        return (_eval_arguments_raw(command, executable),)
    if name == "trap" and literal == "trap" and not executable.external_lookup:
        # The action is shell source the shell runs later, so it is a sink for the same reason an
        # ``eval`` argument is. The deferral is what hid it, not any difference in what it runs.
        action_index = _trap_action_index(command, executable.argv_index)
        if action_index is None:
            return direct_sinks
        return (command.argv[action_index].content, *direct_sinks)
    if name in {"source", "."} and literal == name and not executable.external_lookup:
        operand_index = _source_operand_index(command, executable.argv_index)
        if operand_index >= len(command.argv):
            return ()
        operand = command.argv[operand_index]
        if operand.process_resource_id is not None:
            return (_process_resource_input(operand.process_resource_id, process_resources),)
        return (_script_port_expression(operand, stdin),)
    if _normalized_shell_head(executable.name) in _SHELL_HEADS:
        return _shell_source_sinks(
            command, executable.argv_index, stdin, process_resources, direct_sinks
        )
    if executable.ambiguous:
        # The head could not be resolved to an exact name, so it may be a shell. AD-17 rejects
        # treating an unresolved head as inert, and the same reasoning applies on the sink side:
        # select from the first argv position rather than removing the sink. The resolver's own
        # index points at a later word here, so the unresolved head word is added separately.
        unresolved = tuple(port.content for port in command.argv[:1] if port.dynamic)
        return _shell_source_sinks(
            command, 0, stdin, process_resources, (*unresolved, *direct_sinks)
        )
    # ``external_lookup`` records how this command's own HEAD was reached, which says nothing
    # about whether a later argv word is a shell. Suppressing the launcher search on it for every
    # external head dropped the payload sink for ``command timeout 5 bash -c "$A$B"`` and the
    # ``env``/``exec`` spellings of the same body, all of which Bash runs the marker in. Only an
    # external shadow of a wrapper builtin keeps that suppression, which is the case AD-18
    # actually reasons about.
    launcher_index = (
        None
        if executable.external_lookup and executable.name in _EXTERNAL_WRAPPER_SHADOW_NAMES
        else _nested_shell_index(command, executable.argv_index)
    )
    if launcher_index is not None:
        # An unrecognized head such as ``timeout``, ``nohup``, or ``xargs`` may exec a shell that
        # appears later in its own argv. Selecting from that shell keeps the payload a sink
        # without an allowlist of launcher names, which AD-17's founding principle rules out.
        return _shell_source_sinks(
            command, launcher_index, stdin, process_resources, direct_sinks, from_launcher=True
        )
    return direct_sinks


def _resolves_to_marker_name(name: str | None) -> bool:
    """Return whether a resolved executable name already carries the authored marker.

    Such a head is a recognized invocation the command-local resolver certifies or refuses under
    AD-17, so the taint pass does not treat its command-name word as a separate sink.

    Args:
        name: The resolved executable name, or None when the head did not resolve.

    Returns:
        Whether the name contains the marker.
    """
    if name is None:
        return False
    return _marker_capable(frozenset({_TransferSummary.literal(name)}))


def _nested_shell_index(command: _CommandEvidence, head_index: int) -> int | None:
    """Return the argv index of a shell this command may exec, beyond its own head.

    A dynamic word is skipped rather than ending the search. Abandoning the scan on the first
    such word dropped the payload sink entirely, so one variable-spelled launcher option was
    enough to certify a body Bash runs the marker in: ``D=5; timeout "$D" bash -c "$A$B"``
    certified while the literal ``timeout 5`` spelling refused. Continuing past the word keeps
    the later shell visible, which is the fail-closed direction, because selecting some sink is
    always at least as conservative as selecting none.

    Args:
        command: The command evidence being inspected.
        head_index: The argv index of the command's own resolved head.

    Returns:
        The index of the first literal shell word after the head, or None when there is none.
    """
    for index in range(head_index + 1, len(command.argv)):
        port = command.argv[index]
        if port.dynamic:
            continue
        if _normalized_shell_head(port.literal) in _SHELL_HEADS:
            return index
    return None


def _shell_source_sinks(  # noqa: PLR0911, PLR0913
    command: _CommandEvidence,
    head_index: int,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
    direct_sinks: tuple[ContentExpr, ...],
    *,
    from_launcher: bool = False,
) -> tuple[ContentExpr, ...]:
    """Return the sink expressions a shell invocation at one argv index can execute.

    Args:
        command: The command evidence being inspected.
        head_index: The argv index the shell option grammar is read from.
        stdin: The command's finalized standard input expression.
        process_resources: Process substitution evidence for operand resolution.
        direct_sinks: Sinks already established for this command.
        from_launcher: Whether this shell was reached through an unrecognized head.

    Returns:
        The shell payload sinks followed by the established direct sinks.
    """
    selection = _select_shell_source(command.argv, head_index)
    if selection.init_file_indices:
        # An ``--rcfile``/``--init-file`` value is read IN ADDITION to the selected source, so it
        # joins the direct sinks rather than replacing any branch's own result. Every branch below
        # splats ``direct_sinks``, so folding it in here reaches all five selection kinds.
        direct_sinks = (
            *(
                _shell_script_source_expression(command.argv[index], process_resources, stdin)
                for index in selection.init_file_indices
            ),
            *direct_sinks,
        )
    if selection.kind is _ShellSourceKind.NONE:
        # ``xargs bash -c`` names no operand of its own because the launcher supplies one from
        # its standard input, so a missing operand behind a launcher is a stdin payload rather
        # than the usage error it would be for a directly invoked shell.
        return (stdin, *direct_sinks) if from_launcher else direct_sinks
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
        return (_shell_script_source_expression(port, process_resources, stdin), *direct_sinks)
    candidates: list[ContentExpr] = []
    for index in selection.candidate_indices:
        port = command.argv[index]
        candidates.extend(
            (port.content, _shell_script_source_expression(port, process_resources, stdin))
        )
    if selection.include_stdin:
        candidates.append(stdin)
    return (choice(*candidates), *direct_sinks)


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


def _payload_second_parse_marker_capable(
    expression: ContentExpr,
    context: _EvalSyntaxContext,
    environment: _EvalCommandEnvironment,
) -> bool:
    """Return whether one interpreted payload's second parse can accept the marker.

    Args:
        expression: The payload word's content, read as shell source rather than as a value.
        context: The eval-syntax context holding the solved variable tables.
        environment: The per-command execution environment for this body.

    Returns:
        Whether the payload's second parse composes the marker.
    """
    return any(
        summary.full.entries[_DFA_START][1]
        for state in _eval_syntax_expression(
            expression,
            None,
            context,
            environment_variables=environment.variables,
            fixed_point_overrides=environment.fixed_point_overrides,
            definitely_set_variables=environment.definitely_set,
        )
        for summary in _finalize_eval_syntax(state, context)
    )


def _shell_payload_candidate_indices(
    command: _CommandEvidence, executable: _ExecutableEvidence
) -> tuple[int, ...]:
    """Return the argv indices one executable candidate may run as an interpreted ``-c`` payload.

    Only two of the five selection kinds name such a payload. ``COMMAND`` names it exactly.
    ``AMBIGUOUS`` means the argv shape is uncertain, so ``_select_shell_source`` deliberately
    retains every remaining word rather than committing to one, and any of them can still land in
    the payload position; this mirrors the choice ``_shell_source_sinks`` builds over the same set.
    ``SCRIPT`` is excluded because it names a file rather than an interpreted payload, so reading
    its text as shell source would be a category error, and the file's own content already reaches
    the sink path through ``_shell_script_source_expression``. ``STDIN`` and ``NONE`` name no argv
    word at all.

    Args:
        command: The command evidence being inspected.
        executable: One resolved executable candidate of that command.

    Returns:
        The argv indices to second-parse, empty when this candidate reaches no payload.
    """
    head_index = _shell_source_head_index(command, executable)
    if head_index is None:
        return ()
    selection = _select_shell_source(command.argv, head_index)
    if selection.kind is _ShellSourceKind.COMMAND:
        return () if selection.argv_index is None else (selection.argv_index,)
    if selection.kind is _ShellSourceKind.AMBIGUOUS:
        return selection.candidate_indices
    return ()


def _child_shell_payload_assignments(
    command: _CommandEvidence,
    payload_index: int,
    limits: TaintLimits,
) -> tuple[_AssignmentEvidence, ...]:
    """Return the assignments a literal shell ``-c`` payload performs on itself.

    The payload runs in a child shell, so its own commands execute before the words that follow
    them in the same payload expand. Reading the payload only as a value expanded against the
    parent's variables therefore misses every fragment the payload composes itself, and
    ``bash -c 'A=doc-; "$A"lattice reconcile'`` certified while the ``eval`` spelling of the same
    body already refused.

    The payload is handed to ``_static_eval_mutations`` as an exact eval program, so the child's
    assignments are recovered by the one extractor that already implements assignment-prefix
    words, the declaration builtins, namerefs, and per-branch reachability. A payload this
    analysis cannot resolve to exact text contributes nothing here; its content still reaches the
    ordinary sink path, which is what refuses the double-quoted spelling.

    Args:
        command: The command whose argv holds the payload.
        payload_index: The argv index of the interpreted payload word.
        limits: The bounds this scan runs under.

    Returns:
        The payload's own assignments, in payload order.
    """
    payload = _exact_content_literal(command.argv[payload_index].content, {}, limits)
    if payload is None:
        return ()
    mutations, _unsets = _static_eval_mutations(
        replace(command, resolved_eval_program=None, resolved_eval_programs=(payload,)),
        limits=limits,
    )
    return tuple(
        replace(
            mutation.assignment,
            content=(
                mutation.assignment.content
                if mutation.eval_content is None
                else mutation.eval_content
            ),
        )
        for mutation in mutations
    )


def _child_shell_positional_environment(
    operands: tuple[_ArgPort, ...],
    first_offset: int,
    context: _EvalSyntaxContext,
    environment: _EvalCommandEnvironment,
) -> _EvalCommandEnvironment:
    """Return the payload environment with the child's positional parameters bound.

    The operands a shell is given after its program become the child's positional parameters, so
    ``bash -c '$0$1 reconcile' doc- lattice`` composes the marker out of words that are plain argv
    in the parent. Each operand is evaluated in the parent environment, which is where those words
    actually expand, so a parent ``$1`` among them still reads the parent's own positional rather
    than the binding being installed here.

    ``first_offset`` differs by dispatch form and is not cosmetic. A ``-c`` payload's operands start
    at ``$0``, while a ``-s`` (stdin program) invocation leaves ``$0`` as the shell's own name and
    starts its operands at ``$1`` -- both verified under real Bash 5.2. Binding a ``-s`` operand
    list from ``$0`` would shift every parameter by one and miss the composition entirely.

    Args:
        operands: The argv ports the child receives as positional parameters, in order.
        first_offset: The positional number the first operand binds to.
        context: The eval-syntax context holding the solved variable tables.
        environment: The per-command execution environment for this body.

    Returns:
        The environment to second-parse this payload under, before its own assignments.
    """
    if not operands:
        return environment
    if len(operands) > context.limits.max_table_entries:
        raise _TaintLimitExceeded(
            GuardRefusal(
                "taint.child-shell.positional-operand-limit",
                "shell payload positional operands cannot be represented",
            )
        )
    inherited: Mapping[str | int, _ContentValue] = ChainMap(
        dict(environment.fixed_point_overrides),
        dict(environment.variables),
        context.raw_variables,
    )
    overrides: dict[str, _ContentValue] = dict(environment.fixed_point_overrides)
    overrides.update(
        (
            str(offset),
            _evaluate_with_tables(port.content, inherited, {}, {}, context.limits),
        )
        for offset, port in enumerate(operands, start=first_offset)
    )
    return replace(environment, fixed_point_overrides=tuple(overrides.items()))


def _child_shell_assignment_environment(
    command: _CommandEvidence,
    payload_index: int,
    context: _EvalSyntaxContext,
    environment: _EvalCommandEnvironment,
    *,
    limits: TaintLimits,
) -> _EvalCommandEnvironment | None:
    """Return the payload environment with the payload's own assignments applied.

    Assignments are installed as fixed-point overrides, the layer that already sits above the
    inherited environment. Each joins with whatever the name held on entry instead of replacing
    it, because this parse collapses the payload to a single position and a use that precedes its
    assignment still reads the inherited value. Joining keeps both readings live, which is the
    fail-closed direction and matches how a non-definite authored assignment is applied.

    Args:
        command: The command whose argv holds the payload.
        payload_index: The argv index of the interpreted payload word.
        context: The eval-syntax context holding the solved variable tables.
        environment: The payload environment with positional parameters already bound.

    Returns:
        The environment the payload's own assignments produce, or None when it assigns nothing.
    """
    assignments = _child_shell_payload_assignments(command, payload_index, limits)
    if not assignments:
        return None
    overrides: dict[str | int, _ContentValue] = dict(environment.fixed_point_overrides)
    inherited: dict[str | int, _ContentValue] = dict(environment.variables)
    # ``overrides`` is the first map deliberately: each assignment has to be visible to the ones
    # after it, the way the payload's own source order runs them.
    layered: ChainMap[str | int, _ContentValue] = ChainMap(
        overrides,
        inherited,
        context.raw_variables,
    )
    for assignment in assignments:
        name = _unscoped_variable_name(assignment.name)
        value = _evaluate_with_tables(assignment.content, layered, {}, {}, context.limits)
        prior = layered.get(name, _OUTSIDE_VALUE)
        overrides[name] = _cap_value(
            _compose_values(prior, value) if assignment.append else _join_values(prior, value),
            context.limits,
        )
    return replace(
        environment,
        # Every key is a variable name this function or the inherited overrides put there, so the
        # narrowing only restates what the table already holds.
        fixed_point_overrides=tuple(
            (name, value) for name, value in overrides.items() if isinstance(name, str)
        ),
    )


def _shell_stdin_positional_operands(
    command: _CommandEvidence, executable: _ExecutableEvidence
) -> tuple[_ArgPort, ...] | None:
    """Return the operands a stdin-program shell binds as positionals, or None if it selects none.

    ``bash -s -- doc- lattice`` reads its program from standard input and binds the trailing words
    as ``$1``, ``$2``, ... . Those words are plain argv in the parent, so the child composes the
    marker out of text this analysis can already read, exactly as the ``-c`` operand case did
    before it was bound. ``None`` distinguishes "this candidate does not read its program from
    stdin" from "it does, with no operands".

    Args:
        command: The command evidence being inspected.
        executable: One resolved executable candidate of that command.

    Returns:
        The operand ports, empty when the shell reads stdin but is given none, or None when this
        candidate does not select a stdin program at all.
    """
    head_index = _shell_source_head_index(command, executable)
    if head_index is None:
        return None
    selection = _select_shell_source(command.argv, head_index)
    if selection.kind is not _ShellSourceKind.STDIN:
        return None
    if selection.argv_index is None:
        return ()
    return command.argv[selection.argv_index :]


def _shell_command_payload_marker_capable(
    command: _CommandEvidence,
    stdin: ContentExpr,
    context: _EvalSyntaxContext,
    environment: _EvalCommandEnvironment,
    *,
    limits: TaintLimits,
) -> bool:
    """Return whether a shell ``-c`` payload composes the marker through its own expansion.

    AD-18 puts shell ``-c`` in the interpreted-payload set beside ``eval``, but only ``eval``
    reached the second-parse machinery. A literal payload such as ``bash -c '$A$B'`` is expanded by
    the child shell, so its parameter references have to resolve against this body's values the
    same way an ``eval`` payload's do. Resolving from the whole variable table rather than the
    exported subset over-approximates, which keeps the check fail-closed.

    The route this reaches a shell by is ``_shell_source_head_index``, the same entry point the
    glob guard uses, rather than the shell name at an executable candidate's own argv index. That
    narrower test saw only the direct head, so every other spelling ``_candidate_sink_expressions``
    dispatches through skipped the second parse and certified a body real Bash runs the marker in:
    a launcher such as ``timeout 60 bash -c '$A$B'``, an unresolved head such as ``$S -c '$A$B'``,
    and, through the ambiguous selection, a wrapped head or a dynamic shell option value
    (issue #154). Two spellings stay open and are tracked separately: a wrapper whose head and
    payload are both dynamic produces no executable evidence for any route to start from
    (issue #157), and the ambiguous route reads the option grammar across a wrapper's own options
    (issue #158).

    A shell that reads its program from standard input is second-parsed here too. It reaches the
    same machinery by a different route, because the program is a stream rather than an argv word:
    ``printf '%s\\n' '$1$2 reconcile' | bash -s -- doc- lattice`` and its heredoc spelling both
    compose the marker from operands that are plain argv in the parent, which is the same flow the
    ``-c`` operand binding closes, one dispatch form over. The stdin program is parsed whether or
    not operands accompany it, because an EXPORTED parent variable is visible to that child as
    well, so gating the parse on the presence of operands would certify the exported spelling.

    Args:
        command: The command whose executable candidates may be a shell.
        stdin: The command's finalized standard input expression, read as shell source when a
            candidate selects a stdin program.
        context: The eval-syntax context holding the solved variable tables.
        environment: The per-command execution environment for this body.

    Returns:
        Whether any shell ``-c`` or stdin payload's second parse can accept the marker.
    """
    scanned: set[int] = set()
    for executable in _iter_executable_evidence(command.executable):
        if (
            executable.name == "trap"
            and executable.literal == "trap"
            and not executable.external_lookup
            and executable.argv_index is not None
        ):
            # A trap action is ordinarily single-quoted so the shell defers its expansion to the
            # moment the signal arrives, which is precisely the shape the value route cannot see:
            # as a VALUE the word is the inert text ``${A}lattice``. Reading it as shell source is
            # the same second parse an ``eval`` payload gets.
            action_index = _trap_action_index(command, executable.argv_index)
            if action_index is not None and action_index not in scanned:
                scanned.add(action_index)
                action = command.argv[action_index].content
                if _payload_second_parse_marker_capable(action, context, environment):
                    return True
                # The action runs as a unit when the signal arrives, so a fragment it assigns to
                # itself is composed there rather than in this body's flow: ``trap 'A=doc-;
                # "$A"lattice reconcile' EXIT`` composes the marker entirely inside the action.
                # This is the same recovery the ``-c`` payload gets, and it may raise on state
                # this analysis cannot represent, so it runs only after the plain parse declined.
                assigned = _child_shell_assignment_environment(
                    command, action_index, context, environment, limits=limits
                )
                if assigned is not None and _payload_second_parse_marker_capable(
                    action, context, assigned
                ):
                    return True
        operands = _shell_stdin_positional_operands(command, executable)
        if operands is not None:
            positional = _child_shell_positional_environment(operands, 1, context, environment)
            if _payload_second_parse_marker_capable(stdin, context, positional):
                return True
        for index in _shell_payload_candidate_indices(command, executable):
            if index in scanned:
                # Routes converge on shared words, and a parse depends only on the word and this
                # command's own context, so a repeated index cannot reach a different answer.
                continue
            scanned.add(index)
            payload = command.argv[index].content
            # The positional binding cannot fail, so it runs first and keeps the taint reason on
            # every body the ordinary parse already catches. Recovering the payload's own
            # assignments can raise on state this analysis cannot represent, which is only
            # allowed to decide a body that would otherwise certify.
            positional = _child_shell_positional_environment(
                command.argv[index + 1 :], 0, context, environment
            )
            if _payload_second_parse_marker_capable(payload, context, positional):
                return True
            assigned = _child_shell_assignment_environment(
                command, index, context, positional, limits=limits
            )
            if assigned is not None and _payload_second_parse_marker_capable(
                payload, context, assigned
            ):
                return True
    return False


def _summary_marker_fragment_capable(summary: _TransferSummary) -> bool:
    """Return whether any DFA entry state finds marker-relevant progress in this summary.

    ``_marker_capable`` asks only "does this text, scanned fresh (entry state zero), complete the
    marker" -- exactly right for a value that is itself a full, final sink. This instead asks
    whether *any* of the 11 possible entry states -- standing in for "whatever partial match, if
    any, was already in progress when this text starts" -- either completes the marker or leaves
    the scan somewhere other than idle. A prefix fragment such as ``doc-`` shows this from entry
    state zero already (it advances the scan on its own). A suffix fragment such as ``lattice``
    shows nothing from entry state zero (nothing in it is special starting fresh) but completes
    the marker outright from ``_LATTICE_START_STATE``, the entry state standing for "already
    matched doc and a separator." Checking every entry state instead of only zero is required for
    the second case; skip length-zero content is the one thing this must never flag, since a
    zero-length text's every "exit" is trivially its own entry state (no character was ever
    processed to justify calling that "progress"), which would otherwise fire on any resource
    whose solved value includes a routine empty/unwritten alternative.

    Args:
        summary: One alternative from a resource's solved content value.

    Returns:
        Whether some entry state finds this text able to advance toward or complete the marker.
    """
    entries = summary.stripped.entries
    is_identity_transfer = all(
        exit_state == entry and not accepted for entry, (exit_state, accepted) in enumerate(entries)
    )
    if is_identity_transfer:
        # Every entry state maps back to itself with no acceptance: the identity transfer, which
        # only zero-length text produces (no character was processed to leave any entry state).
        return False
    return any(accepted or exit_state != _DFA_START for exit_state, accepted in entries)


def _resource_marker_fragment_capable(value: _ContentValue) -> bool:
    """Return whether a resource's own content could plausibly compose the marker.

    A sourced file's content gets read into variables this analysis cannot extract (issue #133),
    so this checks two things instead of relying on an exact-literal replay:

    1. The resource's raw content itself, across every DFA entry state
       (`_summary_marker_fragment_capable`) -- catches both a prefix fragment like ``doc-`` and,
       for content with no ``NAME=`` structure at all, a bare partial fragment like ``-lattice``.
    2. Each ``NAME=VALUE``-shaped line's *value* in isolation, using the same tokenizer eval's own
       payload replay uses (`_static_assignment_word`). This is required, not merely thorough: a
       suffix fragment written as ``Y=lattice`` never shows progress from any entry state as raw
       bytes, because the literal ``Y=`` resets the scan before ``lattice`` is ever reached --
       only the isolated value ``lattice`` reveals it can complete a marker a preceding ``doc-``
       started elsewhere. The non-source control that already refuses this same composition
       (`Y=lattice; eval "doc-$Y"`) sees this because its assignment parser extracts the value
       directly off the AST; a sourced file's bytes carry no such parse, so this reconstructs the
       same value the cheapest sound way available.

    Args:
        value: The resource's solved content value.

    Returns:
        Whether any alternative, or any assignment-shaped line's value within it, could plausibly
        advance toward or complete the marker.
    """
    return any(
        _summary_marker_fragment_capable(alternative) for alternative in value
    ) or _resource_assignment_marker_fragment_capable(value)


def _resource_assignment_marker_fragment_capable(value: _ContentValue) -> bool:
    """Return whether a resource ASSIGNS a marker fragment to one of its own variables.

    This is the second half of ``_resource_marker_fragment_capable`` on its own, for the callers
    that must not use the first half. The raw whole-content check asks whether the file's bytes
    could continue a partial match that began OUTSIDE the file, which is the right question for
    ``source``: the file's state merges into the current shell, so a fragment it leaves behind
    composes with authored text in the same body.

    It is the wrong question for a file a CHILD shell runs, because ordinary script text answers it
    yes. ``make build`` ends in ``d`` and so advances the scan from the idle entry state; ``echo
    hello`` does the same from another. Applying it to ``bash run.sh`` refused
    ``echo 'make build' > run.sh; bash run.sh``, which is a mandatory certification row and about
    as ordinary as a CI body gets. A child shell's variables never return to this body, so the
    composition that route has to detect is one the file performs WITHIN ITSELF, and the tractable
    evidence for that is the file's own assignments. Content that is already the whole marker still
    refuses, through the value the script operand contributes to the sink path.

    Args:
        value: The resource's solved content value.

    Returns:
        Whether any assignment-shaped line's value could advance toward or complete the marker.
    """
    for alternative in value:
        for text in alternative.literal_texts:
            for line in text.splitlines():
                assignment = _static_assignment_word(line)
                if assignment is None or not isinstance(assignment.content, LiteralTransfer):
                    continue
                if _summary_marker_fragment_capable(
                    _TransferSummary.literal(assignment.content.text)
                ):
                    return True
    return False


def _resource_key_patterns(literal: str) -> tuple[str, ...]:
    """Return the glob patterns one authored operand word can match a resource key with.

    Resource keys are lexically normalized before they enter the tables, so ``./task.sh`` is
    stored as ``task.sh``. An authored pattern is not normalized anywhere, which is why
    ``bash ./ta*.sh`` matched no key at all no matter which file Bash expanded it onto (issue
    #150). Running the authored literal through ``normalize_static_resource`` brings the pattern
    into the same space as the keys: the normalizer only splits on ``/`` and rejoins, so every
    glob metacharacter inside a path component survives untouched and the result is still a
    pattern rather than a resolved name.

    Matching the union of both spellings is monotone over matching the raw literal alone, so this
    can only add refusals to what the guards already found. The over-approximations it accepts all
    point the same fail-closed way: a lexical ``..`` in a pattern is collapsed without knowing what
    the intervening component expands to, a backslash-escaped ``\\*`` stays a ``fnmatch``
    metacharacter rather than the literal Bash would match, and ``*`` is allowed to cross ``/``
    where Bash would not. Each widens the match set, never narrows it.

    Args:
        literal: One argv word's authored literal text, carrying glob metacharacters.

    Returns:
        The raw pattern, followed by its key-space normalization when that differs.
    """
    normalized = normalize_static_resource(literal, dynamic=False)
    if normalized is None or normalized == literal:
        return (literal,)
    return (literal, normalized)


def _glob_ports_reach_tracked_marker(
    ports: Iterable[_ArgPort],
    variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether any glob-expanding argv word can match a marker-bearing tracked resource.

    This is the narrow match operation both glob guards share. A glob's exact match set is
    genuinely unknowable without reading the filesystem, so rather than guess which file(s) a
    pattern names it stays inside the resources this same script writes, matching each key with
    Bash's own case-sensitive glob semantics and failing closed only when a match's content could
    plausibly carry a marker fragment. A target no part of this script writes is opaque,
    pre-existing content already outside every other sink check's purview.

    A word carrying ``active_argv_expansion`` is matched by its own pattern. A ``dynamic`` word is
    matched against every tracked key instead, because the name it resolves to is not knowable
    here at all. The claim that the ordinary sink machinery already handles a ``dynamic`` operand
    did not hold for this route: ``_script_port_expression`` maps such a port to ``OutsideGap``,
    and the exact guard beside it drops the same port through its ``dynamic=`` argument, so
    ``F=t.sh; source "$F"`` and ``F=t.sh; bash "$F"`` fell between the two guards and certified
    bodies Bash runs the marker in. A brace-expanded word loses the expansion flag per resulting
    port and is matched by its resolved literal.

    The two callers supply deliberately different tables. The shell-head caller passes empty
    variable and stream tables because a shell operand's own resource content is the whole
    question there; the ``source`` caller passes the command's solved variable and stream tables,
    matching what ``_source_payload_state_unrepresentable`` already resolves the exact operand
    with.

    Args:
        ports: The argv words this route could resolve its target from.
        variables: The variable table used to resolve a matched resource's content.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The stream table used to resolve a matched resource's content.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether some inspected word's pattern matches a script-tracked resource whose content
        could plausibly carry the marker.
    """
    for port in ports:
        if not port.active_argv_expansion and not port.dynamic:
            continue
        patterns = ("*",) if port.dynamic else _resource_key_patterns(port.literal)
        for key in resources:
            if not isinstance(key, str):
                continue
            if not any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns):
                continue
            content = _evaluate_with_tables(ResourceRef(key), variables, resources, streams, limits)
            if _resource_marker_fragment_capable(content):
                return True
    return False


def _shell_source_head_index(  # noqa: PLR0911
    command: _CommandEvidence, executable: _ExecutableEvidence
) -> int | None:
    """Return the argv index a shell option grammar is read from for one executable candidate.

    ``_candidate_sink_expressions`` reaches ``_shell_source_sinks`` by three distinct routes, and
    the guards beside it used to recognize only the first of them, so the same exploit spelled
    through a launcher certified: for the glob guard in issue #150, and for the ``-c`` payload
    second pass in issue #154. This mirrors that dispatch route for route so both consumers see
    exactly the argv positions the sink machinery does. The pre-shell builtin returns are mirrored
    too, and are load-bearing rather than decorative: without them ``source env.sh bash ta*.sh``
    would find ``bash`` by the launcher search and refuse a command whose later words are only
    positional parameters for the sourced script.

    Args:
        command: The command evidence being inspected.
        executable: One resolved executable candidate of that command.

    Returns:
        The argv index to read the shell option grammar from, or None when this candidate does not
        reach a shell.
    """
    if executable.argv_index is None or executable.name is None:
        return None
    name = executable.name
    literal = executable.literal
    if name == "eval" and literal == "eval" and not executable.external_lookup:
        return None
    if name in {"source", "."} and literal == name and not executable.external_lookup:
        return None
    if _normalized_shell_head(name) in _SHELL_HEADS:
        return executable.argv_index
    if executable.ambiguous:
        return 0
    if executable.external_lookup and name in _EXTERNAL_WRAPPER_SHADOW_NAMES:
        return None
    return _nested_shell_index(command, executable.argv_index)


def _source_payload_state_unrepresentable(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a ``source``/``.`` candidate reads a resource whose effects this analysis
    cannot rule out composing the marker.

    AD-18 replays an ``eval`` payload's state effects because the exact text sits directly in the
    command's own arguments, so ``_static_eval_mutations`` can tokenize it. A ``source`` payload's
    state effects live in a FILE the argument only names, and this analysis has no exact-literal
    model of a resource's content the way it does variable assignments (issue #133), so it cannot
    reconstruct what a sourced file assigns to which variable. A target this same script never
    writes is already outside every other sink check's purview (it is opaque, pre-existing content
    this analysis was never going to be able to reason about), so this stays narrow to resources
    the script itself writes.

    Refusing on every script-written source target regardless of content is unsound the other
    way: it over-refuses the ordinary "write a config/env file, then source it" idiom (for
    example ``echo "REGION=us-east-1" > env.sh; source env.sh``) that carries the marker nowhere.
    This only fails closed when the resource's own content could plausibly carry a marker
    fragment (`_resource_marker_fragment_capable`), matching the same content-aware check that
    already makes a source target directly containing the marker refuse.

    Args:
        command: The command whose executable candidates may be ``source``/``.``.
        sink_variables: The per-command solved variable table, for resolving the resource.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving the resource.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether this command sources a script-tracked resource whose state effects go unmodeled
        and whose content could plausibly carry the marker.
    """
    for executable in _iter_executable_evidence(command.executable):
        if executable.argv_index is None or executable.name is None:
            continue
        if executable.name not in {"source", "."} or executable.literal != executable.name:
            continue
        if executable.external_lookup:
            continue
        operand_index = _source_operand_index(command, executable.argv_index)
        if operand_index >= len(command.argv):
            continue
        operand = command.argv[operand_index]
        if _port_reads_tracked_marker_content(operand, sink_variables, resources, streams, limits):
            return True
    return False


def _port_reads_tracked_marker_content(  # noqa: PLR0913
    port: _ArgPort,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
    *,
    assignments_only: bool = False,
) -> bool:
    """Return whether one operand names a script-tracked resource that could carry the marker.

    This is the rule shared by every "a file's state effects go unmodeled" guard: the operand has
    to name a resource THIS script writes (an unwritten target is opaque pre-existing content that
    was never in this analysis's purview), and that resource's own content has to be able to carry
    a marker fragment. A process substitution operand is excluded because it names no static key.

    Args:
        port: The operand word naming the file.
        sink_variables: The per-command solved variable table, for resolving the resource.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving the resource.
        limits: The bound on content evaluation this analysis stays within.
        assignments_only: Whether to ask only whether the file assigns a marker fragment to its own
            variables, which is the question for a file a CHILD shell runs. See
            ``_resource_assignment_marker_fragment_capable`` for why the whole-content check is
            unusable there.

    Returns:
        Whether this operand reads script-tracked content that could plausibly carry the marker.
    """
    if port.process_resource_id is not None:
        return False
    key = normalize_static_resource(
        port.literal, dynamic=port.dynamic or port.active_argv_expansion
    )
    if key is None or key not in resources:
        return False
    content = _evaluate_with_tables(ResourceRef(key), sink_variables, resources, streams, limits)
    if assignments_only:
        return _resource_assignment_marker_fragment_capable(content)
    return _resource_marker_fragment_capable(content)


def _port_tracked_resource_keys(
    port: _ArgPort, resources: Mapping[str | int, _ContentValue]
) -> tuple[str, ...]:
    """Return every script-tracked resource key one operand word could name.

    This is the resolution half of ``_port_reads_tracked_marker_content`` and
    ``_glob_ports_reach_tracked_marker`` put in one place, so a guard that has to ask a question
    ABOUT the named file, rather than only about its content, reaches every spelling both of those
    reach between them. The content half arrived at that union across two guards, one exact and one
    glob; the positional half was written into the exact one alone and so recognized only the
    directly spelled operand (issue #175).

    The three spellings resolve exactly as the content half resolves them. An exactly spelled word
    normalizes to a single key. A word carrying ``active_argv_expansion`` matches by its own
    pattern, in both authored and key space (``_resource_key_patterns``). A ``dynamic`` word matches
    every tracked key with ``*``, because the name it resolves to is not knowable here at all. A
    process substitution operand names no static key and so resolves to nothing.

    Args:
        port: The operand word naming the file.
        resources: The solved resource table; a key present here is one this script writes.

    Returns:
        Every tracked resource key this word could name, empty when it names none.
    """
    if port.process_resource_id is not None:
        return ()
    if port.dynamic or port.active_argv_expansion:
        patterns = ("*",) if port.dynamic else _resource_key_patterns(port.literal)
        return tuple(
            key
            for key in resources
            if isinstance(key, str)
            and any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns)
        )
    key = normalize_static_resource(port.literal, dynamic=False)
    if key is None or key not in resources:
        return ()
    return (key,)


def _source_glob_operand_state_unrepresentable(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a ``source``/``.`` glob operand could read a script-tracked marker.

    ``source ta*.sh`` is ordinary Bash: the builtin's operand is expanded before the file is
    named, so the pattern resolves at run time to whatever matches in the current directory --
    state this deterministic analysis cannot read. ``_source_payload_state_unrepresentable``
    carries issue #133's framing that a sourced file's state effects live in a FILE the argument
    only names, but it asks ``normalize_static_resource`` for an exact key with ``dynamic=`` set
    for exactly this case, so a glob operand resolved to None and the exact guard skipped it
    (issue #150). This closes that gap the same narrow way, over the pattern rather than an exact
    name.

    The exact guard drops a glob operand through its ``dynamic=... or active_argv_expansion``
    argument, and this one covers it. The two overlap on a ``dynamic`` operand, which the shared
    helper now matches against every tracked key: neither guard read that spelling before, so it
    fell between them.

    Only the operand is supplied. Every word after it becomes a positional parameter for the
    sourced script rather than a second source target (verified under real Bash 5.2), so scanning
    later words would refuse bodies Bash never runs the marker in. ``_source_operand_index``'s
    literal ``--`` check needs no glob handling of its own for the same reason it needs none
    today: a word carrying argv expansion can never spell the exact two characters ``--``.

    Args:
        command: The command whose executable candidates may be ``source``/``.``.
        sink_variables: The per-command solved variable table, for resolving a matched resource.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving a matched resource.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether this command sources a glob operand whose pattern matches a script-tracked
        resource whose content could plausibly carry the marker.
    """
    for executable in _iter_executable_evidence(command.executable):
        if executable.argv_index is None or executable.name is None:
            continue
        if executable.name not in {"source", "."} or executable.literal != executable.name:
            continue
        if executable.external_lookup:
            continue
        operand_index = _source_operand_index(command, executable.argv_index)
        if operand_index >= len(command.argv):
            continue
        operand = command.argv[operand_index]
        if operand.process_resource_id is not None:
            continue
        if _glob_ports_reach_tracked_marker((operand,), sink_variables, resources, streams, limits):
            return True
    return False


def _glob_script_operand_state_unrepresentable(
    command: _CommandEvidence,
    resources: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a shell's glob script operand could reach a script-tracked marker.

    An unquoted glob such as ``ta*.sh`` in ``bash ta*.sh`` resolves at run time to whatever file(s)
    match the pattern in the current directory -- state this deterministic analysis cannot read.
    ``_select_shell_source`` already widens this to an ``AMBIGUOUS`` selection so the operand is
    never silently dropped as the sink, but the general widening alone resolves to ``OutsideGap()``
    (issue #133's opaque, externally-unknown placeholder), which is not itself proof of marker
    composition and would certify clean even when this exact evidence graph tracks the answer.

    This is the same shape as ``_source_payload_state_unrepresentable`` for a sourced file: rather
    than guess which file(s) the pattern matches, it stays narrow to resources this same script
    itself writes. A target no part of this script writes is already outside every other sink
    check's purview for the same reason a non-tracked ``source`` target is.

    Three things widen this past its first form, each closing a confirmed false-safe (issue #150):

    1. ``_shell_source_head_index`` mirrors all three routes ``_candidate_sink_expressions`` uses
       to reach the shell option grammar, not just a shell that is the command's own head, so the
       launcher spelling ``timeout 5 bash ta*.sh`` is seen too. The ambiguous route is mirrored for
       fail-closure rather than reachability: no run body constructs it today, because a command
       whose head is unresolved and whose operand carries a glob refuses earlier at the
       executable-word check. That earlier check is a separate guard whose scope could narrow, so
       the mirror stays.
    2. Every candidate word of the ambiguous selection is scanned, not only the first. Once the
       expansion count of one word is unknown, any later word can shift into the operand position
       (``bash -o p* ta*.sh`` expands ``p*`` onto a tracked ``pipefail`` file, making the glob that
       follows the real script operand). This is the same reason ``_shell_source_sinks`` builds a
       choice over every candidate rather than committing to one.
    3. Patterns are matched in resource-key space as well as as authored
       (``_resource_key_patterns``), so a ``./`` prefixed pattern can match the key it names.

    Args:
        command: The command whose executable candidates may be a shell.
        resources: The solved resource table; a key present here is one this script writes.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether some argv position selects a glob operand whose pattern matches a script-tracked
        resource whose content could plausibly carry the marker.
    """
    for executable in _iter_executable_evidence(command.executable):
        head_index = _shell_source_head_index(command, executable)
        if head_index is None:
            continue
        selection = _select_shell_source(command.argv, head_index)
        if selection.kind is not _ShellSourceKind.AMBIGUOUS or not selection.candidate_indices:
            continue
        if _glob_ports_reach_tracked_marker(
            (command.argv[index] for index in selection.candidate_indices),
            {},
            resources,
            {},
            limits,
        ):
            return True
    return False


def _shell_script_operand_state_unrepresentable(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a shell reads a script-tracked file whose state effects go unmodeled.

    ``_source_payload_state_unrepresentable`` makes this exact argument for ``source``/``.``, and
    ``_glob_script_operand_state_unrepresentable`` makes it for a shell whose script operand is a
    glob. The EXACT script operand of a shell was the one spelling of the same idea with no guard,
    so ``printf '%s\\n' 'A=doc-' '"${A}lattice" reconcile' > env.sh; bash env.sh`` certified while
    the ``source env.sh`` and ``bash e*.sh`` spellings of the identical file both refused, and real
    Bash 5.2 executed the marker. The file's content does reach the sink as a VALUE through
    ``_shell_script_source_expression``, which is why content that is already the whole marker
    refuses; what has no model is the file's own state effects, so content that merely composes the
    marker once the child shell runs it was dropped.

    Three port sets reach this, matching the ones that actually name a file the shell reads:

    1. The ``SCRIPT`` selection's operand, the direct ``bash env.sh`` spelling.
    2. Every ``AMBIGUOUS`` candidate, for the same reason ``_shell_source_sinks`` builds a choice
       over all of them rather than committing to one: once the argv shape is uncertain any of
       those words can land in the operand position.
    3. Every ``--rcfile``/``--init-file`` value, which Bash reads BEFORE the selected source. The
       option grammar previously skipped that word as an inert option argument, so
       ``bash --rcfile env.sh -ic :`` reached no sink for it at all (issue from the Codex review
       round). The value is checked whatever the final selection kind is, because the shell reads
       it in addition to, not instead of, that source.

    The interactive gate Bash applies to an rcfile is deliberately not modeled. Bash reads the file
    only when the shell is interactive, so a non-interactive ``bash --rcfile env.sh -c :`` runs
    nothing from it; recognizing that would mean tracking ``-i`` through the same option grammar an
    attacker controls, and AD-17's founding principle rules out narrowing a refusal on evidence the
    body itself supplies. The content gate keeps the cost of over-approximating here to a file this
    script writes whose content could carry the marker.

    The content test is ``assignments_only``, unlike the ``source`` guard's. A child shell's
    variables never return to this body, so the composition to detect is one the file performs
    within itself; asking the broader cross-boundary question instead refused
    ``echo 'make build' > run.sh; bash run.sh``. One class stays open as a result and is recorded
    rather than silently dropped: a file that supplies part of the marker literally and the rest
    from an EXPORTED parent variable composes across the boundary in the one direction a child does
    inherit, and neither this guard nor the value route second-parses a script operand's content to
    see it.

    Args:
        command: The command whose executable candidates may be a shell.
        sink_variables: The per-command solved variable table, for resolving the resource.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving the resource.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether some shell source names a script-tracked resource whose state effects go unmodeled
        and whose content could plausibly carry the marker.
    """
    for executable in _iter_executable_evidence(command.executable):
        head_index = _shell_source_head_index(command, executable)
        if head_index is None:
            continue
        selection = _select_shell_source(command.argv, head_index)
        indices: tuple[int, ...] = selection.init_file_indices
        if selection.kind is _ShellSourceKind.SCRIPT and selection.argv_index is not None:
            indices = (*indices, selection.argv_index)
        elif selection.kind is _ShellSourceKind.AMBIGUOUS:
            indices = (*indices, *selection.candidate_indices)
        for index in indices:
            if index >= len(command.argv):
                continue
            if _port_reads_tracked_marker_content(
                command.argv[index],
                sink_variables,
                resources,
                streams,
                limits,
                assignments_only=True,
            ):
                return True
    return False


def _text_references_positional_parameters(text: str, *, include_zero: bool = False) -> bool:
    """Return whether shell source text reads a positional parameter its caller supplies.

    Only the forms that can carry a caller's word into the child's own expansion count:
    ``$1``..``$9``, ``$@``, ``$*``, and the braced ``${1}``/``${@}``/``${*}`` spellings, including
    ``${1:-default}`` and the rest of the brace grammar, which all start with the same two
    characters. ``$#`` is excluded because a count cannot carry marker text. A ``$`` that a
    backslash escapes still counts, since this reads raw text without knowing the quoting context
    each one lands in, and that direction over-approximates.

    ``include_zero`` splits the two dispatch forms, which disagree about ``$0``. A SCRIPT operand's
    ``$0`` is the script's own path rather than a word the caller chose, so it must not trigger. A
    ``-c`` payload's ``$0`` is the first operand the caller supplies, which is exactly the position
    ``xargs -n1`` binds its input word to, so for that form it must.

    Args:
        text: One literal alternative of the shell source being read.
        include_zero: Whether ``$0`` counts as a caller-supplied positional.

    Returns:
        Whether the text reads a caller-supplied positional parameter anywhere.
    """
    index = 0
    while True:
        index = text.find("$", index)
        if index == -1 or index + 1 >= len(text):
            return False
        following = text[index + 1]
        if following == "{":
            if index + 2 < len(text) and _positional_reference_start(
                text[index + 2], include_zero=include_zero
            ):
                return True
        elif _positional_reference_start(following, include_zero=include_zero):
            return True
        index += 1


def _positional_reference_start(character: str, *, include_zero: bool = False) -> bool:
    """Return whether one character after a ``$`` opens a caller-supplied positional reference."""
    if character in {"@", "*"}:
        return True
    return character.isdigit() and (include_zero or character != "0")


def _resource_references_positional_parameters(
    value: _ContentValue, *, include_zero: bool = False
) -> bool:
    """Return whether a script-tracked file's own content reads a caller-supplied positional.

    Content this analysis cannot read as text answers yes. A resource whose solved value is opaque
    or truncated could reference anything, and the caller pairs this with a marker-capable argument
    before refusing, so the unreadable case fails closed rather than certifying on absent evidence.

    Args:
        value: The resource's solved content value.
        include_zero: Whether ``$0`` counts as a caller-supplied positional. See
            ``_text_references_positional_parameters`` for why the dispatch forms differ.

    Returns:
        Whether any alternative reads a positional parameter, or is unreadable as text.
    """
    for alternative in value:
        if alternative.projection_opaque or alternative.projection_incomplete:
            return True
        if any(
            _text_references_positional_parameters(text, include_zero=include_zero)
            for text in alternative.literal_texts
        ):
            return True
    return False


def _substitute_positional_references(
    text: str, replacement: ContentExpr, *, include_zero: bool = False
) -> ContentExpr:
    """Return the file's text as an expression with every positional replaced by one choice.

    Substituting a CHOICE over the caller's arguments, rather than one string, is what makes this
    sound without solving which argument lands at which position. The true binding of any one
    reference is a member of that choice, so composing the choices in the order the file spells
    them over-approximates every assignment of arguments to references at once, and the marker DFA
    that evaluates the result does the work. Enumerating the assignments instead would be
    exponential in the reference count; this stays linear because a ``Choice`` is one node and
    ``_merge_content_summaries`` collapses alternatives that share a DFA transfer.

    Substituting one joined string was unsound for a file selecting a NON-ADJACENT subset of its
    arguments: ``printf '%s\\n' '"$1$3" reconcile' > s.sh; bash s.sh doc- SAFE lattice`` runs the
    marker under real Bash 5.2 while the joined ``doc-SAFElattice`` composes nothing (issue #176).

    Args:
        text: One literal alternative of the file's own content.
        replacement: The expression every positional reference is replaced by, normally the choice
            ``_PositionalBinding.expression`` builds.
        include_zero: Whether ``$0`` counts as a caller-supplied positional. See
            ``_text_references_positional_parameters`` for why the two dispatch forms differ.

    Returns:
        The substituted expression, in the file's own order.
    """
    pieces: list[ContentExpr] = []
    index = 0
    start = 0
    while True:
        index = text.find("$", index)
        if index == -1 or index + 1 >= len(text):
            pieces.append(LiteralTransfer(text[start:]))
            return concat(*pieces)
        following = text[index + 1]
        end = -1
        if following == "{":
            closing = text.find("}", index + 2)
            if closing != -1 and _positional_reference_start(
                text[index + 2], include_zero=include_zero
            ):
                end = closing + 1
        elif _positional_reference_start(following, include_zero=include_zero):
            end = index + 2
            while end < len(text) and following.isdigit() and text[end].isdigit():
                end += 1
        if end == -1:
            index += 1
            continue
        pieces.append(LiteralTransfer(text[start:index]))
        pieces.append(replacement)
        start = end
        index = end


@dataclass(frozen=True, slots=True)
class _PositionalBinding:
    """Every text one caller-supplied positional reference could bind to.

    ``parts`` holds the individually selectable texts: one per argument word a caller spells, or
    one per word a launcher's input splits into. ``joined`` holds every one of them concatenated
    in order, which is what the whole analysis used to substitute on its own.

    Keeping ``joined`` as an alternative beside the parts is load bearing rather than tidy. It
    makes the choice model MONOTONE with the joined-string model it replaces, so every text the
    old one matched is still matched and no refusal this family already makes can be removed by
    the change. It is also the alternative that covers word splitting, where one argument the
    caller spells splits into several words the child concatenates back, and the multi-alternative
    argument, whose own alternatives this cannot tell apart at the seam.
    """

    parts: tuple[str, ...]
    joined: str

    @property
    def texts(self) -> tuple[str, ...]:
        """Return every text a single positional reference could bind to, without duplicates."""
        return tuple(dict.fromkeys((*self.parts, self.joined)))

    @property
    def expression(self) -> ContentExpr:
        """Return the choice a positional reference is replaced by."""
        return choice(*(LiteralTransfer(text) for text in self.texts))

    @property
    def fragment_capable(self) -> bool:
        """Return whether any bindable text could carry marker-relevant progress.

        This is the fallback the routes take when the text a reference sits in cannot be read, so
        the reference's placement is unknown and nothing narrower than the bound text is
        available. Asking it of every alternative rather than only the joined text is the same
        widening ``expression`` performs, one lattice level up.
        """
        return any(
            _summary_marker_fragment_capable(_TransferSummary.literal(text)) for text in self.texts
        )


def _argument_positional_binding(
    arguments: Iterable[_ArgPort],
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> _PositionalBinding:
    """Return what a positional reference can bind to, given the argv words a shell is handed.

    Each argument contributes its own literal text as a separately selectable alternative, since a
    file is free to read any subset of its arguments in any order. Every alternative that one
    argument's content solves to contributes too, because a reference binds one of them rather
    than their concatenation. Argv order still drives ``joined``: it is the order that composes
    the marker when the child reads the arguments in the order they were given.

    Args:
        arguments: The argv words after the script operand, in order.
        sink_variables: The per-command solved variable table.
        resources: The solved resource table.
        streams: The solved stream table.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        The binding those arguments supply.
    """
    parts: list[str] = []
    pieces: list[str] = []
    for argument in arguments:
        value = _evaluate_with_tables(argument.content, sink_variables, resources, streams, limits)
        alternatives = sorted(text for alternative in value for text in alternative.literal_texts)
        parts.extend(alternatives)
        parts.append("".join(alternatives))
        pieces.extend(alternatives)
    return _PositionalBinding(parts=tuple(parts), joined="".join(pieces))


@dataclass(frozen=True, slots=True)
class _PositionalRead:
    """One tracked file a shell reads, paired with the positionals that shell binds for it.

    ``keys`` is every script-tracked resource the naming word could resolve to, ``arguments`` the
    argv words the reading shell binds as positional parameters, and ``include_zero`` whether
    ``$0`` is one of those caller-supplied words for that dispatch form.
    """

    keys: tuple[str, ...]
    arguments: tuple[_ArgPort, ...]
    include_zero: bool


def _shell_child_argument_bindings(
    command: _CommandEvidence, selection: _ShellSourceSelection
) -> tuple[tuple[tuple[_ArgPort, ...], bool], ...]:
    """Return the positional bindings a selected shell holds while it reads its startup files.

    A ``--rcfile``/``--init-file`` value and a ``BASH_ENV`` target are read BEFORE the program the
    option grammar selected, but the child's positional parameters are already bound by then, so a
    startup file's ``$1`` is the selected program's first argument rather than anything beside the
    option. Verified under real Bash 5.2 with a shim on ``PATH``: a ``BASH_ENV`` file spelling
    ``"$0" reconcile`` runs the marker for ``bash -c : doc-lattice`` and does not for
    ``bash -s doc-lattice``.

    That is the whole difference between the forms, and it is the ``include_zero`` flag:

    - ``COMMAND``: ``bash -c PROGRAM w0 w1 ...`` binds ``$0`` to the first word after the payload,
      so every word after it is caller supplied and ``$0`` counts.
    - ``SCRIPT``: the child's ``$0`` is the script's own path, so only the words after the operand
      are caller supplied.
    - ``STDIN``: ``bash -s w1 w2`` starts its operands at ``$1``, with ``$0`` left as the shell's
      own name, so the selection's first operand index is already ``$1``.
    - ``AMBIGUOUS``: the argv shape is uncertain, so each candidate is read as both the
      operands-start-here form and the program-here form, which is the same widening
      ``_shell_source_sinks`` performs by building a choice over the whole candidate set. Adding
      words to a binding is not monotone (a wider join can break a composition that spanned the
      seam between the file's text and the argument), so the forms are enumerated rather than
      merged into one maximal list.

    Args:
        command: The command whose argv the selection indexes.
        selection: The shell source selection made from that argv.

    Returns:
        Each ``(arguments, include_zero)`` binding the child could hold, empty when it binds none.
    """
    argv = command.argv
    index = selection.argv_index
    if selection.kind is _ShellSourceKind.COMMAND and index is not None:
        return ((argv[index + 1 :], True),)
    if selection.kind is _ShellSourceKind.SCRIPT and index is not None:
        return ((argv[index + 1 :], False),)
    if selection.kind is _ShellSourceKind.STDIN and index is not None:
        return ((argv[index:], False),)
    if selection.kind is _ShellSourceKind.AMBIGUOUS:
        return tuple(
            binding
            for candidate in selection.candidate_indices
            if candidate < len(argv)
            for binding in ((argv[candidate:], False), (argv[candidate + 1 :], True))
        )
    return ()


def _shell_positional_reads(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> Iterator[_PositionalRead]:
    """Yield every script-tracked file this command's shells read, with that shell's arguments.

    This is the single place the seven shell-source routes of the state-unrepresentable family are
    enumerated for the positional question, mirroring the way ``_port_reads_tracked_marker_content``
    is the single place they all ask the content question. The positional test was originally
    written into the exact script operand's own guard, so a tracked file that composes the marker
    out of caller-supplied arguments certified on every other spelling of the same read (issue
    #175). Adding a route means adding it here rather than growing an eighth guard.

    Each route supplies exactly two things, and nothing else about it varies:

    1. ``source``/``.``: the operand names the file, in all three of its exact, glob and dynamic
       spellings, and the words after the operand are the sourced script's positionals. The child
       is the CURRENT shell, whose ``$0`` the builtin leaves untouched, so ``$0`` is excluded.
    2. A shell's ``SCRIPT`` operand, and every ``AMBIGUOUS`` candidate, with the words after that
       operand as its arguments and ``$0`` excluded as the script's own path.
    3. Every ``--rcfile``/``--init-file`` value and every tracked ``BASH_ENV`` target, whose
       arguments are the selected program's, per ``_shell_child_argument_bindings``.

    Args:
        command: The command whose executable candidates may read a shell source.
        sink_variables: The per-command solved variable table, for resolving a ``BASH_ENV`` value.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving a ``BASH_ENV`` value.
        limits: The bound on content evaluation this analysis stays within.

    Yields:
        One record per file-and-arguments pairing a shell in this command could read.
    """
    argv = command.argv
    environment_keys: tuple[str, ...] | None = None
    for executable in _iter_executable_evidence(command.executable):
        if (
            executable.argv_index is not None
            and executable.name in {"source", "."}
            and executable.literal == executable.name
            and not executable.external_lookup
        ):
            operand_index = _source_operand_index(command, executable.argv_index)
            if operand_index < len(argv):
                yield _PositionalRead(
                    _port_tracked_resource_keys(argv[operand_index], resources),
                    argv[operand_index + 1 :],
                    include_zero=False,
                )
            continue
        head_index = _shell_source_head_index(command, executable)
        if head_index is None:
            continue
        selection = _select_shell_source(argv, head_index)
        operand_indices: tuple[int, ...] = ()
        if selection.kind is _ShellSourceKind.SCRIPT and selection.argv_index is not None:
            operand_indices = (selection.argv_index,)
        elif selection.kind is _ShellSourceKind.AMBIGUOUS:
            operand_indices = selection.candidate_indices
        for index in operand_indices:
            if index < len(argv):
                yield _PositionalRead(
                    _port_tracked_resource_keys(argv[index], resources),
                    argv[index + 1 :],
                    include_zero=False,
                )
        if environment_keys is None:
            environment_keys = _bash_env_tracked_keys(
                command, sink_variables, resources, streams, limits
            )
        startup_keys = (
            tuple(
                key
                for index in selection.init_file_indices
                if index < len(argv)
                for key in _port_tracked_resource_keys(argv[index], resources)
            )
            + environment_keys
        )
        if not startup_keys:
            continue
        for arguments, include_zero in _shell_child_argument_bindings(command, selection):
            yield _PositionalRead(startup_keys, arguments, include_zero=include_zero)


def _shell_script_positional_state_unrepresentable(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a shell source composes the marker from the arguments its shell is given.

    ``_shell_script_operand_state_unrepresentable`` reads the file's own assignments, which is the
    composition a file performs out of text it contains. A file can also compose out of text the
    CALLER supplies: every word after the script operand becomes the child's ``$1``, ``$2``, ... ,
    so ``printf '%s\\n' '"$1$2" reconcile' > s.sh; bash s.sh doc- lattice`` executes the marker
    under real Bash 5.2 with no assignment anywhere in the file for the assignments-only content
    test to find. The ``-c`` spelling of the identical composition already refuses, through the
    operand binding that route received; this is that binding's missing counterpart one dispatch
    form over, and the sibling of the exported-parent-variable class AD-18 already discloses for
    these same child-run routes.

    Binding the arguments and second-parsing the file the way the ``-c`` route does is not
    available here: that parse can only read a program it can see as text, and a script operand's
    content reaches it as a ``ResourceRef`` that the eval layer folds into an opaque token
    (issue #159). So this fails closed on the conjunction instead, which is what the reviewer's
    report offered as the alternative and what keeps the cost bounded.

    Three conditions have to hold together, and each one is load bearing for over-refusal:

    1. The operand names a resource THIS script writes, the rule every guard in this family shares.
       An unwritten target is pre-existing content that was never in this analysis's purview.
    2. That file's own content reads a caller-supplied positional. A file that ignores its
       arguments cannot compose out of them, which keeps ``bash deploy.sh --verbose`` certifying.
    3. Substituting the arguments into that file's own text composes the marker. Asking instead
       whether any argument is marker-FRAGMENT capable is the trap the ``source`` guard already
       documents one function over: ordinary text answers it yes, because ``build`` ends in ``d``
       and so advances the scan from the idle entry state, and it refused ``bash s.sh build``.
       Substituting reads the file's real structure, so a fragment only counts where the file
       actually places it. What each reference is substituted BY is a choice over every argument,
       not one joined string, because a file is free to select a non-adjacent subset of its
       arguments; see ``_substitute_positional_references``.

    All three were originally applied at the exact script operand alone, one route out of the seven
    its sibling content test covers, so the same file reached by ``source``, by a glob or variable
    operand, by ``--rcfile``/``--init-file``, or through ``BASH_ENV`` certified (issue #175).
    ``_shell_positional_reads`` now enumerates every route once and this applies the conditions to
    what it yields, so no route can carry the test without the others.

    Args:
        command: The command whose executable candidates may read a shell source.
        sink_variables: The per-command solved variable table, for resolving the resource.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving the resource.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether some shell reads a script-tracked file that composes the marker from its arguments.
    """
    for read in _shell_positional_reads(command, sink_variables, resources, streams, limits):
        if not read.keys:
            continue
        binding = _argument_positional_binding(
            read.arguments, sink_variables, resources, streams, limits
        )
        if not binding.joined:
            continue
        for key in read.keys:
            content = _evaluate_with_tables(
                ResourceRef(key), sink_variables, resources, streams, limits
            )
            if _resource_composes_marker_from_binding(
                content, binding, limits, include_zero=read.include_zero
            ):
                return True
    return False


def _resource_composes_marker_from_binding(
    content: _ContentValue,
    binding: _PositionalBinding,
    limits: TaintLimits,
    *,
    include_zero: bool,
) -> bool:
    """Return whether a tracked file composes the marker once its positionals are bound.

    This is conditions 2 and 3 of ``_shell_script_positional_state_unrepresentable`` on their own,
    shared by every route that resolves a tracked file and by the launcher-fed route, which differ
    only in where the texts a positional binds to come from.

    Args:
        content: The tracked resource's solved content value.
        binding: Every text one positional reference could bind to.
        limits: The bound on content evaluation this analysis stays within.
        include_zero: Whether ``$0`` counts as a caller-supplied positional for this dispatch form.

    Returns:
        Whether the file composes the marker once that binding is substituted.
    """
    if not _resource_references_positional_parameters(content, include_zero=include_zero):
        return False
    readable = [
        text
        for alternative in content
        if not (alternative.projection_opaque or alternative.projection_incomplete)
        for text in alternative.literal_texts
    ]
    if not readable:
        # The file references a positional but this analysis cannot read its text to see where.
        # Nothing narrower than the bound texts themselves is available, so the unreadable case
        # falls back to the fragment question.
        return binding.fragment_capable
    return any(
        _substituted_text_composes_marker(text, binding, limits, include_zero=include_zero)
        for text in readable
    )


def _bash_env_shell_source_unrepresentable(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a child shell sources a script-tracked ``BASH_ENV`` with unmodeled effects.

    A non-interactive Bash child reads the file ``BASH_ENV`` names BEFORE the ``-c``, script, or
    stdin program it was selected to run, so that file's state effects are the child's. Nothing
    selected it: ``_select_shell_source`` reads argv, and this arrives through the environment, so

        printf '%s\\n' 'A=doc-' '"${A}lattice" reconcile' > env.sh
        export BASH_ENV=env.sh
        bash -c :

    certified while Bash 5.2 executed the marker. The same file reached by an argv-selected route
    already refuses on both spellings that name it there, ``bash --rcfile env.sh -ic :`` and
    ``. ./env.sh``, which isolates the environment channel rather than the file or its content.

    This is the ``--rcfile`` fix's counterpart for the channel argv cannot show. Both value
    spellings are read: an exported variable from an earlier command, and the per-command prefix
    assignment ``BASH_ENV=env.sh bash -c :``, which is scoped to the command and so never reaches
    the body-wide table.

    Export status is deliberately not modeled, so a ``BASH_ENV`` this body sets without exporting
    refuses although the child never sees it. That is the same over-approximation the whole
    ``-c`` payload second pass already makes, tracked as issue #122, and narrowing it would mean
    reading export evidence the body itself supplies, which AD-17's founding principle rules out.

    The content gate is the one the child-run routes share, plus the completed marker. A child
    shell's variables never return to this body, so the composition to detect is one the file
    performs within itself, and ``_resource_assignment_marker_fragment_capable`` is the tractable
    evidence for that. ``_marker_capable`` is asked alongside it because no sink expression reads
    this file, unlike the argv-selected routes where the operand's own value reaches the sink path:
    without it a file whose content composes the whole marker would have nothing to refuse on.
    Asking the raw FRAGMENT question instead is what would refuse
    ``echo 'make build' > env.sh``, the same trap the assignments-only split already documents.

    Args:
        command: The command whose executable candidates may be a shell.
        sink_variables: The per-command solved variable table, for resolving the value and target.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving the resource.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether a shell in this command sources a script-tracked ``BASH_ENV`` file that could
        compose the marker.
    """
    if all(
        _shell_source_head_index(command, executable) is None
        for executable in _iter_executable_evidence(command.executable)
    ):
        return False
    for key in _bash_env_tracked_keys(command, sink_variables, resources, streams, limits):
        content = _evaluate_with_tables(
            ResourceRef(key), sink_variables, resources, streams, limits
        )
        if _marker_capable(content) or _resource_assignment_marker_fragment_capable(content):
            return True
    return False


def _bash_env_tracked_keys(
    command: _CommandEvidence,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> tuple[str, ...]:
    """Return every script-tracked resource key this command's ``BASH_ENV`` could name.

    This is the channel half of ``_bash_env_shell_source_unrepresentable`` on its own, so the
    content question that guard asks and the positional question
    ``_shell_script_positional_state_unrepresentable`` asks resolve the same targets from the same
    two value spellings rather than each recognizing its own set.

    Args:
        command: The command whose prefix assignments may set ``BASH_ENV``.
        sink_variables: The per-command solved variable table, for the exported spelling.
        resources: The solved resource table; a key present here is one this script writes.
        streams: The solved stream table, for resolving a prefix assignment's value.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Every tracked resource key a ``BASH_ENV`` value in scope for this command names.
    """
    # Variables live under scoped keys, so every scope that spells this name is read rather than
    # one reconstructed scope. Reading them all is the fail-closed direction: the child inherits
    # whichever one is live, and this analysis does not track which that is.
    values = [
        value
        for name, value in sink_variables.items()
        if isinstance(name, str) and _unscoped_variable_name(name) == "BASH_ENV"
    ]
    values.extend(
        _evaluate_with_tables(assignment.content, sink_variables, resources, streams, limits)
        for assignment in (
            *command.assignments,
            *command.definite_assignments,
            *command.builtin_assignments,
        )
        if _unscoped_variable_name(assignment.name) == "BASH_ENV"
    )
    keys: list[str] = []
    for value in values:
        for alternative in value:
            for text in alternative.literal_texts:
                key = normalize_static_resource(text, dynamic=False)
                if key is not None and key in resources and key not in keys:
                    keys.append(key)
    return tuple(keys)


def _launcher_shell_head_index(
    command: _CommandEvidence, executable: _ExecutableEvidence
) -> int | None:
    """Return the argv index of a shell this candidate reaches ONLY through a launcher.

    This is the third of the routes ``_shell_source_head_index`` mirrors, isolated on its own. The
    direct and ambiguous routes are excluded deliberately: a launcher is the only one of the three
    that can hand the shell argv words this body never spells, so it is the only one whose
    positional parameters can carry content from the launcher's own input.

    Args:
        command: The command evidence being inspected.
        executable: One resolved executable candidate of that command.

    Returns:
        The launcher-reached shell's argv index, or None when this candidate reaches none.
    """
    if executable.argv_index is None or executable.name is None:
        return None
    name = executable.name
    if name == "eval" and executable.literal == "eval" and not executable.external_lookup:
        return None
    if name in {"source", "."} and executable.literal == name and not executable.external_lookup:
        return None
    if _normalized_shell_head(name) in _SHELL_HEADS or executable.ambiguous:
        return None
    if executable.external_lookup and name in _EXTERNAL_WRAPPER_SHADOW_NAMES:
        return None
    return _nested_shell_index(command, executable.argv_index)


def _launcher_input_marker_fragment_capable(summary: _TransferSummary) -> bool:
    """Return whether a launcher's input could carry marker text into a child's argv.

    A launcher splits its input into WORDS and passes them as separate argv entries, so the child
    can concatenate what the input never spelled adjacently. Asking the ordinary fragment question
    of the raw bytes misses exactly that: ``printf '%s\\n' doc- lattice`` produces
    ``doc-\\nlattice\\n``, where the newline resets the scan, yet ``xargs -n2 sh -c '$0$1'`` binds
    the two words and the child composes the marker. So the words are joined before the question is
    asked, which is what the launcher does to them.

    The raw form is asked too, since a single input word carrying a partial fragment is the case
    joining cannot make any more visible. So is each individual WORD, because the child can bind
    a non-adjacent subset of them: ``printf '%s\\n' doc- SAFE lattice`` joins to
    ``doc-SAFElattice``, which carries no marker progress at all, while the child binding only the
    first and third words composes the marker outright (issue #176).

    OPAQUE input answers no, which is the one place this deliberately does not fail closed. An
    opaque standard input is the step's own, or an untracked file's, and that is pre-existing
    content outside this analysis's purview by the same rule every guard in this family applies to
    an unwritten target. Answering yes there refused ``timeout 60 sh -c '$0 reconcile'``, an
    ordinary body real Bash runs no marker in, since a launcher that reads nothing appends nothing
    and ``$0`` stays the shell's own name. Truncated input is different and does answer yes: that
    is this body's own content, unreadable only because it passed a tracking cap.

    Args:
        summary: One alternative of the command's solved standard input value.

    Returns:
        Whether the launcher's input could feed marker-composing words into the child's argv.
    """
    if summary.projection_opaque:
        return False
    if summary.projection_incomplete:
        return True
    if _summary_marker_fragment_capable(summary):
        return True
    return any(
        _summary_marker_fragment_capable(_TransferSummary.literal(candidate))
        for text in summary.literal_texts
        for candidate in ("".join(text.split()), *text.split())
    )


def _launcher_shell_positional_state_unrepresentable(  # noqa: PLR0913
    command: _CommandEvidence,
    stdin: ContentExpr,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a launcher can feed marker text into a shell's positional parameters.

    A launcher appends words to the command it runs, taken from its own standard input:
    ``xargs --help`` documents "Run COMMAND with arguments INITIAL-ARGS and more arguments read
    from input". Those appended words are the child shell's positional parameters, and no argv
    position in this body spells them, so
    ``printf '%s%s\\n' doc- lattice | xargs -n1 sh -c '$0 reconcile'`` executes the marker under
    GNU xargs 4.9 and Bash 5.2 while every operand binding this analysis performs sees an empty
    operand list. The direct spelling ``sh -c '$0 reconcile' doc-lattice`` already refuses, which
    isolates the launcher-fed argv rather than the payload parse: an exported parent variable in
    the same ``xargs`` payload also already refuses, so the second pass does reach through it.

    Recognizing ``xargs`` by name is what AD-17's founding principle rules out, and
    ``_shell_source_sinks`` already reasons this way for the missing-operand case. So the trigger
    is the launcher's INPUT rather than its name, which is the only channel any launcher has for
    supplying argv this body never spells. That keeps the ordinary launcher certifying:
    ``timeout 60 sh -c '$0 reconcile'`` reads a stdin that carries no marker text and stays
    certified, which is correct, since Bash leaves ``$0`` as the shell's own name there.

    Both dispatch forms a launcher can feed are covered. A ``-c`` payload's operands begin at
    ``$0``, so ``$0`` counts for that form; a script operand's ``$0`` is its own path, so for that
    form the trigger is the file's own reference to ``$1`` and up, the same condition
    ``_shell_script_positional_state_unrepresentable`` applies to visibly spelled arguments.

    This is scoped to the POSITIONAL channel. ``xargs -I{}`` substitutes its input into the
    payload's own text rather than binding a positional, so no reference exists for this to find
    and ``printf '%s%s\\n' doc- lattice | xargs -I{} sh -c '{} reconcile'`` still certifies while
    real Bash runs the marker. Closing it needs a trigger that does not read the payload, and the
    only one available refuses an inert payload beside the marker-bearing input, which is a
    measurable over-refusal rather than a free widening. It is filed rather than absorbed here.

    Args:
        command: The command whose executable candidates may reach a shell through a launcher.
        stdin: The command's finalized standard input expression, which a launcher reads argv from.
        sink_variables: The per-command solved variable table.
        resources: The solved resource table.
        streams: The solved stream table.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether a launcher-reached shell reads a positional the launcher's input could supply.
    """
    candidates = [
        head_index
        for executable in _iter_executable_evidence(command.executable)
        if (head_index := _launcher_shell_head_index(command, executable)) is not None
    ]
    if not candidates:
        return False
    fed = _evaluate_with_tables(stdin, sink_variables, resources, streams, limits)
    if not any(_launcher_input_marker_fragment_capable(alternative) for alternative in fed):
        return False
    readable = sorted(
        text
        for alternative in fed
        if not alternative.projection_opaque
        for text in alternative.literal_texts
    )
    # A launcher splits its input into WORDS and appends them as separate argv entries, so each
    # word is a text one positional can bind to on its own, and the whitespace-free join of one
    # alternative is what a child concatenating all of them sees.
    binding = _PositionalBinding(
        parts=tuple(
            candidate for text in readable for candidate in (*text.split(), "".join(text.split()))
        ),
        joined="".join("".join(text.split()) for text in readable),
    )
    return any(
        _shell_source_composes_from_launcher_input(
            command, head_index, binding, sink_variables, resources, streams, limits
        )
        for head_index in candidates
    )


def _shell_source_composes_from_launcher_input(  # noqa: PLR0913
    command: _CommandEvidence,
    head_index: int,
    binding: _PositionalBinding,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether the shell source at one argv index composes the marker from launcher input.

    Substituting the input into every positional reference is the same technique
    ``_shell_script_positional_state_unrepresentable`` applies to visibly spelled arguments, and it
    is required here for the same reason: asking only whether the input is marker-FRAGMENT capable
    refused ``ls *.md | xargs -n1 sh -c 'echo $0'``, because ``.md`` ends in ``d`` and so advances
    the scan from the idle entry state. Substituting reads where the payload actually places the
    input, so it still catches the input supplying only part of the marker
    (``printf 'doc-\\n' | xargs -n1 sh -c '${0}lattice reconcile'``) while leaving ordinary
    pipelines certified.

    Args:
        command: The command evidence being inspected.
        head_index: The argv index the shell option grammar is read from.
        binding: Every text a positional may bind to, one per input word plus their join.
        sink_variables: The per-command solved variable table.
        resources: The solved resource table.
        streams: The solved stream table.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether the selected payload or script operand composes the marker from that input.
    """
    selection = _select_shell_source(command.argv, head_index)
    if selection.kind is _ShellSourceKind.COMMAND and selection.argv_index is not None:
        payload_indices: tuple[int, ...] = (selection.argv_index,)
        operand_indices: tuple[int, ...] = ()
    elif selection.kind is _ShellSourceKind.SCRIPT and selection.argv_index is not None:
        payload_indices = ()
        operand_indices = (selection.argv_index,)
    elif selection.kind is _ShellSourceKind.AMBIGUOUS:
        # Once the argv shape is uncertain any remaining word can land in either position, which
        # is the same reason ``_shell_source_sinks`` builds a choice over the whole candidate set.
        payload_indices = selection.candidate_indices
        operand_indices = selection.candidate_indices
    else:
        return False
    for index in payload_indices:
        if index >= len(command.argv):
            continue
        port = command.argv[index]
        if port.dynamic:
            # A payload this analysis cannot read as text could place the input anywhere, so the
            # unreadable case falls back to the fragment question over the input itself.
            if binding.fragment_capable:
                return True
            continue
        if _substituted_text_composes_marker(port.literal, binding, limits, include_zero=True):
            return True
    for index in operand_indices:
        if index >= len(command.argv):
            continue
        if _script_operand_composes_from_binding(
            command.argv[index], binding, sink_variables, resources, streams, limits
        ):
            return True
    return False


def _substituted_text_composes_marker(
    text: str, binding: _PositionalBinding, limits: TaintLimits, *, include_zero: bool
) -> bool:
    """Return whether binding the caller's texts into every positional composes the marker.

    The substituted expression is closed, holding only literal segments of the file's own text and
    one choice per reference, so it is evaluated against empty tables. Evaluation still goes
    through ``_evaluate_with_tables`` rather than ``_evaluate_closed`` so that an alternative width
    no bound can represent raises ``_TaintLimitExceeded`` and fails closed, the way every other
    evaluation in this module does, instead of silently narrowing the choice.

    Args:
        text: One literal alternative of the shell source being read.
        binding: Every text one positional reference could bind to.
        limits: The bound on content evaluation this analysis stays within.
        include_zero: Whether ``$0`` counts as a caller-supplied positional for this dispatch form.

    Returns:
        Whether some assignment of those texts to the references composes the marker.
    """
    if not _text_references_positional_parameters(text, include_zero=include_zero):
        return False
    substituted = _substitute_positional_references(
        text, binding.expression, include_zero=include_zero
    )
    return _marker_capable(_evaluate_with_tables(substituted, {}, {}, {}, limits))


def _script_operand_composes_from_binding(  # noqa: PLR0913
    operand: _ArgPort,
    binding: _PositionalBinding,
    sink_variables: Mapping[str | int, _ContentValue],
    resources: Mapping[str | int, _ContentValue],
    streams: Mapping[str | int, _ContentValue],
    limits: TaintLimits,
) -> bool:
    """Return whether a tracked script operand composes the marker from one supplied binding.

    Resolution and composition are both the shared ones, so a launcher-fed shell reaches the same
    tracked files through a glob or variable operand that a visibly spelled argument list reaches
    (``_port_tracked_resource_keys``), and asks them the same question
    (``_resource_composes_marker_from_binding``).

    Args:
        operand: The argv word naming the script.
        binding: Every text a positional may bind to.
        sink_variables: The per-command solved variable table.
        resources: The solved resource table.
        streams: The solved stream table.
        limits: The bound on content evaluation this analysis stays within.

    Returns:
        Whether the tracked file composes the marker once that binding is substituted.
    """
    return any(
        _resource_composes_marker_from_binding(
            _evaluate_with_tables(ResourceRef(key), sink_variables, resources, streams, limits),
            binding,
            limits,
            include_zero=False,
        )
        for key in _port_tracked_resource_keys(operand, resources)
    )


def _sink_expressions(
    command: _CommandEvidence,
    stdin: ContentExpr,
    process_resources: dict[int, _ProcessResourceEvidence],
    *,
    skip_builtin_eval: bool = False,
) -> tuple[ContentExpr, ...]:
    """Return every conservative execution sink expression for all candidates."""
    expressions: list[ContentExpr] = []
    for executable in _iter_executable_evidence(command.executable):
        if (
            skip_builtin_eval
            and executable.name == "eval"
            and executable.literal == "eval"
            and not executable.external_lookup
        ):
            continue
        expressions.extend(
            _candidate_sink_expressions(
                command,
                executable,
                stdin,
                process_resources,
            )
        )
    return tuple(expressions)


def analyze_marker_taint(  # noqa: PLR0911, PLR0912, PLR0915
    evidence: _ShellTaintEvidence,
    *,
    limits: TaintLimits = TaintLimits(),  # noqa: B008 - immutable limits value object
) -> ScanVerdict:
    """Return a discriminated verdict for authored marker flow in one run body.

    Args:
        evidence: Frozen structured evidence for one run body.
        limits: Deterministic caps for this pass.

    Returns:
        `Certified` when no authored marker flow reaches an execution sink,
        `MarkerDetected` when it does, or the originating `GuardRefusal` when a
        fail-closed bound stopped the analysis first.
    """
    if any(scope.kind not in _STREAM_SCOPE_KINDS for scope in evidence.scopes):
        return GuardRefusal(
            "taint.evidence.stream-scope-kind",
            "shell taint stream scope cannot be structured",
        )
    if any(
        (pipe.consumer_command_id is None) == (pipe.consumer_scope_id is None)
        for pipe in evidence.pipes
    ):
        return GuardRefusal(
            "taint.evidence.pipe-consumer-arity",
            "shell taint pipe cannot be structured",
        )
    if any(
        resource.direction not in _PROCESS_RESOURCE_DIRECTIONS
        for resource in evidence.process_resources
    ):
        return GuardRefusal(
            "taint.evidence.process-resource-direction",
            "shell taint process resource cannot be structured",
        )
    evidence_edges = (
        len(evidence.pipes)
        + len(evidence.process_resources)
        + sum(len(command.redirections) for command in evidence.commands)
        + sum(len(scope.redirections) for scope in evidence.scopes)
    )
    if evidence_edges > limits.max_edges:
        return GuardRefusal(
            "taint.evidence.edge-limit",
            "shell taint edge limit exceeded",
        )
    evidence_entries = (
        len(evidence.commands)
        + len(evidence.scopes)
        + len(evidence.process_resources)
        + sum(_executable_alternate_count(command.executable) for command in evidence.commands)
    )
    if evidence_entries > limits.max_table_entries:
        return GuardRefusal(
            "taint.evidence.table-entry-limit",
            "shell taint table entry limit exceeded",
        )

    try:
        _validate_nested_evidence(evidence, limits)
        function_names = frozenset(
            command.defines_function_name
            for command in evidence.commands
            if command.defines_function_name in _STATIC_EVAL_SHADOW_NAMES
        )
        if function_names:
            evidence = replace(
                evidence,
                commands=tuple(
                    replace(
                        command,
                        active_function_names=command.active_function_names | function_names,
                    )
                    if command.function_context_id is not None
                    else command
                    for command in evidence.commands
                ),
            )
        evidence = _resolve_builtin_writer_evidence(evidence, limits)
        if any(command.unsupported_builtin_write for command in evidence.commands):
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.builtin-writer.unsupported-before-context",
                    "shell builtin writer cannot be represented",
                )
            )
        evidence = _contextualize_evidence(evidence, limits=limits)
        evidence = _route_runtime_nameref_writes(evidence, limits)
        if any(command.unsupported_builtin_write for command in evidence.commands):
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.builtin-writer.unsupported-after-nameref-routing",
                    "shell builtin writer cannot be represented",
                )
            )
        definitions, inputs = _build_flow_definitions(evidence, limits=limits)
        (
            command_environment_ids,
            environment_parents,
            _lastpipe,
        ) = _execution_environment_ids(evidence)
        eval_commands = tuple(
            command for command in evidence.commands if _builtin_eval_candidates(command)
        )
        if eval_commands:
            (
                definitions,
                solved,
                eval_writes,
                eval_syntax_variables,
                command_environments,
            ) = _solve_eval_conditional_flow(evidence, definitions, eval_commands, limits)
        else:
            solved = _solve_flow_definitions(definitions, limits=limits)
            eval_writes = ()
            eval_syntax_variables = {}
            command_environments = _eval_command_environments(
                evidence,
                solved.variables,
                {},
                command_environment_ids,
                environment_parents,
                limits,
            )
        ordered_eval_syntax_variables = (
            _ordered_eval_syntax_variables(
                eval_writes,
                solved.variables,
                limits,
                eval_syntax_variables,
            )
            if eval_commands
            else {}
        )
        ordered_eval_context = _EvalSyntaxContext(
            ordered_eval_syntax_variables,
            solved.variables,
            limits,
            _eval_syntax_programs(eval_writes),
        )
        if any(
            _eval_sink_marker_capable(
                command,
                ordered_eval_context,
                command_environments[command.command_id],
                limits=limits,
            )
            for command in eval_commands
        ):
            return MarkerDetected()
        eval_context = _EvalSyntaxContext(
            eval_syntax_variables,
            solved.variables,
            limits,
            _eval_syntax_programs(eval_writes),
        )
        process_resources = {
            resource.resource_id: resource for resource in evidence.process_resources
        }
        for command in evidence.commands:
            if command in eval_commands and _eval_sink_marker_capable(
                command, eval_context, command_environments[command.command_id], limits=limits
            ):
                return MarkerDetected()
            stdin = inputs[command.command_id]
            if _shell_command_payload_marker_capable(
                command,
                stdin,
                eval_context,
                command_environments[command.command_id],
                limits=limits,
            ):
                return MarkerDetected()
            sink_variables: Mapping[str | int, _ContentValue] = ChainMap(
                dict(command_environments[command.command_id].fixed_point_overrides),
                solved.variables,
            )
            for expression in _sink_expressions(
                command,
                stdin,
                process_resources,
                skip_builtin_eval=(
                    command.runtime_eval_program_authoritative
                    or any(
                        _strip_active_shell_comments(program) != program
                        for program in _static_eval_programs(command)
                    )
                ),
            ):
                if _marker_capable(
                    _evaluate_with_tables(
                        expression,
                        sink_variables,
                        solved.resources,
                        solved.streams,
                        limits,
                    )
                ):
                    return MarkerDetected()
            if _source_payload_state_unrepresentable(
                command,
                sink_variables,
                solved.resources,
                solved.streams,
                limits,
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.source.payload-state",
                        "shell source payload state cannot be represented",
                    )
                )
            if _source_glob_operand_state_unrepresentable(
                command,
                sink_variables,
                solved.resources,
                solved.streams,
                limits,
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.source.glob-operand-state",
                        "shell source glob operand state cannot be represented",
                    )
                )
            if _glob_script_operand_state_unrepresentable(command, solved.resources, limits):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.glob-script.operand-state",
                        "shell glob script operand state cannot be represented",
                    )
                )
            if _shell_script_operand_state_unrepresentable(
                command,
                sink_variables,
                solved.resources,
                solved.streams,
                limits,
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.shell-script.operand-state",
                        "shell script operand state cannot be represented",
                    )
                )
            if _shell_script_positional_state_unrepresentable(
                command,
                sink_variables,
                solved.resources,
                solved.streams,
                limits,
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.shell-script.positional-state",
                        "shell script positional state cannot be represented",
                    )
                )
            if _launcher_shell_positional_state_unrepresentable(
                command,
                stdin,
                sink_variables,
                solved.resources,
                solved.streams,
                limits,
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.launcher-shell.positional-state",
                        "launcher-fed shell positional state cannot be represented",
                    )
                )
            if _bash_env_shell_source_unrepresentable(
                command,
                sink_variables,
                solved.resources,
                solved.streams,
                limits,
            ):
                raise _TaintLimitExceeded(
                    GuardRefusal(
                        "taint.bash-env.source-state",
                        "shell BASH_ENV source state cannot be represented",
                    )
                )
        if any(_printf_b_unrepresentable(command, limits) for command in evidence.commands):
            raise _TaintLimitExceeded(
                GuardRefusal(
                    "taint.printf-b.unrepresentable-output",
                    "dynamic printf %b output cannot be represented",
                )
            )
    except (_MalformedTaintEvidence, _TaintLimitExceeded) as error:
        return error.refusal
    return Certified()
