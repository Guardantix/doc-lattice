"""Bounded non-executing scanner for direct doc-lattice shell invocations."""

import re
from dataclasses import dataclass, field
from enum import Enum, auto

from doc_lattice.error_types import ConfigError, ProjectError

_Invocation = tuple[str, bool]
_MAX_SHELL_SOURCE_CHARS = 1_048_576
_MAX_SHELL_SCAN_STEPS = 4_194_304
_MAX_SHELL_RECURSION_DEPTH = 64
_MAX_SHELL_INVOCATIONS = 10_000
_MAX_LAUNCHER_NESTING_DEPTH = 64
_OCTAL_BASE = 8
_ANSI_C_OCTAL_BYTE_MASK = 0xFF
_UNICODE_MAX = 0x10FFFF
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF

_COMMAND_PREFIXES = frozenset(
    {
        "!",
        "do",
        "elif",
        "else",
        "if",
        "then",
        "until",
        "while",
    }
)
_SHELL_ASSIGNMENT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PYTHON_DISTRIBUTION_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_PYTHON_DISTRIBUTION_SEPARATOR_RE = re.compile(r"[-_.]+")
_UV_REQUIREMENT_SUFFIX_STARTS = frozenset("([<>=!~@;")
_ENV_SPLIT_STRING_LONG_OPTION = "--split-string"
_ENV_LONG_OPTION_KINDS = {
    "--argv0": "required",
    "--block-signal": "optional",
    "--chdir": "required",
    "--debug": "flag",
    "--default-signal": "optional",
    "--help": "stop",
    "--ignore-environment": "flag",
    "--ignore-signal": "optional",
    "--list-signal-handling": "flag",
    "--null": "flag",
    _ENV_SPLIT_STRING_LONG_OPTION: "split",
    "--unset": "required",
    "--version": "stop",
}
_ENV_SHORT_FLAGS = frozenset({"0", "i", "v"})
_ENV_SHORT_REQUIRED = frozenset({"a", "C", "u"})
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
_COMMAND_OPERATORS = (
    ";;&",
    "&&",
    "||",
    ";;",
    ";&",
    ";",
    "&",
    "|",
    "(",
    ")",
    "{",
    "}",
)
_WORD_BREAKS = frozenset(" \t\n;&|()<>")

_UV_SHARED_OPTIONS_WITH_ARGUMENTS = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--config-settings-package",
        "--default-index",
        "--directory",
        "--exclude-newer",
        "--exclude-newer-package",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-sources-package",
        "--prerelease",
        "--project",
        "--python",
        "--python-platform",
        "--refresh-package",
        "--reinstall-package",
        "--resolution",
        "--upgrade-group",
        "--upgrade-package",
        "-C",
        "-P",
        "-f",
        "-i",
        "-p",
    }
)
_UVX_OPTIONS_WITH_ARGUMENTS = _UV_SHARED_OPTIONS_WITH_ARGUMENTS | frozenset(
    {
        "--build-constraints",
        "--constraints",
        "--env-file",
        "--from",
        "--overrides",
        "--torch-backend",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-b",
        "-c",
        "-w",
    }
)
_UV_RUN_OPTIONS_WITH_ARGUMENTS = _UV_SHARED_OPTIONS_WITH_ARGUMENTS | frozenset(
    {
        "--env-file",
        "--extra",
        "--group",
        "--no-editable-package",
        "--no-extra",
        "--no-group",
        "--only-group",
        "--package",
        "--with-requirements",
        # --with, --with-editable, and -w attach extra dependencies for the run.
        "--with",
        "--with-editable",
        "-w",
    }
)
_UV_HELP_OPTIONS = frozenset({"--help", "-h"})
_UV_VERSION_OPTIONS = frozenset({"--version", "-V"})
_UV_GLOBAL_STOP_OPTIONS = _UV_HELP_OPTIONS | _UV_VERSION_OPTIONS
_UV_RUN_NON_COMMAND_OPTIONS = frozenset(
    {
        "--gui-script",
        "--module",
        "--script",
        "-m",
        "-s",
    }
)
_UV_GLOBAL_OPTIONS_WITH_ARGUMENTS = frozenset(
    {
        "--allow-insecure-host",
        "--cache-dir",
        "--color",
        "--config-file",
        "--directory",
        "--project",
    }
)
_UV_GLOBAL_FLAGS = frozenset(
    {
        "--managed-python",
        "--native-tls",
        "--no-cache",
        "--no-config",
        "--no-managed-python",
        "--no-progress",
        "--no-python-downloads",
        "--offline",
        "--quiet",
        "--verbose",
        "-n",
        "-q",
        "-v",
    }
)
_UV_LAUNCHER_FLAGS = frozenset(
    {
        "--active",
        "--frozen",
        "--isolated",
        "--locked",
        "--managed-python",
        "--native-tls",
        "--no-cache",
        "--no-config",
        "--no-dev",
        "--no-editable",
        "--no-env-file",
        "--no-managed-python",
        "--no-progress",
        "--no-project",
        "--no-python-downloads",
        "--no-sync",
        "--offline",
        "--quiet",
        "--verbose",
        "-q",
        "-v",
    }
)
_UV_TOOL_RUN_FLAGS = _UV_LAUNCHER_FLAGS | frozenset(
    {
        "--compile-bytecode",
        "--lfs",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-index",
        "--no-sources",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "-U",
        "-n",
    }
)
_UV_RUN_FLAGS = _UV_LAUNCHER_FLAGS | frozenset(
    {
        "--all-extras",
        "--all-groups",
        "--all-packages",
        "--compile-bytecode",
        "--exact",
        "--no-binary",
        "--no-build",
        "--no-build-isolation",
        "--no-default-groups",
        "--no-index",
        "--no-sources",
        "--only-dev",
        "--refresh",
        "--reinstall",
        "--system-certs",
        "--upgrade",
        "-U",
        "-n",
    }
)
_DOC_LATTICE_ROOT_OPTIONS = frozenset({"--no-color"})
# --help and --version are eager Typer options that exit before any subcommand runs.
_DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS = frozenset({"--version", "--help"})
_LINEAR_OPTIONS_WITH_ARGUMENTS = frozenset({"--config", "--format", "--from", "--indent"})
_LINEAR_FLAGS = frozenset({"--exit-code", "--warn-exit"})
_RECONCILE_OPTIONS_WITH_ARGUMENTS = frozenset({"--config", "--format", "--ref"})
_RECONCILE_FLAGS = frozenset({"--all", "--dry-run", "--recover"})
_RECONCILE_NON_MUTATING_OPTIONS = frozenset({"--dry-run"})


class _CommandDisposition(Enum):
    """Describe whether a recognized policy-sensitive command can run."""

    SENSITIVE = auto()
    NON_MUTATING = auto()
    NON_EXECUTING = auto()


def _short_options(options: frozenset[str]) -> tuple[str, ...]:
    """Return the single-dash options whose values may attach without a separator."""
    return tuple(option for option in options if option.startswith("-") and option[1:2] != "-")


@dataclass(frozen=True, slots=True)
class _LauncherOptions:
    """Precomputed option surface for one uv launcher, avoiding per-word set filtering."""

    options_with_arguments: frozenset[str]
    flags: frozenset[str]
    non_command_options: frozenset[str]
    short_options_with_arguments: tuple[str, ...]
    short_non_command_options: tuple[str, ...]

    @classmethod
    def build(
        cls,
        options_with_arguments: frozenset[str],
        flags: frozenset[str],
        non_command_options: frozenset[str] = frozenset(),
    ) -> "_LauncherOptions":
        """Bundle option data with its short-option subsets computed once at import."""
        return cls(
            options_with_arguments=options_with_arguments,
            flags=flags,
            non_command_options=non_command_options,
            short_options_with_arguments=_short_options(options_with_arguments),
            short_non_command_options=_short_options(non_command_options),
        )


_UVX_LAUNCHER = _LauncherOptions.build(
    _UVX_OPTIONS_WITH_ARGUMENTS,
    _UV_TOOL_RUN_FLAGS,
    _UV_HELP_OPTIONS | _UV_VERSION_OPTIONS,
)
_UV_TOOL_RUN_LAUNCHER = _LauncherOptions.build(
    _UVX_OPTIONS_WITH_ARGUMENTS,
    _UV_TOOL_RUN_FLAGS,
    _UV_HELP_OPTIONS,
)
_UV_RUN_LAUNCHER = _LauncherOptions.build(
    _UV_RUN_OPTIONS_WITH_ARGUMENTS,
    _UV_RUN_FLAGS,
    _UV_RUN_NON_COMMAND_OPTIONS | _UV_HELP_OPTIONS,
)
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
class _ShellWord:
    literal: str
    dynamic: bool = False
    locale_translated: bool = False
    unquoted_dynamic: bool = False
    quoted_zero_field_expansion: bool = False
    active_argv_expansion: bool = False
    shell_assignment: bool = False
    keyword_eligible: bool = True


@dataclass(slots=True)
class _ShellWordBuilder:
    characters: list[str]
    active_syntax: list[str]
    dynamic: bool = False
    locale_translated: bool = False
    unquoted_dynamic: bool = False
    quoted_zero_field_expansion: bool = False
    assignment_name_is_literal: bool = True
    assignment_name: str = ""
    shell_assignment: bool = False
    keyword_eligible: bool = True

    def append_protected(
        self,
        segment: str | list[str],
        *,
        dynamic: bool = False,
        locale_translated: bool = False,
        unquoted_dynamic: bool = False,
        quoted_zero_field_expansion: bool = False,
    ) -> None:
        """Append text protected from literal argv expansion."""
        self.characters.extend(segment)
        self.active_syntax.append(" ")
        self.dynamic = self.dynamic or dynamic
        self.locale_translated = self.locale_translated or locale_translated
        self.unquoted_dynamic = self.unquoted_dynamic or unquoted_dynamic
        self.quoted_zero_field_expansion = (
            self.quoted_zero_field_expansion or quoted_zero_field_expansion
        )
        self.keyword_eligible = False
        if not self.shell_assignment:
            self.assignment_name_is_literal = False

    def append_active(self, character: str) -> None:
        """Append one unquoted, unescaped literal character."""
        self.characters.append(character)
        self.active_syntax.append(character)
        if not self.assignment_name_is_literal or self.shell_assignment:
            return
        if character == "=":
            assignment_name = self.assignment_name.removesuffix("+")
            self.shell_assignment = bool(_SHELL_ASSIGNMENT_NAME_RE.fullmatch(assignment_name))
            self.assignment_name_is_literal = False
            return
        self.assignment_name += character

    def build(self) -> _ShellWord:
        """Build the immutable decoded word and its expansion provenance."""
        return _ShellWord(
            literal="".join(self.characters),
            dynamic=self.dynamic,
            locale_translated=self.locale_translated,
            unquoted_dynamic=self.unquoted_dynamic,
            quoted_zero_field_expansion=self.quoted_zero_field_expansion,
            active_argv_expansion=_has_active_argv_expansion("".join(self.active_syntax)),
            shell_assignment=self.shell_assignment,
            keyword_eligible=self.keyword_eligible,
        )


