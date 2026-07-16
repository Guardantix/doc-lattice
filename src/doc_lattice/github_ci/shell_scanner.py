"""Bounded non-executing scanner for direct doc-lattice shell invocations."""

import re
from dataclasses import dataclass

from doc_lattice.error_types import ConfigError

_Invocation = tuple[str, bool]
_MAX_SHELL_SOURCE_CHARS = 1_048_576
_MAX_SHELL_SCAN_STEPS = 4_194_304
_MAX_SHELL_RECURSION_DEPTH = 64
_MAX_SHELL_INVOCATIONS = 10_000
_OCTAL_BASE = 8
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
_SHELL_ASSIGNMENT_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\+=|=).*",
    re.DOTALL,
)
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
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
_WORD_BREAKS = frozenset(" \t\r\n;&|()<>")

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
_UV_RUN_OPTIONS_WITH_ARGUMENTS = (
    frozenset(
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
        }
    )
    | _UV_SHARED_OPTIONS_WITH_ARGUMENTS
    | frozenset(
        {
            "--with",
            "--with-editable",
            "-w",
        }
    )
)
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
_DOC_LATTICE_ROOT_OPTIONS = frozenset({"--no-color"})
_DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS = frozenset({"--version"})


@dataclass(frozen=True, slots=True)
class _ShellWord:
    literal: str
    dynamic: bool = False


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


class _ShellScanIncomplete(RuntimeError):
    """A declared scanner resource bound prevented a complete result."""


@dataclass(slots=True)
class _ScanBudget:
    remaining_steps: int = _MAX_SHELL_SCAN_STEPS

    def step(self, amount: int = 1) -> bool:
        if amount < 0:
            raise ValueError("shell scan step amount cannot be negative")
        if self.remaining_steps < amount:
            self.remaining_steps = 0
            raise _ShellScanIncomplete("step limit exceeded")
        self.remaining_steps -= amount
        return True


