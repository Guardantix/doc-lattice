"""Bounded non-executing scanner for direct doc-lattice shell invocations."""

import re
from dataclasses import dataclass

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
        "coproc",
        "do",
        "elif",
        "if",
        "then",
        "time",
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
_COMMAND_OPERATORS = (";;&", "&&", "||", ";;", ";&", ";", "&", "|", "(", ")")
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


@dataclass(slots=True)
class _ScanBudget:
    remaining_steps: int = _MAX_SHELL_SCAN_STEPS

    def step(self, amount: int = 1) -> bool:
        if amount < 0 or self.remaining_steps < amount:
            self.remaining_steps = 0
            return False
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
            return start
        state = _CommandScanState(words=[], heredocs=[])
        index = start
        while index < limit and self.budget.step():
            character = self.source[index]
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
            operator = self._command_operator_at(index, limit)
            if operator is not None:
                self._flush_command(state.words)
                index += len(operator)
                if operator == "(":
                    index = self._scan_commands(
                        index,
                        limit,
                        terminator=")",
                        depth=depth + 1,
                    )
                continue
            word, next_index = self._parse_word(index, limit, depth)
            if next_index == index:
                index += 1
                continue
            state.words.append(word)
            index = next_index
        self._flush_command(state.words)
        return index

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
        if not words or len(self.invocations) >= _MAX_SHELL_INVOCATIONS:
            words.clear()
            return
        invocation = _invocation_in_simple_command(words)
        if invocation is not None:
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
            expansion_end = self._consume_active_expansion(index, limit, depth)
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
    ) -> int | None:
        if depth > _MAX_SHELL_RECURSION_DEPTH:
            return min(index + 1, limit)
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
            end = self._consume_parameter(index + 2, limit, depth + 1)
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
    ) -> int:
        index = start
        braces = 1
        quote: str | None = None
        while index < limit and self.budget.step():
            character = self.source[index]
            if quote == "'":
                if character == "'":
                    quote = None
                index += 1
                continue
            if quote == '"':
                if character == '"':
                    quote = None
                    index += 1
                    continue
                if character == "\\":
                    index = min(index + 2, limit)
                    continue
            elif character in {"'", '"'}:
                quote = character
                index += 1
                continue
            if self.source.startswith("${", index):
                braces += 1
                index += 2
                continue
            expansion_end = self._consume_active_expansion(index, limit, depth)
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
                return operator
        return None

    def _line_end(self, index: int, limit: int) -> int:
        line_end = self.source.find("\n", index, limit)
        return limit if line_end == -1 else line_end


def direct_doc_lattice_invocations(script: str) -> tuple[_Invocation, ...]:
    """Return conservative direct doc-lattice commands from literal Bash syntax.

    The scanner is bounded, recursive, and non-executing. It intentionally does not resolve
    aliases, functions, variables used as executable names, ``eval``/``source``, ``sh -c`` or
    ``bash -c``, external wrapper scripts, actions, or reusable workflows.
    """
    normalized = script.replace("\\\r\n", "").replace("\\\n", "")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > _MAX_SHELL_SOURCE_CHARS:
        normalized = normalized[:_MAX_SHELL_SOURCE_CHARS]
    return _ShellScanner(normalized).scan()


def _invocation_in_simple_command(words: list[_ShellWord]) -> _Invocation | None:
    index = _skip_shell_prefixes(words, 0)
    if index >= len(words):
        return None
    executable_index = _doc_lattice_payload_index(words, index)
    if executable_index is None or executable_index + 1 >= len(words):
        return None
    subcommand = words[executable_index + 1]
    if subcommand.dynamic or not subcommand.literal:
        return None
    arguments = words[executable_index + 1 :]
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


def _doc_lattice_payload_index(
    words: list[_ShellWord],
    executable_index: int,
) -> int | None:
    executable_word = words[executable_index]
    executable = _basename(executable_word.literal)
    if _is_doc_lattice_executable(executable_word):
        return executable_index
    if executable_word.dynamic:
        return None
    payload_index: int | None = None
    if executable == "uvx":
        payload_index = _skip_options(
            words,
            executable_index + 1,
            _UVX_OPTIONS_WITH_ARGUMENTS,
        )
    elif executable == "uv":
        run_index = executable_index + 1
        if (
            run_index < len(words)
            and not words[run_index].dynamic
            and words[run_index].literal == "run"
        ):
            payload_index = _skip_options(
                words,
                run_index + 1,
                _UV_RUN_OPTIONS_WITH_ARGUMENTS,
                non_command_options=_UV_RUN_NON_COMMAND_OPTIONS,
            )
    if (
        payload_index is not None
        and payload_index < len(words)
        and not words[payload_index].dynamic
        and _basename(words[payload_index].literal) == "doc-lattice"
    ):
        return payload_index
    return None


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