def _reject_active_extglob_opener(
    builder: _ShellWordBuilder,
    boundary: str,
    *,
    enabled: bool = True,
) -> None:
    """Reject an unquoted extglob opener before ``(`` becomes a command-group operator."""
    if (
        enabled
        and boundary == "("
        and builder.active_syntax
        and builder.active_syntax[-1] in "?*+@!"
    ):
        raise _ShellScanIncomplete("extglob expansion cannot be scanned safely")


@dataclass(frozen=True, slots=True)
class _ShellExpansion:
    """One consumed shell expansion and whether quoted syntax can yield no argv field."""

    end: int
    quoted_zero_field_expansion: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedIndex:
    """A static grammar position plus ambiguity inherited from prior syntax."""

    index: int | None
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class _UvGlobalResolution:
    """The static uv subcommand plus alternate launcher starts from dynamic grammar."""

    index: int | None
    ambiguous: bool = False
    launcher_starts: tuple[int, ...] = ()
    unresolved_option: bool = False


@dataclass(frozen=True, slots=True)
class _LauncherPayloadRequest:
    """The grammar and provenance used to resolve one selected launcher payload."""

    options: _LauncherOptions
    strip_version: bool
    inherited_ambiguity: bool
    fail_on_unknown: bool
    launcher_depth: int


@dataclass(frozen=True, slots=True)
class _Heredoc:
    delimiter: str
    strip_tabs: bool
    expand: bool


@dataclass(slots=True)
class _CommandScanState:
    words: list[_ShellWord]
    heredocs: list[_Heredoc]
    cases: list["_CaseScanState"]
    prefix_mode: str = "normal"
    prefix_pending: int = 0
    at_command_position: bool = True

    def reset_command(self) -> None:
        """Clear the accumulated simple command and its incremental prefix-scan state."""
        self.words.clear()
        self.prefix_mode = "normal"
        self.prefix_pending = 0
        self.at_command_position = True


@dataclass(slots=True)
class _CaseScanState:
    phase: str
    pattern_parentheses: int = 0
    at_pattern_start: bool = True


@dataclass(frozen=True, slots=True)
class ShellScanResult:
    """Complete invocations or an explicit reason the bounded scan stopped."""

    invocations: tuple[_Invocation, ...]
    incomplete_reason: str | None = None


class _ShellScanIncomplete(ProjectError):
    """A declared scanner resource bound prevented a complete result."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SHELL_SCAN_INCOMPLETE")


@dataclass(slots=True)
class _ScanBudget:
    remaining_steps: int = _MAX_SHELL_SCAN_STEPS

    def step(self) -> None:
        """Charge one scan step, raising when the declared step budget is exhausted."""
        if self.remaining_steps < 1:
            raise _ShellScanIncomplete("step limit exceeded")
        self.remaining_steps -= 1


@dataclass(slots=True)
class _LauncherResolutionState:
    """Shared budget and memoized states for one simple command's launcher grammar."""

    budget: _ScanBudget
    cache: dict[tuple[str, int, int], _ResolvedIndex] = field(default_factory=dict)

    def step(self) -> None:
        """Charge speculative launcher work to the shell scanner's declared budget."""
        self.budget.step()