class _ShellScanner:
    def __init__(
        self,
        source: str,
        *,
        budget: _ScanBudget | None = None,
        invocations: list[_Invocation] | None = None,
    ) -> None:
        self.source = source
        self.budget = budget if budget is not None else _ScanBudget()
        self.invocations = invocations if invocations is not None else []

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
        while index < limit and self.budget.step():
            character = self.source[index]
            if character == ")" and self._consume_case_pattern_close(state):
                index += 1
                continue
            if terminator is not None and character == terminator:
                self._flush_command(state.words)
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
                self._flush_command(state.words)
                index = self._consume_arithmetic(index + 2, limit, depth + 1)
                continue
            process_end = self._consume_process_substitution(index, limit, depth)
            if process_end is not None:
                state.words.append(_ShellWord("", dynamic=True))
                index = process_end
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
        self._flush_command(state.words)
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
        self._flush_command(state.words)
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
        command_position = _skip_shell_prefixes(state.words, 0) == len(state.words)
        if not word.dynamic and command_position and word.literal == "case":
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
        state.words.append(word)

    def _consume_case_pattern_close(self, state: _CommandScanState) -> bool:
        if not state.cases or state.cases[-1].phase != "pattern":
            return False
        case = state.cases[-1]
        if case.pattern_parentheses:
            case.pattern_parentheses -= 1
        else:
            state.words.clear()
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
        character = self.source[index]
        if character in " \t\r":
            return index + 1
        if character == "#":
            return self._line_end(index, limit)
        if character != "\n":
            return None
        self._flush_command(state.words)
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

    def _flush_command(self, words: list[_ShellWord]) -> None:
        if not words:
            words.clear()
            return
        invocation = _invocation_in_simple_command(words)
        if invocation is not None:
            if len(self.invocations) >= _MAX_SHELL_INVOCATIONS:
                raise _ShellScanIncomplete("invocation limit exceeded")
            self.invocations.append(invocation)
        words.clear()

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
            delimiter, quoted, index = self._parse_heredoc_delimiter(index, limit)
            if not delimiter:
                return index, None
            return (
                index,
                _Heredoc(
                    delimiter=delimiter,
                    strip_tabs=operator == "<<-",
                    expand=not quoted,
                ),
            )
        process_end = self._consume_process_substitution(index, limit, depth)
        if process_end is not None:
            return process_end, None
        _target, index = self._parse_word(index, limit, depth)
        return index, None

    def _parse_heredoc_delimiter(
        self,
        start: int,
        limit: int,
    ) -> tuple[str, bool, int]:
        characters: list[str] = []
        quoted = False
        index = start
        while index < limit and self.source[index] not in _WORD_BREAKS:
            if self.source.startswith("$'", index):
                segment, index, closed = _read_ansi_c_quoted_segment(
                    self.source,
                    index,
                    limit,
                )
                if not closed:
                    return "", True, index
                characters.extend(segment)
                quoted = True
                continue
            if self.source.startswith('$"', index):
                segment, index, closed = _read_simple_quoted_segment(
                    self.source,
                    index + 1,
                    limit,
                    '"',
                )
                if not closed:
                    return "", True, index
                characters.extend(segment)
                quoted = True
                continue
            character = self.source[index]
            if character in {"'", '"'}:
                segment, index, closed = _read_simple_quoted_segment(
                    self.source,
                    index,
                    limit,
                    character,
                )
                if not closed:
                    return "", True, index
                characters.extend(segment)
                quoted = True
                continue
            if character == "\\" and index + 1 < limit:
                characters.append(self.source[index + 1])
                quoted = True
                index += 2
                continue
            characters.append(character)
            index += 1
        return "".join(characters), quoted, index

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
            while index <= limit and self.budget.step():
                line_end = self._line_end(index, limit)
                candidate = self.source[index:line_end]
                if heredoc.strip_tabs:
                    candidate = candidate.lstrip("\t")
                if candidate == heredoc.delimiter:
                    body_end = index
                    after_delimiter = (
                        line_end + 1
                        if line_end < limit and self.source[line_end] == "\n"
                        else line_end
                    )
                    break
                index = (
                    line_end + 1
                    if line_end < limit and self.source[line_end] == "\n"
                    else limit + 1
                )
            if heredoc.expand:
                self._scan_heredoc_expansions(body_start, body_end, depth + 1)
            index = after_delimiter
        return min(index, limit)

    def _scan_heredoc_expansions(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> None:
        index = start
        while index < limit and self.budget.step():
            if self.source[index] == "\\":
                index = min(index + 2, limit)
                continue
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                index = expansion_end
                continue
            index += 1

    def _parse_word(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> tuple[_ShellWord, int]:
        characters: list[str] = []
        dynamic = False
        index = start
        while index < limit and self.source[index] not in _WORD_BREAKS:
            if not self.budget.step():
                break
            if self.source.startswith("$'", index):
                segment, index, _closed = _read_ansi_c_quoted_segment(
                    self.source,
                    index,
                    limit,
                )
                characters.extend(segment)
                continue
            if self.source.startswith('$"', index):
                segment, index, fragment_dynamic = self._parse_double_quoted(
                    index + 2,
                    limit,
                    depth,
                )
                characters.extend(segment)
                dynamic = dynamic or fragment_dynamic
                continue
            character = self.source[index]
            if character == "'":
                closing = self.source.find("'", index + 1, limit)
                if closing == -1:
                    characters.append(self.source[index + 1 : limit])
                    return _ShellWord("".join(characters), dynamic), limit
                characters.append(self.source[index + 1 : closing])
                index = closing + 1
                continue
            if character == '"':
                segment, index, fragment_dynamic = self._parse_double_quoted(
                    index + 1,
                    limit,
                    depth,
                )
                characters.extend(segment)
                dynamic = dynamic or fragment_dynamic
                continue
            if character == "\\":
                if index + 1 < limit:
                    characters.append(self.source[index + 1])
                    index += 2
                else:
                    index += 1
                continue
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                dynamic = True
                index = expansion_end
                continue
            process_end = self._consume_process_substitution(index, limit, depth)
            if process_end is not None:
                dynamic = True
                index = process_end
                continue
            characters.append(character)
            index += 1
        return _ShellWord("".join(characters), dynamic), index

    def _parse_double_quoted(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> tuple[list[str], int, bool]:
        characters: list[str] = []
        dynamic = False
        index = start
        while index < limit and self.budget.step():
            character = self.source[index]
            if character == '"':
                return characters, index + 1, dynamic
            if character == "\\" and index + 1 < limit:
                escaped = self.source[index + 1]
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
                index = expansion_end
                continue
            characters.append(character)
            index += 1
        return characters, index, dynamic

    def _consume_active_expansion(
        self,
        index: int,
        limit: int,
        depth: int,
        *,
        double_quoted: bool = False,
    ) -> int | None:
        if depth > _MAX_SHELL_RECURSION_DEPTH:
            raise _ShellScanIncomplete("recursion limit exceeded")
        end: int | None = None
        if self.source.startswith("$((", index):
            end = self._consume_arithmetic(index + 3, limit, depth + 1)
        elif self.source.startswith("$(", index):
            end = self._scan_commands(
                index + 2,
                limit,
                terminator=")",
                depth=depth + 1,
            )
        elif self.source.startswith("${", index):
            end = self._consume_parameter(
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
        return end

    def _consume_parameter(
        self,
        start: int,
        limit: int,
        depth: int,
        *,
        double_quoted: bool,
    ) -> int:
        index = start
        braces = 1
        quote: str | None = None
        while index < limit and self.budget.step():
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
            if self.source.startswith("${", index):
                braces += 1
                index += 2
                continue
            expansion_end = self._consume_active_expansion(
                index,
                limit,
                depth,
                double_quoted=double_quoted,
            )
            if expansion_end is not None:
                index = expansion_end
                continue
            if character == "}":
                braces -= 1
                index += 1
                if braces == 0:
                    return index
                continue
            index += 1
        return index

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
    ) -> int:
        index = start
        parentheses = 1
        while index < limit and self.budget.step():
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                index = expansion_end
                continue
            character = self.source[index]
            if character == "(":
                parentheses += 1
                index += 1
                continue
            if character == ")":
                if parentheses == 1 and self.source.startswith("))", index):
                    return index + 2
                parentheses = max(1, parentheses - 1)
            index += 1
        return index

    def _consume_legacy_arithmetic(
        self,
        start: int,
        limit: int,
        depth: int,
    ) -> int:
        index = start
        while index < limit and self.budget.step():
            expansion_end = self._consume_active_expansion(index, limit, depth)
            if expansion_end is not None:
                index = expansion_end
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
        while index < limit and self.budget.step():
            character = self.source[index]
            if character == "`":
                child = _ShellScanner(
                    "".join(body),
                    budget=self.budget,
                    invocations=self.invocations,
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
        return next_index == limit or self.source[next_index] in " \t\r\n;&|()<>"

    def _line_end(self, index: int, limit: int) -> int:
        line_end = self.source.find("\n", index, limit)
        return limit if line_end == -1 else line_end


def scan_doc_lattice_invocations(script: str) -> ShellScanResult:
    """Scan literal Bash syntax and explicitly report bounded-scan exhaustion."""
    normalized = re.sub(r"(?<!\\)((?:\\\\)*)\\\r?\n", r"\1", script)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > _MAX_SHELL_SOURCE_CHARS:
        return ShellScanResult((), "source character limit exceeded")

    scanner = _ShellScanner(normalized)
    try:
        invocations = scanner.scan()
    except _ShellScanIncomplete as error:
        return ShellScanResult(tuple(scanner.invocations), str(error))
    return ShellScanResult(invocations)


def direct_doc_lattice_invocations(script: str) -> tuple[_Invocation, ...]:
    """Return conservative direct doc-lattice commands from literal Bash syntax.

    The scanner is bounded, recursive, and non-executing. It intentionally does not resolve
    aliases, functions, variables used as executable names, ``eval``/``source``, ``sh -c`` or
    ``bash -c``, external wrapper scripts, actions, or reusable workflows.

    Raises:
        ConfigError: If a scanner resource bound prevents a complete result.
    """
    result = scan_doc_lattice_invocations(script)
    if result.incomplete_reason is not None:
        raise ConfigError(f"shell scan incomplete: {result.incomplete_reason}")
    return result.invocations


def _invocation_in_simple_command(words: list[_ShellWord]) -> _Invocation | None:
    executable_index = _doc_lattice_command_index(words, 0)
    if executable_index is None:
        return None
    subcommand_index = _doc_lattice_subcommand_index(words, executable_index + 1)
    if subcommand_index is None or subcommand_index >= len(words):
        return None
    subcommand = words[subcommand_index]
    if subcommand.dynamic or not subcommand.literal:
        return None
    arguments = words[subcommand_index:]
    has_dry_run = any(
        not argument.dynamic and argument.literal == "--dry-run" for argument in arguments
    )
    return subcommand.literal, has_dry_run


def _skip_shell_prefixes(words: list[_ShellWord], start: int) -> int:
    index = start
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return index
        if word.literal in _COMMAND_PREFIXES or _SHELL_ASSIGNMENT_RE.fullmatch(word.literal):
            index += 1
            continue
        if word.literal == "time":
            index += 1
            if index < len(words) and not words[index].dynamic and words[index].literal == "-p":
                index += 1
            continue
        if word.literal == "env":
            index = _skip_env_prefix(words, index + 1)
            continue
        if word.literal == "command":
            return _skip_command_builtin(words, index + 1)
        if word.literal == "exec":
            return _skip_exec_wrapper(words, index + 1)
        return index
    return index


def _skip_command_builtin(words: list[_ShellWord], start: int) -> int:
    index = start
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return index
        if word.literal == "--":
            return index + 1
        if not word.literal.startswith("-"):
            return index
        if "v" in word.literal[1:] or "V" in word.literal[1:]:
            return len(words)
        index += 1
    return index


def _skip_exec_wrapper(words: list[_ShellWord], start: int) -> int:
    index = start
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return index
        if word.literal == "--":
            return index + 1
        if word.literal == "-a":
            index += 2
        elif word.literal.startswith("-"):
            index += 1
        else:
            return index
    return index


def _skip_env_prefix(words: list[_ShellWord], start: int) -> int:
    index = start
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return index
        if _ENV_ASSIGNMENT_RE.fullmatch(word.literal):
            index += 1
        elif word.literal in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
        elif word.literal.startswith("-"):
            index += 1
        else:
            return index
    return index


def _doc_lattice_command_index(
    words: list[_ShellWord],
    start: int,
) -> int | None:
    """Resolve one direct command, including an optional named Bash coprocess."""
    command_index = _skip_shell_prefixes(words, start)
    if (
        command_index < len(words)
        and not words[command_index].dynamic
        and words[command_index].literal == "coproc"
    ):
        return _coproc_doc_lattice_command_index(words, command_index + 1)
    return _doc_lattice_payload_index(words, command_index)


def _coproc_doc_lattice_command_index(
    words: list[_ShellWord],
    start: int,
) -> int | None:
    """Resolve the unnamed command or one optional literal coprocess name."""
    unnamed = _doc_lattice_command_after_prefixes(words, start)
    if unnamed is not None:
        return unnamed
    if start >= len(words):
        return None
    name = words[start]
    if name.dynamic or not _is_name(name.literal):
        return None
    return _doc_lattice_command_after_prefixes(words, start + 1)


def _doc_lattice_command_after_prefixes(
    words: list[_ShellWord],
    start: int,
) -> int | None:
    """Reuse normal prefix, wrapper, and payload resolution from one command start."""
    executable_index = _skip_shell_prefixes(words, start)
    return _doc_lattice_payload_index(words, executable_index)


def _doc_lattice_payload_index(
    words: list[_ShellWord],
    executable_index: int,
) -> int | None:
    if executable_index >= len(words):
        return None
    executable_word = words[executable_index]
    if _is_doc_lattice_executable(executable_word):
        return executable_index
    if executable_word.dynamic:
        return None
    executable = _basename(executable_word.literal)
    if executable == "uvx":
        return _uvx_payload_index(words, executable_index + 1)
    if executable == "uv":
        return _uv_payload_index(words, executable_index + 1)
    return None


def _uvx_payload_index(words: list[_ShellWord], start: int) -> int | None:
    """Resolve a ``uvx [options] doc-lattice`` payload, tolerating an ``@spec`` suffix."""
    payload_index = _skip_options(words, start, _UVX_OPTIONS_WITH_ARGUMENTS)
    return _matched_launcher_payload(words, payload_index, strip_version=True)


def _uv_payload_index(words: list[_ShellWord], start: int) -> int | None:
    """Resolve ``uv`` launcher payloads for ``run`` and the ``tool run`` (uvx) long form.

    Global flags that precede the subcommand are skipped. An unknown or dynamic option-like
    word between ``uv`` and its subcommand cannot be resolved statically, so the scan fails
    closed by marking itself incomplete instead of silently reporting no invocation.

    Raises:
        _ShellScanIncomplete: If an unresolvable option-like word precedes the subcommand.
    """
    subcommand_index = _skip_uv_global_options(words, start)
    if subcommand_index is None or subcommand_index >= len(words):
        return None
    subcommand = words[subcommand_index]
    if subcommand.dynamic:
        return None
    if subcommand.literal == "run":
        payload_index = _skip_options(
            words,
            subcommand_index + 1,
            _UV_RUN_OPTIONS_WITH_ARGUMENTS,
            non_command_options=_UV_RUN_NON_COMMAND_OPTIONS,
        )
        return _matched_launcher_payload(words, payload_index, strip_version=False)
    if subcommand.literal == "tool":
        run_index = subcommand_index + 1
        if run_index >= len(words) or words[run_index].dynamic or words[run_index].literal != "run":
            return None
        payload_index = _skip_options(words, run_index + 1, _UVX_OPTIONS_WITH_ARGUMENTS)
        return _matched_launcher_payload(words, payload_index, strip_version=True)
    return None


def _skip_uv_global_options(words: list[_ShellWord], start: int) -> int | None:
    """Skip uv global flags to the subcommand index, failing closed on unresolvable options.

    Returns:
        The index of the first non-option word, or ``None`` when only flags remain.

    Raises:
        _ShellScanIncomplete: If an unknown or dynamic option-like word is encountered.
    """
    index = start
    while index < len(words):
        word = words[index]
        if not word.literal.startswith("-"):
            return index
        if word.dynamic:
            raise _ShellScanIncomplete("unresolved uv global option")
        option_name = word.literal.split("=", 1)[0]
        if option_name in _UV_GLOBAL_OPTIONS_WITH_ARGUMENTS:
            index += 1 if "=" in word.literal else 2
        elif word.literal in _UV_GLOBAL_FLAGS:
            index += 1
        else:
            raise _ShellScanIncomplete("unresolved uv global option")
    return None


def _matched_launcher_payload(
    words: list[_ShellWord],
    payload_index: int | None,
    *,
    strip_version: bool,
) -> int | None:
    """Return the payload index when it names doc-lattice, optionally after an ``@spec`` strip."""
    if payload_index is None or payload_index >= len(words):
        return None
    payload = words[payload_index]
    if payload.dynamic:
        return None
    basename = _basename(payload.literal)
    if strip_version:
        basename = basename.split("@", 1)[0]
    if basename == "doc-lattice":
        return payload_index
    return None


def _doc_lattice_subcommand_index(
    words: list[_ShellWord],
    start: int,
) -> int | None:
    """Skip exact root options that can precede a doc-lattice subcommand."""
    index = start
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return None
        if word.literal in _DOC_LATTICE_NON_COMMAND_ROOT_OPTIONS:
            return None
        if word.literal in _DOC_LATTICE_ROOT_OPTIONS:
            index += 1
            continue
        return index
    return index


def _skip_options(
    words: list[_ShellWord],
    start: int,
    options_with_arguments: frozenset[str],
    *,
    non_command_options: frozenset[str] = frozenset(),
) -> int | None:
    index = start
    while index < len(words):
        word = words[index]
        if word.dynamic:
            return None
        literal = word.literal
        if literal == "--":
            return index + 1
        option_name = literal.split("=", 1)[0]
        non_command_short_value = any(
            literal.startswith(option) and literal != option
            for option in non_command_options
            if option.startswith("-") and not option.startswith("--")
        )
        if option_name in non_command_options or non_command_short_value:
            return None
        attached_short_value = any(
            literal.startswith(option) and literal != option
            for option in options_with_arguments
            if option.startswith("-") and not option.startswith("--")
        )
        if option_name in options_with_arguments:
            index += 1 if "=" in literal else 2
        elif attached_short_value or literal.startswith("-"):
            index += 1
        else:
            return index
    return index


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
    simple = {
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
    if character in simple:
        result = (simple[character], start + 1)
    elif character in "01234567":
        result = _read_ansi_c_numeric_escape(
            source,
            start,
            limit,
            _OCTAL_BASE,
            3,
        )
    elif character == "x":
        result = _read_ansi_c_prefixed_escape(source, start, limit, 16, 2)
    elif character == "u":
        result = _read_ansi_c_prefixed_escape(source, start, limit, 16, 4)
    elif character == "U":
        result = _read_ansi_c_prefixed_escape(source, start, limit, 16, 8)
    elif character == "c" and start + 1 < limit:
        controlled = source[start + 1]
        value = 127 if controlled == "?" else ord(controlled.upper()) & 0x1F
        result = (chr(value), start + 2)
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


def _read_ansi_c_numeric_escape(
    source: str,
    start: int,
    limit: int,
    base: int,
    digit_limit: int,
) -> tuple[str, int]:
    value, end = _read_ansi_c_digits(source, start, limit, base, digit_limit)
    return _valid_ansi_c_character(value, source[start:end]), end


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
    if value > _UNICODE_MAX or _SURROGATE_MIN <= value <= _SURROGATE_MAX:
        return f"\\{source}"
    return chr(value)


def _consume_parameter_name(source: str, start: int, limit: int) -> int:
    index = start + 1
    if index >= limit:
        return index
    if source[index].isalpha() or source[index] == "_":
        index += 1
        while index < limit and (source[index].isalnum() or source[index] == "_"):
            index += 1
        return index
    return min(index + 1, limit)


def _is_name(value: str) -> bool:
    return (
        bool(value)
        and (value[0].isalpha() or value[0] == "_")
        and all(character.isalnum() or character == "_" for character in value[1:])
    )


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _is_doc_lattice_executable(word: _ShellWord) -> bool:
    if _basename(word.literal) != "doc-lattice":
        return False
    return not word.dynamic or word.literal.startswith("/")