class _ShellScanner:
    def __init__(
        self,
        source: str,
        *,
        budget: _ScanBudget | None = None,
        invocations: list[_Invocation] | None = None,
        classify_commands: bool = True,
    ) -> None:
        self.source = source
        self.budget = budget if budget is not None else _ScanBudget()
        self.invocations = invocations if invocations is not None else []
        self.classify_commands = classify_commands

    def scan(self) -> tuple[_Invocation, ...]:
        self._scan_commands(0, len(self.source), terminator=None, depth=0)
        return tuple(self.invocations)

    def _scan_commands(
        self,
        start: int,
        limit: int,
        *,
        terminator: str | None,
        depth: int,
    ) -> int:
        if depth > _MAX_SHELL_RECURSION_DEPTH:
            raise _ShellScanIncomplete("recursion limit exceeded")
        state = _CommandScanState(words=[], heredocs=[], cases=[])
        index = start
        while index < limit:
            self.budget.step()
            character = self.source[index]
            if character == ")" and self._consume_case_pattern_close(state):
                index += 1
                continue
            if terminator is not None and character == terminator:
                self._flush_command(state)
                return index + 1
            boundary_end = self._consume_command_boundary(
                index,
                limit,
                state,
                depth,
            )
            if boundary_end is not None:
                index = boundary_end
                continue
            if self.source.startswith("((", index):
                index = self._consume_arithmetic_command(index, limit, state, depth)
                continue
            if self.source.startswith(("<(", ">("), index, limit):
                word, index = self._parse_word(index, limit, depth)
                self._record_word(state, word)
                continue
            redirection = self._redirection_at(index, limit)
            if redirection is not None:
                index, heredoc = self._consume_redirection(
                    redirection,
                    limit,
                    depth,
                )
                if heredoc is not None:
                    state.heredocs.append(heredoc)
                continue
            operator_end = self._consume_command_operator(index, limit, state, depth)
            if operator_end is not None:
                index = operator_end
                continue
            word, next_index = self._parse_word(index, limit, depth)
            if next_index == index:
                index += 1
                continue
            self._record_word(state, word)
            index = next_index
        self._flush_command(state)
        return index

    def _consume_arithmetic_command(
        self,
        index: int,
        limit: int,
        state: _CommandScanState,
        depth: int,
    ) -> int:
        """Consume a ``(( ... ))`` arithmetic command or its subshell fallback.

        Flushes any pending simple command, then either skips balanced arithmetic or, when the
        leading ``(`` actually opened a subshell (an unbalanced region such as ``((cmd) )``),
        rescans the region as a command group so inner invocations stay visible.

        Args:
            index: Index of the opening ``((``.
            limit: Exclusive scan limit.
            state: The command being accumulated, flushed before the arithmetic region.
            depth: Current recursion depth.

        Returns:
            The index just past the consumed region.
        """
        self._flush_command(state)
        arithmetic_end = self._consume_arithmetic(index + 2, limit, depth + 1)
        if arithmetic_end is not None:
            return arithmetic_end
        return self._scan_commands(index + 1, limit, terminator=")", depth=depth + 1)

    def _consume_array_assignment(
        self,
        index: int,
        limit: int,
        depth: int,
    ) -> int:
        """Consume compound assignment data while retaining executable expansions."""
        if depth > _MAX_SHELL_RECURSION_DEPTH:
            raise _ShellScanIncomplete("recursion limit exceeded")
        parentheses = 1
        at_word_start = True
        while index < limit:
            self.budget.step()
            character = self.source[index]
            if character in " \t\n":
                index += 1
                at_word_start = True
                continue
            if character == "#" and at_word_start:
                index = self._comment_end(index, limit)
                continue
            process_end = self._consume_process_substitution(index, limit, depth)
            if process_end is not None:
                index = process_end
                at_word_start = False
                continue
            if character == "(":
                parentheses += 1
                index += 1
                at_word_start = False
                continue
            if character == ")":
                parentheses -= 1
                index += 1
                if parentheses == 0:
                    return index
                at_word_start = False
                continue
            _word, next_index = self._parse_word(
                index,
                limit,
                depth,
                reject_extglob=False,
            )
            if next_index != index:
                index = next_index
                at_word_start = False
                continue
            index += 1
            at_word_start = character in ";&|"
        return index

    def _consume_command_operator(
        self,
        index: int,
        limit: int,
        state: _CommandScanState,
        depth: int,
    ) -> int | None:
        operator = self._command_operator_at(index, limit)
        if operator is None:
            return None
        index += len(operator)
        if self._consume_case_pattern_operator(state, operator):
            return index
        if (
            operator == "("
            and state.words
            and state.words[-1].shell_assignment
            and state.words[-1].literal.endswith("=")
        ):
            return self._consume_array_assignment(index, limit, depth + 1)
        self._flush_command(state)
        self._advance_case_body(state, operator)
        if operator == "(":
            return self._scan_commands(
                index,
                limit,
                terminator=")",
                depth=depth + 1,
            )
        return index

    def _record_word(self, state: _CommandScanState, word: _ShellWord) -> None:
        command_position = state.at_command_position
        if (
            not word.dynamic
            and word.keyword_eligible
            and command_position
            and word.literal == "case"
        ):
            state.cases.append(_CaseScanState(phase="word"))
        elif state.cases:
            case = state.cases[-1]
            if case.phase == "word":
                case.phase = "in"
            elif not word.dynamic and case.phase == "in" and word.literal == "in":
                case.phase = "pattern"
                case.at_pattern_start = True
            elif not word.dynamic and case.phase == "pattern":
                if word.literal == "esac" and case.at_pattern_start:
                    state.cases.pop()
                else:
                    case.at_pattern_start = False
        self._advance_prefix_scan(state, word)
        state.words.append(word)

    def _advance_prefix_scan(self, state: _CommandScanState, word: _ShellWord) -> None:
        """Track incrementally whether the next word sits at the simple-command position.

        This tracks deterministic portions of ``_skip_shell_prefixes`` one word at a time so
        ``_record_word`` avoids a full left-to-right rescan of the accumulated words on every
        append. Ambiguous command-position expansions end incremental tracking; final prefix
        resolution revisits them to fail closed if they could expose a payload. Once the running
        scan leaves the prefix region it stays there until the command is reset.

        Args:
            state: The command being accumulated, whose prefix-scan fields are updated in place.
            word: The word just appended to ``state.words``.
        """
        if not state.at_command_position:
            return
        if state.prefix_pending > 0:
            state.prefix_pending -= 1
            return
        if state.prefix_mode != "normal" and self._advance_prefix_wrapper(state, word):
            return
        self._advance_prefix_normal(state, word)

    def _prefix_stop(self, state: _CommandScanState) -> None:
        """Mark the running scan as having left the simple-command prefix region."""
        state.prefix_mode = "stopped"
        state.at_command_position = False

    def _advance_prefix_wrapper(self, state: _CommandScanState, word: _ShellWord) -> bool:
        """Advance a multi-word wrapper prefix, returning whether the word is fully handled.

        A ``False`` result means the wrapper ended on this word, which must then be
        re-evaluated in normal mode by the caller.
        """
        mode = state.prefix_mode
        if mode in {"command_v", "env_stop"}:
            return True
        if mode == "env_dashdash":
            if word.dynamic or _command_boundary_word_may_disappear(word):
                self._prefix_stop(state)
                return True
            if _is_env_assignment_operand(word.literal):
                return True
            state.prefix_mode = "normal"
            return False
        if (
            mode in {"command_dashdash", "exec_dashdash"}
            or word.dynamic
            or _command_boundary_word_may_disappear(word)
        ):
            self._prefix_stop(state)
            return True
        literal = word.literal
        if mode == "time":
            state.prefix_mode = "normal"
            handled = literal == "-p"
        elif mode == "env":
            handled = self._advance_prefix_env(state, literal)
        elif mode in {"builtin", "builtin_target"}:
            handled = self._advance_prefix_builtin(
                state,
                literal,
                allow_dashdash=mode == "builtin",
            )
        elif mode == "command":
            handled = self._advance_prefix_command(state, literal)
        else:
            handled = self._advance_prefix_exec(state, literal)
        return handled

    def _advance_prefix_env(self, state: _CommandScanState, literal: str) -> bool:
        if literal == "--":
            state.prefix_mode = "env_dashdash"
            return True
        if literal.startswith("--"):
            option, attached_value = _resolve_env_long_option(literal)
            kind = _ENV_LONG_OPTION_KINDS[option]
            if kind == "split":
                raise _ShellScanIncomplete("env split-string option cannot be scanned safely")
            if kind == "stop":
                state.prefix_mode = "env_stop"
            elif kind == "required" and not attached_value:
                state.prefix_pending = 1
            elif kind == "flag" and attached_value:
                raise _ShellScanIncomplete("unsupported env option cannot be scanned safely")
            return True
        if literal.startswith("-"):
            if _env_short_option_requires_separate_value(literal):
                state.prefix_pending = 1
            return True
        if _is_env_assignment_operand(literal):
            return True
        state.prefix_mode = "normal"
        return False

    def _advance_prefix_command(self, state: _CommandScanState, literal: str) -> bool:
        if literal == "--":
            state.prefix_mode = "command_dashdash"
            return True
        if not literal.startswith("-"):
            self._prefix_stop(state)
            return True
        if "v" in literal[1:] or "V" in literal[1:]:
            state.prefix_mode = "command_v"
        return True

    def _advance_prefix_builtin(
        self,
        state: _CommandScanState,
        literal: str,
        *,
        allow_dashdash: bool,
    ) -> bool:
        """Follow only builtin targets that can expose a supported command wrapper."""
        if allow_dashdash and literal == "--":
            state.prefix_mode = "builtin_target"
        elif literal == "builtin":
            state.prefix_mode = "builtin"
        elif literal in {"command", "exec"}:
            state.prefix_mode = literal
        else:
            self._prefix_stop(state)
        return True

    def _advance_prefix_exec(self, state: _CommandScanState, literal: str) -> bool:
        if literal == "--":
            state.prefix_mode = "exec_dashdash"
            return True
        if literal.startswith("-"):
            if _exec_option_requires_separate_argv0(literal):
                state.prefix_pending = 1
        else:
            self._prefix_stop(state)
        return True

    def _advance_prefix_normal(self, state: _CommandScanState, word: _ShellWord) -> None:
        literal = word.literal
        if word.shell_assignment:
            return
        if _command_boundary_word_may_disappear(word):
            self._prefix_stop(state)
            return
        if word.dynamic:
            self._prefix_stop(state)
            return
        if word.keyword_eligible and literal in _COMMAND_PREFIXES:
            return
        if word.keyword_eligible and literal == "time":
            state.prefix_mode = literal
            return
        if literal in {"builtin", "env", "command", "exec"}:
            state.prefix_mode = literal
            return
        self._prefix_stop(state)

    def _consume_case_pattern_close(self, state: _CommandScanState) -> bool:
        if not state.cases or state.cases[-1].phase != "pattern":
            return False
        case = state.cases[-1]
        if case.pattern_parentheses:
            case.pattern_parentheses -= 1
        else:
            state.reset_command()
            case.phase = "body"
        return True

    def _consume_case_pattern_operator(
        self,
        state: _CommandScanState,
        operator: str,
    ) -> bool:
        if not state.cases or state.cases[-1].phase != "pattern":
            return False
        case = state.cases[-1]
        if operator == "(":
            if case.at_pattern_start:
                case.at_pattern_start = False
            else:
                case.pattern_parentheses += 1
        return True

    def _advance_case_body(self, state: _CommandScanState, operator: str) -> None:
        if state.cases and state.cases[-1].phase == "body" and operator in {";;", ";&", ";;&"}:
            case = state.cases[-1]
            case.phase = "pattern"
            case.pattern_parentheses = 0
            case.at_pattern_start = True

    def _consume_command_boundary(
        self,
        index: int,
        limit: int,
        state: _CommandScanState,
        depth: int,
    ) -> int | None:
        if self.source.startswith("\\\n", index):
            return index + 2
        character = self.source[index]
        if character in " \t":
            return index + 1
        if character == "#":
            return self._comment_end(index, limit)
        if character != "\n":
            return None
        self._flush_command(state)
        index += 1
        if state.heredocs:
            index = self._consume_heredocs(
                index,
                limit,
                state.heredocs,
                depth,
            )
            state.heredocs.clear()
        return index

    def _flush_command(self, state: _CommandScanState) -> None:
        if not state.words:
            return
        if self.classify_commands:
            invocation = _invocation_in_simple_command(state.words, self.budget)
            if invocation is not None:
                if len(self.invocations) >= _MAX_SHELL_INVOCATIONS:
                    raise _ShellScanIncomplete("invocation limit exceeded")
                self.invocations.append(invocation)
        state.reset_command()

    def _consume_process_substitution(
        self,
        index: int,
        limit: int,
        depth: int,
    ) -> int | None:
        if not (self.source.startswith("<(", index) or self.source.startswith(">(", index)):
            return None
        return self._scan_commands(
            index + 2,
            limit,
            terminator=")",
            depth=depth + 1,
        )

    def _redirection_at(
        self,
        index: int,
        limit: int,
    ) -> tuple[int, str] | None:
        operator_index = index
        if self.source[index].isdigit():
            while operator_index < limit and self.source[operator_index].isdigit():
                operator_index += 1
        elif self.source[index] == "{":
            closing = self.source.find("}", index + 1, limit)
            if closing != -1 and _is_name(self.source[index + 1 : closing]):
                operator_index = closing + 1
        for operator in _REDIRECTION_OPERATORS:
            if self.source.startswith(operator, operator_index):
                return operator_index + len(operator), operator
        return None

    def _consume_redirection(
        self,
        redirection: tuple[int, str],
        limit: int,
        depth: int,
    ) -> tuple[int, _Heredoc | None]:
        index, operator = redirection
        while index < limit and self.source[index] in " \t":
            index += 1
        if operator in {"<<", "<<-"}:
            delimiter, quoted, index = self._parse_heredoc_delimiter(index, limit, depth)
            if delimiter is None:
                return index, None
            return (
                index,
                _Heredoc(
                    delimiter=delimiter,
                    strip_tabs=operator == "<<-",
                    expand=not quoted,
                ),
            )
        _target, index = self._parse_word(index, limit, depth)
        return index, None

    def _parse_heredoc_delimiter(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> tuple[str | None, bool, int]:
        characters: list[str] = []
        quoted = False
        index = start
        if index >= limit or self.source[index] in _WORD_BREAKS:
            return None, quoted, index
        while index < limit and self.source[index] not in _WORD_BREAKS:
            if self.source.startswith("$'", index):
                segment, index, closed = _read_ansi_c_quoted_segment(
                    self.source,
                    index,
                    limit,
                )
                if not closed:
                    return None, True, index
                characters.extend(segment)
                quoted = True
                continue
            if self.source.startswith('$"', index):
                raise _ShellScanIncomplete(
                    "locale-translated heredoc delimiter cannot be scanned safely"
                )
            character = self.source[index]
            if character in {"'", '"'}:
                segment, index, closed = _read_simple_quoted_segment(
                    self.source,
                    index,
                    limit,
                    character,
                )
                if not closed:
                    return None, True, index
                characters.extend(segment)
                quoted = True
                continue
            if character == "\\" and index + 1 < limit:
                if self.source[index + 1] == "\n":
                    index += 2
                    continue
                characters.append(self.source[index + 1])
                quoted = True
                index += 2
                continue
            expansion = self._consume_literal_heredoc_expansion(index, limit, depth)
            if expansion is not None:
                characters.extend(self.source[index : expansion.end])
                index = expansion.end
                continue
            characters.append(character)
            index += 1
        return "".join(characters), quoted, index

    def _consume_literal_heredoc_expansion(
        self,
        index: int,
        limit: int,
        depth: int,
    ) -> _ShellExpansion | None:
        """Consume expansion-shaped delimiter syntax without classifying its commands."""
        lexer = _ShellScanner(
            self.source,
            budget=self.budget,
            classify_commands=False,
        )
        return lexer._consume_active_expansion(index, limit, depth)

    def _consume_heredocs(
        self,
        start: int,
        limit: int,
        heredocs: list[_Heredoc],
        depth: int,
    ) -> int:
        index = start
        for heredoc in heredocs:
            body_start = index
            body_end = limit
            after_delimiter = limit
            while index <= limit:
                logical_line_start = index
                if heredoc.expand:
                    candidate, index = self._consume_unquoted_heredoc_line(
                        index,
                        limit,
                        heredoc.strip_tabs,
                    )
                else:
                    self.budget.step()
                    line_end = self._line_end(index, limit)
                    candidate = self.source[index:line_end]
                    if heredoc.strip_tabs:
                        candidate = candidate.lstrip("\t")
                    index = (
                        line_end + 1
                        if line_end < limit and self.source[line_end] == "\n"
                        else limit + 1
                    )
                if candidate == heredoc.delimiter:
                    body_end = logical_line_start
                    after_delimiter = min(index, limit)
                    break
            if heredoc.expand:
                body = _remove_active_line_continuations(self.source[body_start:body_end])
                child = _ShellScanner(
                    body,
                    budget=self.budget,
                    invocations=self.invocations,
                    classify_commands=self.classify_commands,
                )
                child._scan_heredoc_expansions(0, len(body), depth + 1)
            index = after_delimiter
        return min(index, limit)

    def _consume_unquoted_heredoc_line(
        self,
        start: int,
        limit: int,
        strip_tabs: bool,
    ) -> tuple[str, int]:
        """Read one logical unquoted-heredoc line after active continuations."""
        parts: list[str] = []
        index = start
        while index <= limit:
            self.budget.step()
            line_end = self._line_end(index, limit)
            physical_line = self.source[index:line_end]
            if strip_tabs:
                physical_line = physical_line.lstrip("\t")

            backslash_start = len(physical_line)
            while backslash_start > 0 and physical_line[backslash_start - 1] == "\\":
                backslash_start -= 1
            trailing_backslashes = len(physical_line) - backslash_start
            if line_end < limit and trailing_backslashes % 2 == 1:
                parts.append(physical_line[:-1])
                index = line_end + 1
                continue

            parts.append(physical_line)
            next_index = line_end + 1 if line_end < limit else limit + 1
            return "".join(parts), next_index
        return "".join(parts), limit + 1

    def _scan_heredoc_expansions(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> None:
        index = start
        while index < limit:
            self.budget.step()
            if self.source[index] == "\\":
                index = min(index + 2, limit)
                continue
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                index = expansion_end.end
                continue
            index += 1

    def _parse_word(
        self,
        start: int,
        limit: int,
        depth: int,
        *,
        reject_extglob: bool = True,
    ) -> tuple[_ShellWord, int]:
        builder = _ShellWordBuilder([], [])
        index = start
        while index < limit and self._word_component_at(index, limit):
            self.budget.step()
            if self.source.startswith("$'", index):
                segment, index, _closed = _read_ansi_c_quoted_segment(
                    self.source,
                    index,
                    limit,
                )
                builder.append_protected(segment)
                continue
            if self.source.startswith('$"', index):
                segment, index, _fragment_dynamic, fragment_zero_field = self._parse_double_quoted(
                    index + 2,
                    limit,
                    depth,
                )
                builder.append_protected(
                    segment,
                    dynamic=True,
                    locale_translated=True,
                    quoted_zero_field_expansion=fragment_zero_field,
                )
                continue
            character = self.source[index]
            if character == "'":
                closing = self.source.find("'", index + 1, limit)
                if closing == -1:
                    builder.append_protected(self.source[index + 1 : limit])
                    return builder.build(), limit
                builder.append_protected(self.source[index + 1 : closing])
                index = closing + 1
                continue
            if character == '"':
                segment, index, fragment_dynamic, fragment_zero_field = self._parse_double_quoted(
                    index + 1,
                    limit,
                    depth,
                )
                builder.append_protected(
                    segment,
                    dynamic=fragment_dynamic,
                    quoted_zero_field_expansion=fragment_zero_field,
                )
                continue
            if character == "\\":
                if index + 1 < limit and self.source[index + 1] == "\n":
                    index += 2
                    continue
                if index + 1 < limit:
                    builder.append_protected(self.source[index + 1])
                    index += 2
                else:
                    builder.append_protected("")
                    index += 1
                continue
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                builder.append_protected("", dynamic=True, unquoted_dynamic=True)
                index = expansion_end.end
                continue
            process_end = self._consume_process_substitution(index, limit, depth)
            if process_end is not None:
                builder.append_protected("", dynamic=True)
                index = process_end
                continue
            builder.append_active(character)
            index += 1
        _reject_active_extglob_opener(
            builder,
            self.source[index : index + 1],
            enabled=reject_extglob,
        )
        return builder.build(), index

    def _word_component_at(self, index: int, limit: int) -> bool:
        """Return whether syntax at ``index`` continues the current shell word."""
        return self.source[index] not in _WORD_BREAKS or self.source.startswith(
            ("<(", ">("), index, limit
        )

    def _parse_double_quoted(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> tuple[list[str], int, bool, bool]:
        characters: list[str] = []
        dynamic = False
        quoted_zero_field_expansion = False
        index = start
        while index < limit:
            self.budget.step()
            character = self.source[index]
            if character == '"':
                return characters, index + 1, dynamic, quoted_zero_field_expansion
            if character == "\\" and index + 1 < limit:
                escaped = self.source[index + 1]
                if escaped == "\n":
                    index += 2
                    continue
                if escaped in {"$", '"', "\\", "`"}:
                    characters.append(escaped)
                    index += 2
                    continue
                characters.append("\\")
                index += 1
                continue
            expansion_end = self._consume_active_expansion(
                index,
                limit,
                depth,
                double_quoted=True,
            )
            if expansion_end is not None:
                dynamic = True
                quoted_zero_field_expansion = (
                    quoted_zero_field_expansion or expansion_end.quoted_zero_field_expansion
                )
                index = expansion_end.end
                continue
            characters.append(character)
            index += 1
        return characters, index, dynamic, quoted_zero_field_expansion

    def _consume_active_expansion(
        self,
        index: int,
        limit: int,
        depth: int,
        *,
        double_quoted: bool = False,
    ) -> _ShellExpansion | None:
        if depth > _MAX_SHELL_RECURSION_DEPTH:
            raise _ShellScanIncomplete("recursion limit exceeded")
        end: int | None = None
        quoted_zero_field_expansion = False
        if self.source.startswith("$((", index):
            end = self._consume_arithmetic(index + 3, limit, depth + 1)
            if end is None:
                # Not balanced arithmetic: Bash falls back to a command substitution whose
                # first ( opens a subshell, so scan the region for inner invocations.
                end = self._scan_commands(
                    index + 2,
                    limit,
                    terminator=")",
                    depth=depth + 1,
                )
        elif self.source.startswith("$(", index):
            end = self._scan_commands(
                index + 2,
                limit,
                terminator=")",
                depth=depth + 1,
            )
        elif self.source.startswith("${", index):
            return self._consume_parameter(
                index + 2,
                limit,
                depth + 1,
                double_quoted=double_quoted,
            )
        elif self.source.startswith("$[", index):
            end = self._consume_legacy_arithmetic(index + 2, limit, depth + 1)
        elif self.source[index] == "`":
            end = self._consume_legacy_substitution(index, limit, depth + 1)
        elif self.source[index] == "$":
            end = _consume_parameter_name(self.source, index, limit)
            quoted_zero_field_expansion = double_quoted and (
                _is_unbraced_named_parameter(self.source, index, limit)
                or (index + 1 < limit and self.source[index + 1] == "@")
            )
        if end is None:
            return None
        return _ShellExpansion(end, quoted_zero_field_expansion)

    def _consume_parameter(
        self,
        start: int,
        limit: int,
        depth: int,
        *,
        double_quoted: bool,
    ) -> _ShellExpansion:
        index = start
        braces = 1
        quote: str | None = None
        quoted_zero_field_expansion = double_quoted and _braced_parameter_may_expand_to_zero_fields(
            self.source,
            start,
            limit,
        )
        while index < limit:
            self.budget.step()
            character = self.source[index]
            quoted_character = self._consume_parameter_quoted_character(
                index,
                limit,
                quote,
                double_quoted,
            )
            if quoted_character is not None:
                index, quote = quoted_character
                continue
            expansion_end = self._consume_active_expansion(
                index,
                limit,
                depth,
                double_quoted=double_quoted,
            )
            if expansion_end is not None:
                quoted_zero_field_expansion = (
                    quoted_zero_field_expansion or expansion_end.quoted_zero_field_expansion
                )
                index = expansion_end.end
                continue
            if quote is None and not double_quoted:
                process_end = self._consume_process_substitution(index, limit, depth)
                if process_end is not None:
                    index = process_end
                    continue
            if character == "}":
                braces -= 1
                index += 1
                if braces == 0:
                    return _ShellExpansion(index, quoted_zero_field_expansion)
                continue
            index += 1
        return _ShellExpansion(index, quoted_zero_field_expansion)

    def _consume_parameter_quoted_character(
        self,
        index: int,
        limit: int,
        quote: str | None,
        double_quoted: bool,
    ) -> tuple[int, str | None] | None:
        character = self.source[index]
        if quote == "'":
            return index + 1, None if character == "'" else quote
        if quote == '"' and character == '"':
            return index + 1, None
        if character == "\\" and index + 1 < limit:
            escaped = self.source[index + 1]
            consumes_escape = (quote is None and not double_quoted) or escaped in {
                "$",
                '"',
                "\\",
                "`",
                "\n",
            }
            return index + (2 if consumes_escape else 1), quote
        if quote is None and (character == '"' or (character == "'" and not double_quoted)):
            return index + 1, character
        return None

    def _consume_arithmetic(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> int | None:
        """Consume ``(( ... ))`` arithmetic, or report a command-substitution fallback.

        Bash treats ``((`` as arithmetic only when the region closes with a balanced ``))``.
        When a base-level ``)`` appears without a paired ``)`` the leading ``(`` opened a
        subshell inside a command substitution (for example ``$((cmd) )`` or ``((cmd) )``), so
        this returns ``None`` for the caller to rescan the region as command-substitution
        content instead of silently swallowing it.

        Args:
            start: Index just past the opening ``((``.
            limit: Exclusive scan limit.
            depth: Current recursion depth for nested expansions.

        Returns:
            The index past the closing ``))``, the scan limit for an unterminated region, or
            ``None`` when Bash would fall back to a command substitution containing a subshell.
        """
        index = start
        parentheses = 1
        while index < limit:
            self.budget.step()
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                index = expansion_end.end
                continue
            character = self.source[index]
            if character == "(":
                parentheses += 1
                index += 1
                continue
            if character == ")":
                if parentheses == 1:
                    if self.source.startswith("))", index):
                        return index + 2
                    return None
                parentheses -= 1
            index += 1
        return index

    def _consume_legacy_arithmetic(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> int:
        index = start
        while index < limit:
            self.budget.step()
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                index = expansion_end.end
                continue
            if self.source[index] == "]":
                return index + 1
            index += 1
        return index

    def _consume_legacy_substitution(
        self,
        opening: int,
        limit: int,
        depth: int,
    ) -> int:
        body: list[str] = []
        index = opening + 1
        while index < limit:
            self.budget.step()
            character = self.source[index]
            if character == "`":
                child = _ShellScanner(
                    "".join(body),
                    budget=self.budget,
                    invocations=self.invocations,
                    classify_commands=self.classify_commands,
                )
                child._scan_commands(
                    0,
                    len(child.source),
                    terminator=None,
                    depth=depth,
                )
                return index + 1
            if character == "\\" and index + 1 < limit:
                escaped = self.source[index + 1]
                if escaped == "`":
                    body.append("`")
                else:
                    body.extend(("\\", escaped))
                index += 2
                continue
            body.append(character)
            index += 1
        return index

    def _command_operator_at(self, index: int, limit: int) -> str | None:
        for operator in _COMMAND_OPERATORS:
            if index + len(operator) <= limit and self.source.startswith(
                operator,
                index,
            ):
                if operator in {"{", "}"} and not self._standalone_brace_at(index, limit):
                    continue
                return operator
        return None

    def _standalone_brace_at(self, index: int, limit: int) -> bool:
        """Return whether a leading brace is a shell reserved word, not word text."""
        next_index = index + 1
        return next_index == limit or self.source[next_index] in " \t\n;&|()<>"

    def _line_end(self, index: int, limit: int) -> int:
        line_end = self.source.find("\n", index, limit)
        return limit if line_end == -1 else line_end

    def _comment_end(self, index: int, limit: int) -> int:
        """Return the newline that ends a comment.

        Bash comments run to the next newline unconditionally. A trailing backslash never
        continues a comment onto the following line, so the comment ends at the first newline,
        or at the scan limit when none remains.
        """
        return self._line_end(index, limit)


def _remove_active_line_continuations(source: str) -> str:
    """Remove unescaped continuations from a context where Bash keeps them active."""
    return re.sub(r"(?<!\\)((?:\\\\)*)\\\n", r"\1", source)


def scan_doc_lattice_invocations(script: str) -> ShellScanResult:
    """Scan literal Bash syntax and explicitly report bounded-scan exhaustion."""
    normalized = script.replace("\r\n", "\n")
    if len(normalized) > _MAX_SHELL_SOURCE_CHARS:
        return ShellScanResult((), "source character limit exceeded")

    scanner = _ShellScanner(normalized)
    try:
        invocations = scanner.scan()
    except _ShellScanIncomplete as error:
        return ShellScanResult(tuple(scanner.invocations), str(error))
    return ShellScanResult(invocations)


def direct_doc_lattice_invocations(
    script: str,
    *,
    context: str | None = None,
) -> tuple[_Invocation, ...]:
    """Return conservative direct doc-lattice commands from literal Bash syntax.

    The scanner is bounded, recursive, and non-executing. It intentionally does not resolve
    aliases, functions, variables used as executable names, ``eval``/``source``, ``sh -c`` or
    ``bash -c``, external wrapper scripts, actions, or reusable workflows.

    Args:
        script: Literal Bash source to scan.
        context: Optional caller-supplied prefix (for example a workflow path) that identifies
            the source when the scan cannot complete. When given it is prepended to the raised
            fail-closed error so the operator can locate the offending script.

    Raises:
        ConfigError: If a scanner resource bound prevents a complete result.
    """
    result = scan_doc_lattice_invocations(script)
    if result.incomplete_reason is not None:
        if context is not None:
            raise ConfigError(f"{context}: shell scan incomplete: {result.incomplete_reason}")
        raise ConfigError(f"shell scan incomplete: {result.incomplete_reason}")
    return result.invocations


def _invocation_in_simple_command(
    words: list[_ShellWord],
    budget: _ScanBudget,
) -> _Invocation | None:
    resolution = _LauncherResolutionState(budget)
    executable = _doc_lattice_command_index(words, 0, resolution)
    if executable.index is None:
        return None
    subcommand_resolution = _doc_lattice_subcommand_index(words, executable.index + 1)
    if executable.ambiguous or subcommand_resolution.ambiguous:
        raise _ShellScanIncomplete("command-position expansion cannot be scanned safely")
    if subcommand_resolution.index is None or subcommand_resolution.index >= len(words):
        return None
    subcommand_index = subcommand_resolution.index
    subcommand = words[subcommand_index]
    if subcommand.active_argv_expansion:
        # A brace- or glob-expanded subcommand (for example "linea{r,}") expands to a different
        # word at runtime, so Bash may run linear/reconcile while the unexpanded literal never
        # matches classification. The scanner cannot certify which subcommand runs, so it fails
        # closed rather than silently approving the workflow.
        raise _ShellScanIncomplete("subcommand word uses brace or glob expansion")
    if subcommand.dynamic or not subcommand.literal:
        return None
    arguments = words[subcommand_index + 1 :]
    if subcommand.literal == "linear":
        disposition = _classify_command_disposition(
            arguments,
            options_with_arguments=_LINEAR_OPTIONS_WITH_ARGUMENTS,
            flags=_LINEAR_FLAGS,
        )
    elif subcommand.literal == "reconcile":
        disposition = _classify_command_disposition(
            arguments,
            options_with_arguments=_RECONCILE_OPTIONS_WITH_ARGUMENTS,
            flags=_RECONCILE_FLAGS,
            non_mutating_options=_RECONCILE_NON_MUTATING_OPTIONS,
        )
    else:
        disposition = (
            _CommandDisposition.NON_MUTATING
            if any(
                not argument.dynamic and argument.literal == "--dry-run" for argument in arguments
            )
            else _CommandDisposition.SENSITIVE
        )
    if disposition is _CommandDisposition.NON_EXECUTING:
        return None
    return subcommand.literal, disposition is _CommandDisposition.NON_MUTATING


def _classify_command_disposition(
    arguments: list[_ShellWord],
    *,
    options_with_arguments: frozenset[str],
    flags: frozenset[str],
    non_mutating_options: frozenset[str] = frozenset(),
) -> _CommandDisposition:
    """Classify a static Typer argv prefix without executing the command.

    Known value-taking options consume their next word even when it looks like another option.
    Shell expansion or an unknown option before the effective safe option can change the runtime
    argv shape, so the scanner conservatively preserves the disposition established so far.
    """
    disposition = _CommandDisposition.SENSITIVE
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if _word_may_change_argv(argument):
            return disposition
        literal = argument.literal
        option_name, separator, _value = literal.partition("=")
        if separator and option_name in options_with_arguments:
            index += 1
            continue
        if literal == "--help":
            return _CommandDisposition.NON_EXECUTING
        if literal in non_mutating_options:
            disposition = _CommandDisposition.NON_MUTATING
            index += 1
            continue
        if literal == "--":
            return disposition
        if literal in options_with_arguments:
            value_index = index + 1
            if value_index >= len(arguments) or _word_may_change_argv(arguments[value_index]):
                return disposition
            index += 2
            continue
        if literal in flags:
            index += 1
            continue
        if literal.startswith("-"):
            return disposition
        index += 1
    return disposition


def _word_may_change_argv(word: _ShellWord) -> bool:
    """Return whether shell expansion may change one lexical word's argv shape."""
    return word.dynamic or word.active_argv_expansion


def _reject_unsafe_executable_word(word: _ShellWord) -> None:
    """Reject a runtime-translated or argv-expanded executable word."""
    if word.locale_translated:
        raise _ShellScanIncomplete("locale-translated executable cannot be scanned safely")
    if word.active_argv_expansion:
        raise _ShellScanIncomplete("executable word uses brace or glob expansion")
    if _is_dynamic_relative_doc_lattice_executable(word):
        raise _ShellScanIncomplete(
            "dynamic relative doc-lattice executable cannot be scanned safely"
        )


def _reject_unresolved_unsafe_executable(
    words: list[_ShellWord],
    start: int,
    end: int,
    *,
    ignore_shell_assignments: bool,
) -> None:
    """Reject unresolved executable grammar containing unsafe runtime provenance."""
    for word in words[start:end]:
        if ignore_shell_assignments and word.shell_assignment:
            continue
        _reject_unsafe_executable_word(word)


def _word_may_change_option_value_shape(word: _ShellWord) -> bool:
    """Return whether a static option's value can add or remove argv fields.

    Ordinary quoted scalars, command substitutions, ``$*``, and ``${array[*]}`` retain one
    field. Unquoted expansion, quoted zero-field provenance, and active brace/glob syntax do
    not, so consuming such a value can shift later grammar tokens.
    """
    return word.unquoted_dynamic or word.quoted_zero_field_expansion or word.active_argv_expansion


def _command_boundary_word_may_disappear(word: _ShellWord) -> bool:
    """Return whether a word can expose a later command-position payload.

    A dynamic word with decoded static text (for example
    ``$RUNNER_TEMP/doc-lattice-helper``) always retains that text and cannot make its successor
    executable. In contrast, an unquoted dynamic word with no decoded text can disappear after
    expansion. Quoted ``@``/``[@]`` families can do the same, as can an unbraced named parameter
    that resolves through a nameref to an array reference. Brace/glob expansion can also alter
    the number of argv words. Each form can shift a later wrapper or payload into command
    position.
    """
    return word.active_argv_expansion or (
        not word.literal and (word.unquoted_dynamic or word.quoted_zero_field_expansion)
    )


def _has_active_argv_expansion(syntax: str) -> bool:
    """Return whether unquoted word syntax can expand into a different argv shape."""
    if "*" in syntax or "?" in syntax:
        return True
    bracket_start = syntax.find("[")
    if bracket_start >= 0 and "]" in syntax[bracket_start + 1 :]:
        return True
    brace_separators: list[bool] = []
    previous_period = False
    for character in syntax:
        if character == "{":
            brace_separators.append(False)
            previous_period = False
            continue
        if character == "}":
            if brace_separators and brace_separators.pop():
                return True
            previous_period = False
            continue
        if not brace_separators:
            previous_period = False
            continue
        if character == "," or (character == "." and previous_period):
            brace_separators[-1] = True
        previous_period = character == "."
    return False


def _skip_shell_prefixes(words: list[_ShellWord], start: int) -> _ResolvedIndex:
    """Skip literal shell prefixes and preserve dynamic command-position ambiguity."""
    index = start
    ambiguous = False
    while index < len(words):
        word = words[index]
        if word.shell_assignment:
            index += 1
            continue
        if _command_boundary_word_may_disappear(word):
            ambiguous = True
            index += 1
            continue
        if word.dynamic:
            return _ResolvedIndex(index, ambiguous)
        if word.keyword_eligible and word.literal in _COMMAND_PREFIXES:
            index += 1
            continue
        if word.keyword_eligible and word.literal == "time":
            index += 1
            if (
                index < len(words)
                and not _word_may_change_argv(words[index])
                and words[index].literal == "-p"
            ):
                index += 1
            if (
                index < len(words)
                and not _word_may_change_argv(words[index])
                and words[index].literal == "--"
            ):
                index += 1
            continue
        if _basename(word.literal) == "env":
            return _ResolvedIndex(_skip_env_prefix(words, index + 1), ambiguous)
        if word.literal in {"builtin", "command", "exec"}:
            wrapper_literal = word.literal
            wrapper = _skip_shell_builtin_wrapper(words, index)
            if wrapper.index is None:
                return _ResolvedIndex(None, ambiguous or wrapper.ambiguous)
            index = wrapper.index
            ambiguous = ambiguous or wrapper.ambiguous
            if (
                wrapper_literal in {"command", "exec"}
                and index < len(words)
                and not _word_may_change_argv(words[index])
                and words[index].literal == "time"
            ):
                return _ResolvedIndex(
                    _skip_external_time_prefix(words, index + 1),
                    ambiguous,
                )
            continue
        return _ResolvedIndex(index, ambiguous)
    return _ResolvedIndex(index, ambiguous)


def _skip_shell_builtin_wrapper(words: list[_ShellWord], index: int) -> _ResolvedIndex:
    """Resolve one supported Bash wrapper beginning at ``index``."""
    literal = words[index].literal
    if literal == "builtin":
        return _skip_builtin_wrapper(words, index + 1)
    if literal == "command":
        return _skip_command_builtin(words, index + 1)
    return _skip_exec_wrapper(words, index + 1)


def _skip_builtin_wrapper(words: list[_ShellWord], start: int) -> _ResolvedIndex:
    """Expose a supported literal Bash builtin target or one ambiguous successor."""
    index = start
    if index < len(words) and not words[index].dynamic and words[index].literal == "--":
        index += 1
    if index >= len(words):
        return _ResolvedIndex(index)
    target = words[index]
    if _command_boundary_word_may_disappear(target) or target.dynamic:
        return _ResolvedIndex(index + 1, ambiguous=True)
    if target.literal not in {"builtin", "command", "exec"}:
        return _ResolvedIndex(None)
    return _ResolvedIndex(index)


def _skip_command_builtin(words: list[_ShellWord], start: int) -> _ResolvedIndex:
    """Skip ``command`` options and preserve dynamic option/executable ambiguity."""
    index = start
    ambiguous = False
    while index < len(words):
        word = words[index]
        if _command_boundary_word_may_disappear(word):
            ambiguous = True
            index += 1
            continue
        if word.dynamic:
            # A quoted scalar can still be ``-p`` or ``--`` at runtime, exposing the static
            # successor as the command. Continue along that grammar path and mark it unsafe.
            ambiguous = True
            index += 1
            continue
        if word.literal == "--":
            return _ResolvedIndex(index + 1, ambiguous)
        if not word.literal.startswith("-"):
            return _ResolvedIndex(index, ambiguous)
        if "v" in word.literal[1:] or "V" in word.literal[1:]:
            return _ResolvedIndex(len(words), ambiguous)
        index += 1
    return _ResolvedIndex(index, ambiguous)


def _skip_exec_wrapper(words: list[_ShellWord], start: int) -> _ResolvedIndex:
    """Skip ``exec`` options and preserve dynamic option/executable ambiguity."""
    index = start
    ambiguous = False
    while index < len(words):
        word = words[index]
        if _command_boundary_word_may_disappear(word):
            ambiguous = True
            index += 1
            continue
        if word.dynamic:
            # ``-c``, ``-l``, and ``--`` are valid runtime values that leave the successor in
            # executable position, so a dynamic word cannot be treated as an ordinary command.
            ambiguous = True
            index += 1
            continue
        if word.literal == "--":
            return _ResolvedIndex(index + 1, ambiguous)
        if word.literal.startswith("-"):
            if _exec_option_requires_separate_argv0(word.literal):
                value_index = index + 1
                if value_index < len(words) and _word_may_change_option_value_shape(
                    words[value_index]
                ):
                    ambiguous = True
                index += 2
            else:
                index += 1
        else:
            return _ResolvedIndex(index, ambiguous)
    return _ResolvedIndex(index, ambiguous)


def _exec_option_requires_separate_argv0(literal: str) -> bool:
    """Validate one Bash exec short cluster and locate a separate ``-a`` value."""
    if literal == "-":
        return False
    for offset, option in enumerate(literal[1:], start=1):
        if option in {"c", "l"}:
            continue
        if option == "a":
            return offset == len(literal) - 1
        raise _ShellScanIncomplete("unsupported exec option cannot be scanned safely")
    return False


def _is_env_split_string_long_option(literal: str) -> bool:
    """Return whether static text can form GNU ``env``'s split-string long option."""
    option, _separator, _value = literal.partition("=")
    return (
        option.startswith("--")
        and option != "--"
        and _ENV_SPLIT_STRING_LONG_OPTION.startswith(option)
    )


def _is_env_assignment_operand(literal: str) -> bool:
    """Return whether GNU env treats a non-option operand as an environment assignment."""
    return "=" in literal


def _is_env_split_string_short_option(literal: str) -> bool:
    """Return whether a static GNU ``env`` short-option cluster reaches ``-S``."""
    if not literal.startswith("-") or literal.startswith("--"):
        return False
    for option in literal[1:]:
        # These options consume the rest of this word as their attached argument.
        if option in {"a", "u", "C"}:
            return False
        if option == "S":
            return True
    return False


def _resolve_env_long_option(literal: str) -> tuple[str, bool]:
    """Resolve one exact or uniquely abbreviated GNU env long option."""
    option, separator, _value = literal.partition("=")
    candidates = tuple(
        candidate for candidate in _ENV_LONG_OPTION_KINDS if candidate.startswith(option)
    )
    if len(candidates) != 1:
        raise _ShellScanIncomplete("unsupported env option cannot be scanned safely")
    return candidates[0], bool(separator)


def _env_short_option_requires_separate_value(literal: str) -> bool:
    """Validate one GNU env short cluster and report whether its value is separate."""
    if literal == "-":
        return False
    for offset, option in enumerate(literal[1:], start=1):
        if option in _ENV_SHORT_FLAGS:
            continue
        if option == "S":
            raise _ShellScanIncomplete("env split-string option cannot be scanned safely")
        if option in _ENV_SHORT_REQUIRED:
            return offset == len(literal) - 1
        raise _ShellScanIncomplete("unsupported env option cannot be scanned safely")
    return False


def _skip_env_option_value(words: list[_ShellWord], option_index: int) -> int:
    """Consume one required separate GNU env option value."""
    value_index = option_index + 1
    if value_index >= len(words) or _word_may_change_argv(words[value_index]):
        raise _ShellScanIncomplete("env option value cannot be scanned safely")
    return value_index + 1


def _skip_static_env_option(words: list[_ShellWord], index: int) -> int:
    """Skip one validated static GNU env option and any required separate value."""
    literal = words[index].literal
    if not literal.startswith("--"):
        if _env_short_option_requires_separate_value(literal):
            return _skip_env_option_value(words, index)
        return index + 1
    option, attached_value = _resolve_env_long_option(literal)
    kind = _ENV_LONG_OPTION_KINDS[option]
    if kind == "split":
        raise _ShellScanIncomplete("env split-string option cannot be scanned safely")
    if kind == "stop":
        return len(words)
    if kind == "required" and not attached_value:
        return _skip_env_option_value(words, index)
    if kind == "flag" and attached_value:
        raise _ShellScanIncomplete("unsupported env option cannot be scanned safely")
    return index + 1


def _skip_env_prefix(words: list[_ShellWord], start: int) -> int:
    index = start
    options_enabled = True
    while index < len(words):
        word = words[index]
        if options_enabled and not word.dynamic and word.literal == "--":
            options_enabled = False
            index += 1
            continue
        if options_enabled and (
            _is_env_split_string_long_option(word.literal)
            or _is_env_split_string_short_option(word.literal)
        ):
            raise _ShellScanIncomplete("env split-string option cannot be scanned safely")
        if word.dynamic:
            if _is_env_assignment_operand(word.literal):
                if word.unquoted_dynamic:
                    raise _ShellScanIncomplete(
                        "unquoted dynamic env assignment cannot be scanned safely"
                    )
                raise _ShellScanIncomplete("quoted dynamic env assignment cannot be scanned safely")
            raise _ShellScanIncomplete("dynamic env prefix cannot be scanned safely")
        if _word_may_change_argv(word):
            raise _ShellScanIncomplete("expandable env prefix cannot be scanned safely")
        if options_enabled and word.literal.startswith("-"):
            index = _skip_static_env_option(words, index)
        elif _is_env_assignment_operand(word.literal):
            index += 1
        else:
            return index
    return index


def _doc_lattice_command_index(
    words: list[_ShellWord],
    start: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve one direct command, including an optional named Bash coprocess."""
    command = _skip_shell_prefixes(words, start)
    if command.index is None:
        return command
    command_index = command.index
    if (
        command_index < len(words)
        and not words[command_index].dynamic
        and words[command_index].keyword_eligible
        and words[command_index].literal == "coproc"
    ):
        payload_index = _coproc_doc_lattice_command_index(
            words,
            command_index + 1,
            resolution,
        )
    else:
        payload_index = _doc_lattice_payload_index(words, command_index, resolution)
    if payload_index.index is None:
        _reject_unresolved_unsafe_executable(
            words,
            start,
            command_index + 1,
            ignore_shell_assignments=True,
        )
    return _ResolvedIndex(payload_index.index, command.ambiguous or payload_index.ambiguous)


def _coproc_doc_lattice_command_index(
    words: list[_ShellWord],
    start: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve the unnamed command or one optional literal coprocess name."""
    unnamed = _doc_lattice_command_after_prefixes(words, start, resolution)
    if unnamed.index is not None:
        return unnamed
    if start >= len(words):
        return unnamed
    name = words[start]
    if name.dynamic or not _is_name(name.literal):
        return unnamed
    named = _doc_lattice_command_after_prefixes(words, start + 1, resolution)
    return _ResolvedIndex(named.index, unnamed.ambiguous or named.ambiguous)


def _doc_lattice_command_after_prefixes(
    words: list[_ShellWord],
    start: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Reuse normal prefix, wrapper, and payload resolution from one command start."""
    executable = _skip_shell_prefixes(words, start)
    if executable.index is None:
        return executable
    payload = _doc_lattice_payload_index(words, executable.index, resolution)
    if payload.index is None:
        _reject_unresolved_unsafe_executable(
            words,
            start,
            executable.index + 1,
            ignore_shell_assignments=True,
        )
    return _ResolvedIndex(payload.index, executable.ambiguous or payload.ambiguous)


def _doc_lattice_payload_index(
    words: list[_ShellWord],
    executable_index: int,
    resolution: _LauncherResolutionState,
    *,
    launcher_depth: int = 0,
) -> _ResolvedIndex:
    if executable_index >= len(words):
        return _ResolvedIndex(None)
    executable_word = words[executable_index]
    _reject_unsafe_executable_word(executable_word)
    if _is_doc_lattice_executable(executable_word):
        return _ResolvedIndex(executable_index)
    if not executable_word.dynamic:
        executable = _basename(executable_word.literal)
        if executable in {"env", "time"}:
            return _nested_launcher_payload_index(
                words,
                _ResolvedIndex(executable_index),
                strip_version=False,
                launcher_depth=launcher_depth,
                resolution=resolution,
            )
        if executable == "uvx":
            return _uvx_payload_index(
                words,
                executable_index + 1,
                launcher_depth=launcher_depth,
                resolution=resolution,
            )
        if executable == "uv":
            return _uv_payload_index(
                words,
                executable_index + 1,
                launcher_depth=launcher_depth,
                resolution=resolution,
            )
    return _ResolvedIndex(None)


def _uvx_payload_index(
    words: list[_ShellWord],
    start: int,
    *,
    launcher_depth: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve a ``uvx [options] doc-lattice`` payload, tolerating an ``@spec`` suffix."""
    cache_key = ("uvx", start, launcher_depth)
    cached = resolution.cache.get(cache_key)
    if cached is not None:
        return cached
    resolution.step()
    payload = _launcher_payload_index(
        words,
        start,
        _LauncherPayloadRequest(
            _UVX_LAUNCHER,
            strip_version=True,
            inherited_ambiguity=False,
            fail_on_unknown=True,
            launcher_depth=launcher_depth,
        ),
        resolution,
    )
    resolution.cache[cache_key] = payload
    return payload


def _uv_payload_index(
    words: list[_ShellWord],
    start: int,
    *,
    launcher_depth: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve and memoize one ``uv`` grammar state."""
    cache_key = ("uv", start, launcher_depth)
    cached = resolution.cache.get(cache_key)
    if cached is not None:
        return cached
    resolution.step()
    payload = _resolve_uv_payload_index(
        words,
        start,
        launcher_depth=launcher_depth,
        resolution=resolution,
    )
    resolution.cache[cache_key] = payload
    return payload


def _resolve_uv_payload_index(
    words: list[_ShellWord],
    start: int,
    *,
    launcher_depth: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve ``uv`` launcher payloads for ``run`` and the ``tool run`` (uvx) long form.

    Global flags that precede the subcommand are skipped. Dynamic grammar words are followed
    speculatively only when a static launcher payload remains reachable; that path is marked
    ambiguous for the caller to fail closed.

    Raises:
        _ShellScanIncomplete: If an unresolvable option-like word precedes the subcommand.
    """
    subcommand_resolution = _skip_uv_global_options(words, start, resolution)
    for launcher_start in subcommand_resolution.launcher_starts:
        dynamic_launcher = _uv_dynamic_launcher_payload_index(
            words,
            launcher_start,
            launcher_depth=launcher_depth,
            resolution=resolution,
        )
        if dynamic_launcher.index is not None:
            return dynamic_launcher
    if subcommand_resolution.unresolved_option:
        raise _ShellScanIncomplete("unresolved uv global option")
    if subcommand_resolution.index is None or subcommand_resolution.index >= len(words):
        return _ResolvedIndex(None, subcommand_resolution.ambiguous)
    subcommand_index = subcommand_resolution.index
    subcommand = words[subcommand_index]
    if subcommand.dynamic or subcommand.active_argv_expansion:
        return _ResolvedIndex(None, subcommand_resolution.ambiguous)
    if subcommand.literal == "run":
        return _uv_run_payload_index(
            words,
            subcommand_index + 1,
            _LauncherPayloadRequest(
                _UV_RUN_LAUNCHER,
                strip_version=False,
                inherited_ambiguity=subcommand_resolution.ambiguous,
                fail_on_unknown=True,
                launcher_depth=launcher_depth,
            ),
            resolution,
        )
    if subcommand.literal == "tool":
        return _uv_tool_payload_index(
            words,
            subcommand_index + 1,
            _LauncherPayloadRequest(
                _UV_TOOL_RUN_LAUNCHER,
                strip_version=True,
                inherited_ambiguity=subcommand_resolution.ambiguous,
                fail_on_unknown=True,
                launcher_depth=launcher_depth,
            ),
            resolution,
        )
    return _ResolvedIndex(None, subcommand_resolution.ambiguous)


def _uv_run_payload_index(
    words: list[_ShellWord],
    start: int,
    request: _LauncherPayloadRequest,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve a ``uv run`` payload and retain ambiguity inherited from its subcommand."""
    return _launcher_payload_index(words, start, request, resolution)


def _uv_tool_payload_index(
    words: list[_ShellWord],
    run_index: int,
    request: _LauncherPayloadRequest,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve the ``run`` portion of ``uv tool run`` and retain dynamic-token ambiguity."""
    if run_index >= len(words):
        return _ResolvedIndex(None, request.inherited_ambiguity)
    run = words[run_index]
    if run.active_argv_expansion:
        raise _ShellScanIncomplete("uv command word uses brace or glob expansion")
    dynamic_run = run.dynamic
    if not dynamic_run and run.literal != "run":
        return _ResolvedIndex(None, request.inherited_ambiguity)
    return _launcher_payload_index(
        words,
        run_index + 1,
        _LauncherPayloadRequest(
            request.options,
            strip_version=request.strip_version,
            inherited_ambiguity=request.inherited_ambiguity or dynamic_run,
            fail_on_unknown=request.fail_on_unknown,
            launcher_depth=request.launcher_depth,
        ),
        resolution,
    )


def _uv_dynamic_launcher_payload_index(
    words: list[_ShellWord],
    start: int,
    *,
    launcher_depth: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Try launcher grammars a dynamic uv token could have supplied before ``start``.

    A shape-changing global option value can emit both its own value and ``run`` or ``tool run``.
    Likewise, a dynamic global token can be a subcommand. The remaining static words are parsed
    with each supported launcher grammar, but are marked ambiguous so a reachable payload fails
    closed rather than being classified as a trusted literal invocation.
    """
    cache_key = ("dynamic", start, launcher_depth)
    cached = resolution.cache.get(cache_key)
    if cached is not None:
        return cached
    resolution.step()
    candidate = _uv_run_payload_index(
        words,
        start,
        _LauncherPayloadRequest(
            _UV_RUN_LAUNCHER,
            strip_version=False,
            inherited_ambiguity=True,
            fail_on_unknown=False,
            launcher_depth=launcher_depth,
        ),
        resolution,
    )
    if candidate.index is None:
        candidate = _uv_tool_payload_index(
            words,
            start,
            _LauncherPayloadRequest(
                _UV_TOOL_RUN_LAUNCHER,
                strip_version=True,
                inherited_ambiguity=True,
                fail_on_unknown=False,
                launcher_depth=launcher_depth,
            ),
            resolution,
        )
    if candidate.index is None:
        candidate = _launcher_payload_index(
            words,
            start,
            _LauncherPayloadRequest(
                _UVX_LAUNCHER,
                strip_version=True,
                inherited_ambiguity=True,
                fail_on_unknown=False,
                launcher_depth=launcher_depth,
            ),
            resolution,
        )
    result = candidate if candidate.index is not None else _ResolvedIndex(None, True)
    resolution.cache[cache_key] = result
    return result


def _launcher_payload_index(
    words: list[_ShellWord],
    start: int,
    request: _LauncherPayloadRequest,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve a selected launcher's static payload, including executable prefix chains."""
    resolution.step()
    option_resolution = _skip_options(
        words,
        start,
        request.options,
        fail_on_unknown=request.fail_on_unknown,
        resolution=resolution,
    )
    payload = _nested_launcher_payload_index(
        words,
        option_resolution,
        strip_version=request.strip_version,
        launcher_depth=request.launcher_depth,
        resolution=resolution,
    )
    if payload.index is None:
        expansion_end = (
            option_resolution.index + 1 if option_resolution.index is not None else len(words)
        )
        _reject_unresolved_unsafe_executable(
            words,
            start,
            expansion_end,
            ignore_shell_assignments=False,
        )
    return _ResolvedIndex(payload.index, request.inherited_ambiguity or payload.ambiguous)


def _nested_launcher_payload_index(
    words: list[_ShellWord],
    payload_resolution: _ResolvedIndex,
    *,
    strip_version: bool,
    launcher_depth: int,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Resolve real executable chains after a uv launcher without assuming shell builtins.

    ``uv`` executes an argv payload directly, so Bash-only words such as ``command`` and
    ``exec`` are not wrappers here. ``env`` is an executable prefix, however, and nested
    ``uv``/``uvx`` launchers are also executable commands; those are resolved recursively.
    """
    resolution.step()
    payload_index = payload_resolution.index
    if payload_index is None or payload_index >= len(words):
        return payload_resolution
    payload = words[payload_index]
    _reject_unsafe_executable_word(payload)
    if payload.dynamic:
        return _ResolvedIndex(None, payload_resolution.ambiguous)
    basename = _basename(payload.literal)
    is_doc_lattice = (
        _is_doc_lattice_uv_tool_payload(payload.literal)
        if strip_version
        else _is_doc_lattice_executable_basename(basename)
    )
    if is_doc_lattice:
        return _ResolvedIndex(payload_index, payload_resolution.ambiguous)
    if launcher_depth >= _MAX_LAUNCHER_NESTING_DEPTH:
        raise _ShellScanIncomplete("launcher nesting limit exceeded")
    if basename == "env":
        nested_start = _skip_env_prefix(words, payload_index + 1)
        nested = _nested_launcher_payload_index(
            words,
            _ResolvedIndex(nested_start),
            strip_version=False,
            launcher_depth=launcher_depth + 1,
            resolution=resolution,
        )
    elif basename == "time":
        nested_start = _skip_external_time_prefix(words, payload_index + 1)
        nested = _nested_launcher_payload_index(
            words,
            _ResolvedIndex(nested_start),
            strip_version=False,
            launcher_depth=launcher_depth + 1,
            resolution=resolution,
        )
    elif basename == "uv":
        nested = _uv_payload_index(
            words,
            payload_index + 1,
            launcher_depth=launcher_depth + 1,
            resolution=resolution,
        )
    elif basename == "uvx":
        nested = _uvx_payload_index(
            words,
            payload_index + 1,
            launcher_depth=launcher_depth + 1,
            resolution=resolution,
        )
    else:
        return _ResolvedIndex(None, payload_resolution.ambiguous)
    return _ResolvedIndex(nested.index, payload_resolution.ambiguous or nested.ambiguous)


def _skip_external_time_prefix(words: list[_ShellWord], start: int) -> int:
    """Skip the externally executed ``time`` command's safe, known prefix grammar.

    This is intentionally distinct from Bash's ``time`` keyword. ``uv`` executes the payload
    directly, so a basename of ``time`` invokes an external program such as GNU time. The
    portable ``-p`` flag and ``--`` terminator preserve a known command position; other dynamic
    or option-like forms are rejected rather than silently hiding a static payload.
    """
    index = start
    while index < len(words):
        word = words[index]
        if _word_may_change_argv(word):
            raise _ShellScanIncomplete("dynamic external time prefix cannot be scanned safely")
        if word.literal == "--":
            return index + 1
        if word.literal == "-p":
            index += 1
            continue
        if word.literal.startswith("-"):
            raise _ShellScanIncomplete("external time option cannot be scanned safely")
        return index
    return index


def _dynamic_uv_global_word_result(
    word: _ShellWord,
    index: int,
) -> tuple[int, int | None, bool] | None:
    """Return the next index, injected-launcher start, and unresolved-option state for one word."""
    if word.active_argv_expansion:
        raise _ShellScanIncomplete("uv command word uses brace or glob expansion")
    option_name = word.literal.split("=", 1)[0]
    if word.dynamic and "=" in word.literal and option_name in _UV_GLOBAL_OPTIONS_WITH_ARGUMENTS:
        candidate_start = index + 1 if _word_may_change_option_value_shape(word) else None
        return index + 1, candidate_start, False
    if word.dynamic:
        if word.literal.startswith("-"):
            return index + 1, None, True
        return index + 1, index + 1, False
    return None


def _static_uv_global_option_result(
    words: list[_ShellWord],
    index: int,
    word: _ShellWord,
) -> tuple[int, int | None] | None:
    """Return the next index and any injected-launcher start for one known global option."""
    option_name = word.literal.split("=", 1)[0]
    if option_name in _UV_GLOBAL_OPTIONS_WITH_ARGUMENTS:
        if "=" in word.literal:
            candidate_start = index + 1 if _word_may_change_option_value_shape(word) else None
            return index + 1, candidate_start
        value_index = index + 1
        candidate_start = None
        if value_index < len(words) and _word_may_change_option_value_shape(words[value_index]):
            candidate_start = value_index + 1
        return index + 2, candidate_start
    if word.literal in _UV_GLOBAL_STOP_OPTIONS:
        return len(words), None
    if word.literal in _UV_GLOBAL_FLAGS:
        return index + 1, None
    return None


def _unresolved_uv_global_option(
    *,
    ambiguous: bool,
    launcher_starts: list[int],
) -> _UvGlobalResolution:
    """Defer an option error only when a prior dynamic grammar path may still be valid."""
    if launcher_starts:
        return _UvGlobalResolution(
            None,
            ambiguous,
            tuple(launcher_starts),
            unresolved_option=True,
        )
    raise _ShellScanIncomplete("unresolved uv global option")


def _skip_uv_global_options(
    words: list[_ShellWord],
    start: int,
    resolution: _LauncherResolutionState,
) -> _UvGlobalResolution:
    """Skip uv global flags and retain starts where dynamic syntax may have injected a launcher."""
    index = start
    ambiguous = False
    launcher_starts: list[int] = []

    def add_launcher_start(candidate_start: int) -> None:
        nonlocal ambiguous
        ambiguous = True
        if candidate_start not in launcher_starts:
            launcher_starts.append(candidate_start)

    while index < len(words):
        resolution.step()
        word = words[index]
        dynamic_result = _dynamic_uv_global_word_result(word, index)
        if dynamic_result is not None:
            next_index, candidate_start, unresolved_option = dynamic_result
            if candidate_start is not None:
                add_launcher_start(candidate_start)
            if unresolved_option:
                return _unresolved_uv_global_option(
                    ambiguous=ambiguous,
                    launcher_starts=launcher_starts,
                )
            index = next_index
            continue
        if not word.literal.startswith("-"):
            return _UvGlobalResolution(index, ambiguous, tuple(launcher_starts))
        static_result = _static_uv_global_option_result(words, index, word)
        if static_result is None:
            return _unresolved_uv_global_option(
                ambiguous=ambiguous,
                launcher_starts=launcher_starts,
            )
        index, candidate_start = static_result
        if candidate_start is not None:
            add_launcher_start(candidate_start)
    return _UvGlobalResolution(None, ambiguous, tuple(launcher_starts))


def _doc_lattice_subcommand_index(
    words: list[_ShellWord],
    start: int,
) -> _ResolvedIndex:
    """Skip known root options that can precede a doc-lattice subcommand, failing closed.

    Raises:
        _ShellScanIncomplete: If an unknown static root option precedes the subcommand, since a
            future root option that consumes its successor could otherwise hide an invocation.
    """
    index = start
    ambiguous = False
    while index < len(words):
        word = words[index]
        if word.active_argv_expansion:
            # Preserve the established dedicated error for an expanded candidate subcommand.
            # It is raised by ``_invocation_in_simple_command`` after this index is returned.
            return _ResolvedIndex(index, ambiguous)
        if word.dynamic:
            # A dynamic word can be a supported root flag or option terminator, exposing the
            # static successor as a subcommand. Its concrete value is not safely knowable.
            ambiguous = True
            index += 1
            continue
        if word.literal in _DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS:
            return _ResolvedIndex(None, ambiguous)
        if word.literal in _DOC_LATTICE_ROOT_OPTIONS:
            index += 1
            continue
        if word.literal.startswith("-"):
            raise _ShellScanIncomplete("unresolved doc-lattice root option")
        return _ResolvedIndex(index, ambiguous)
    return _ResolvedIndex(index, ambiguous)


def _has_attached_short_value(literal: str, short_options: tuple[str, ...]) -> bool:
    """Return whether ``literal`` is a known short option carrying an attached value."""
    return any(literal.startswith(option) and literal != option for option in short_options)


def _has_clustered_short_flags(literal: str, flags: frozenset[str]) -> bool:
    """Return whether every member of a short-option cluster is a known flag."""
    cluster = literal.removeprefix("-")
    return (
        len(cluster) > 1
        and literal.startswith("-")
        and not literal.startswith("--")
        and all(f"-{option}" in flags for option in cluster)
    )


def _unresolved_uv_launcher_option(
    *,
    fail_on_unknown: bool,
    ambiguous: bool,
) -> _ResolvedIndex:
    """Raise in strict mode or abandon one speculative launcher grammar path."""
    if fail_on_unknown:
        raise _ShellScanIncomplete("unresolved uv launcher option")
    return _ResolvedIndex(None, ambiguous)


def _skip_options(
    words: list[_ShellWord],
    start: int,
    options: _LauncherOptions,
    *,
    fail_on_unknown: bool = True,
    resolution: _LauncherResolutionState,
) -> _ResolvedIndex:
    """Skip a uv launcher's options to its payload word, retaining dynamic ambiguity.

    Raises:
        _ShellScanIncomplete: If a static option-like word is neither a known valueless flag nor
            a known option with an argument, since silently skipping it could hide an invocation.
    """
    index = start
    ambiguous = False
    while index < len(words):
        resolution.step()
        word = words[index]
        if word.dynamic:
            if word.literal.startswith("-"):
                return _unresolved_uv_launcher_option(
                    fail_on_unknown=fail_on_unknown,
                    ambiguous=ambiguous,
                )
            # This can be a known flag at runtime, leaving the static successor as payload.
            ambiguous = True
            index += 1
            continue
        if word.active_argv_expansion:
            ambiguous = True
            index += 1
            continue
        literal = word.literal
        if literal == "--":
            return _ResolvedIndex(index + 1, ambiguous)
        option_name = literal.split("=", 1)[0]
        if option_name in options.non_command_options or _has_attached_short_value(
            literal, options.short_non_command_options
        ):
            return _ResolvedIndex(None, ambiguous)
        if option_name in options.options_with_arguments:
            if "=" in literal:
                index += 1
                continue
            value_index = index + 1
            if value_index < len(words) and _word_may_change_option_value_shape(words[value_index]):
                ambiguous = True
            index += 2
        elif (
            option_name in options.flags
            or _has_clustered_short_flags(literal, options.flags)
            or _has_attached_short_value(literal, options.short_options_with_arguments)
        ):
            index += 1
        elif literal.startswith("-"):
            return _unresolved_uv_launcher_option(
                fail_on_unknown=fail_on_unknown,
                ambiguous=ambiguous,
            )
        else:
            return _ResolvedIndex(index, ambiguous)
    return _ResolvedIndex(index, ambiguous)


def _read_simple_quoted_segment(
    source: str,
    start: int,
    limit: int,
    quote: str,
) -> tuple[str, int, bool]:
    characters: list[str] = []
    index = start + 1
    while index < limit:
        character = source[index]
        if character == quote:
            return "".join(characters), index + 1, True
        if quote == '"' and character == "\\" and index + 1 < limit:
            escaped = source[index + 1]
            if escaped == "\n":
                index += 2
                continue
            if escaped in {"$", '"', "\\", "`"}:
                characters.append(escaped)
                index += 2
                continue
        characters.append(character)
        index += 1
    return "".join(characters), index, False


def _read_ansi_c_quoted_segment(
    source: str,
    start: int,
    limit: int,
) -> tuple[str, int, bool]:
    characters: list[str] = []
    index = start + 2
    while index < limit:
        character = source[index]
        if character == "'":
            return "".join(characters), index + 1, True
        if character != "\\":
            characters.append(character)
            index += 1
            continue
        escaped, index = _read_ansi_c_escape(source, index + 1, limit)
        characters.append(escaped)
    return "".join(characters), index, False


def _read_ansi_c_escape(
    source: str,
    start: int,
    limit: int,
) -> tuple[str, int]:
    if start >= limit:
        return "\\", start
    character = source[start]
    if character in _ANSI_C_SIMPLE_ESCAPES:
        result = (_ANSI_C_SIMPLE_ESCAPES[character], start + 1)
    elif character in "01234567":
        value, end = _read_ansi_c_digits(source, start, limit, _OCTAL_BASE, 3)
        value &= _ANSI_C_OCTAL_BYTE_MASK
        result = (_valid_ansi_c_character(value, source[start:end]), end)
    elif character == "x":
        result = _read_ansi_c_prefixed_escape(source, start, limit, 16, 2)
    elif character == "u":
        result = _read_ansi_c_prefixed_escape(source, start, limit, 16, 4)
    elif character == "U":
        result = _read_ansi_c_prefixed_escape(source, start, limit, 16, 8)
    elif character == "c" and start + 1 < limit:
        controlled = source[start + 1]
        value = 127 if controlled == "?" else ord(controlled.upper()) & 0x1F
        result = (_valid_ansi_c_character(value, source[start : start + 2]), start + 2)
    else:
        result = (f"\\{character}", start + 1)
    return result


def _read_ansi_c_prefixed_escape(
    source: str,
    prefix_index: int,
    limit: int,
    base: int,
    digit_limit: int,
) -> tuple[str, int]:
    value, end = _read_ansi_c_digits(
        source,
        prefix_index + 1,
        limit,
        base,
        digit_limit,
    )
    if end == prefix_index + 1:
        return f"\\{source[prefix_index]}", end
    return _valid_ansi_c_character(value, source[prefix_index:end]), end


def _read_ansi_c_digits(
    source: str,
    start: int,
    limit: int,
    base: int,
    digit_limit: int,
) -> tuple[int, int]:
    valid = "01234567" if base == _OCTAL_BASE else "0123456789abcdefABCDEF"
    index = start
    while index < limit and index - start < digit_limit and source[index] in valid:
        index += 1
    value = int(source[start:index], base) if index != start else 0
    return value, index


def _valid_ansi_c_character(value: int, source: str) -> str:
    if value == 0:
        raise _ShellScanIncomplete("ANSI-C quoted word decodes to NUL")
    if value > _UNICODE_MAX or _SURROGATE_MIN <= value <= _SURROGATE_MAX:
        return f"\\{source}"
    return chr(value)


def _consume_parameter_name(source: str, start: int, limit: int) -> int:
    return (
        _parameter_name_end(source, start + 1, limit)
        if _is_unbraced_named_parameter(
            source,
            start,
            limit,
        )
        else min(start + 2, limit)
    )


def _is_unbraced_named_parameter(source: str, start: int, limit: int) -> bool:
    """Return whether ``$`` at ``start`` begins an unbraced variable-name expansion."""
    name_start = start + 1
    return name_start < limit and (source[name_start].isalpha() or source[name_start] == "_")


def _parameter_name_end(source: str, start: int, limit: int) -> int:
    """Return the exclusive end of a shell variable name beginning at ``start``."""
    index = start
    if index >= limit or not (source[index].isalpha() or source[index] == "_"):
        return index
    index += 1
    while index < limit and (source[index].isalnum() or source[index] == "_"):
        index += 1
    return index


def _braced_parameter_may_expand_to_zero_fields(source: str, start: int, limit: int) -> bool:
    """Recognize quoted braced parameter forms that Bash can expand to zero argv fields.

    In double quotes, ``$@`` and array ``[@]`` expansions preserve one field per expanded item,
    including zero fields for an empty parameter/array set. Named expansions such as
    ``${!prefix@}`` have the same property. Ordinary braced scalar references and ``[*]`` forms
    retain a single empty field instead, so they deliberately stay out of this provenance bit.
    """
    if start >= limit:
        return False
    if source[start] == "@":
        return True

    indirect = source[start] == "!"
    index = start + 1 if indirect else start
    if indirect and index < limit and source[index] == "@":
        return True
    name_end = _parameter_name_end(source, index, limit)
    if name_end == index:
        return False
    if indirect:
        return source.startswith("@", name_end) or source.startswith("[@]", name_end)
    return source.startswith("[@]", name_end)


def _is_name(value: str) -> bool:
    return (
        bool(value)
        and (value[0].isalpha() or value[0] == "_")
        and all(character.isalnum() or character == "_" for character in value[1:])
    )


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _is_doc_lattice_executable_basename(value: str) -> bool:
    return value.casefold() in ("doc-lattice", "doc-lattice.exe")


def _is_doc_lattice_uv_tool_payload(value: str) -> bool:
    """Return whether a uv tool payload names the doc-lattice executable or distribution."""
    value = value.lstrip()
    executable_name = _basename(value).split("@", 1)[0]
    if _is_doc_lattice_executable_basename(executable_name):
        return True
    match = _PYTHON_DISTRIBUTION_NAME_RE.match(value)
    if match is None:
        return False
    normalized_name = _PYTHON_DISTRIBUTION_SEPARATOR_RE.sub("-", match.group()).casefold()
    if normalized_name != "doc-lattice":
        return False
    suffix = value[match.end() :]
    if not suffix:
        return True
    suffix = suffix.lstrip()
    return not suffix or suffix[0] in _UV_REQUIREMENT_SUFFIX_STARTS


def _is_doc_lattice_executable(word: _ShellWord) -> bool:
    if not _is_doc_lattice_executable_basename(_basename(word.literal)):
        return False
    return not word.dynamic or word.literal.startswith("/")


def _is_dynamic_relative_doc_lattice_executable(word: _ShellWord) -> bool:
    """Return whether a dynamic relative path can name the doc-lattice executable."""
    return (
        word.dynamic
        and not word.literal.startswith("/")
        and "/" in word.literal
        and _is_doc_lattice_executable_basename(_basename(word.literal))
    )
