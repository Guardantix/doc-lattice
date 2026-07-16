"""Pure repository-global and managed GitHub CI audit policy."""

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from doc_lattice.error_types import ConfigError

from .model import (
    ArtifactRole,
    AuditFinding,
    InstalledArtifact,
    RepositoryIdentity,
    WorkflowDiscovery,
    WorkflowDocument,
    WorkflowJob,
    WorkflowStep,
    WorkflowStructureEntry,
)
from .render import render_managed_artifacts, render_workflows
from .workflow_parser import parse_workflow

PR_EVENTS = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
    }
)
SECRET_NAMES = frozenset({"LINEAR_API_KEY", "DOC_LATTICE_LINEAR_API_KEY"})

_COMMAND_SEPARATORS = frozenset(";&|()")
_SHELL_PREFIXES = frozenset({"if", "then", "do", "!"})
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
            "--env-file",
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
_SHELL_ASSIGNMENT_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\+=|=).*",
    re.DOTALL,
)
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_SECRET_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:LINEAR_API_KEY|DOC_LATTICE_LINEAR_API_KEY)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CANONICAL_LINEAR_PATH = ".github/workflows/doc-lattice-linear.yml"
_CANONICAL_LINEAR_ENV_VALUE = (
    "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"  # pragma: allowlist secret
)
_ROOT_ENV_PATH_LENGTH = 2
_JOB_FIELD_PATH_LENGTH = 3
_JOB_ENV_PATH_LENGTH = 4
_STEP_FIELD_PATH_LENGTH = 5
_STEP_ENV_PATH_LENGTH = 6
_OCTAL_BASE = 8
_UNICODE_MAX = 0x10FFFF
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
_MANAGED_MESSAGES = {
    "MANAGED_TRIGGERS": "managed workflow triggers differ from the canonical installation",
    "MANAGED_PERMISSIONS": "managed workflow permissions differ from the canonical installation",
    "MANAGED_JOB": "managed workflow job structure differs from the canonical installation",
    "MANAGED_ACTION": "managed workflow action identities differ from the canonical installation",
    "MANAGED_CHECKOUT": "managed checkout must disable persisted credentials exactly",
    "MANAGED_CACHE": "managed workflow cache policy differs from the canonical installation",
    "MANAGED_COMMAND": "managed workflow commands differ from the canonical installation",
    "MANAGED_SECRET": (  # pragma: allowlist secret
        "managed Linear secret scope differs from the canonical installation"
    ),
}


@dataclass(slots=True)
class _SubstitutionScanState:
    separated: list[str]
    quotes: list[str | None]
    substitution_depths: list[int]
    parameter_depths: list[int]


def audit_global_workflows(
    documents: tuple[WorkflowDocument, ...],
) -> tuple[AuditFinding, ...]:
    """Audit repository-global GitHub Actions prohibitions.

    These rules intentionally avoid treating unrelated workflow permissions, action tags,
    checkout credential settings, or cache usage as repository-global policy.

    Args:
        documents: Parsed repository workflow documents.

    Returns:
        Deterministically sorted unique repository-global findings.
    """
    findings: list[AuditFinding] = []
    for document in documents:
        trigger_names = frozenset(trigger.name for trigger in document.triggers)
        if "pull_request_target" in trigger_names:
            findings.append(
                _finding(
                    document,
                    "PULL_REQUEST_TARGET",
                    "pull_request_target is prohibited for repository workflows",
                )
            )
        if trigger_names & PR_EVENTS:
            invocations = tuple(
                invocation
                for job in document.jobs
                for step in job.steps
                if step.run is not None
                for invocation in direct_doc_lattice_invocations(step.run)
            )
            if any(command == "linear" for command, _dry_run in invocations):
                findings.append(
                    _finding(
                        document,
                        "PR_LINEAR_INVOCATION",
                        "pull-request workflows must not invoke doc-lattice linear",
                    )
                )
            if any(command == "reconcile" and not dry_run for command, dry_run in invocations):
                findings.append(
                    _finding(
                        document,
                        "PR_MUTATING_RECONCILE",
                        "pull-request workflows must use --dry-run for doc-lattice reconcile",
                    )
                )
        if _has_linear_secret_reference(document):
            findings.append(
                _finding(
                    document,
                    "LINEAR_SECRET_REFERENCE",
                    "Linear secret names are allowed only in the canonical trusted step",
                )
            )
    return _sorted_unique(findings)


def audit_managed_installation(
    discovery: WorkflowDiscovery,
    installed: tuple[InstalledArtifact | None, ...],
    repository: RepositoryIdentity,
    running_version: str,
) -> tuple[AuditFinding, ...]:
    """Audit the exact managed GitHub CI installation separately from global policy.

    Args:
        discovery: Parsed repository workflow discovery state.
        installed: Read-only inspection results aligned with expected managed artifacts.
        repository: Explicit or origin-derived repository identity for this audit.
        running_version: Current generator version used to diagnose stale installations.

    Returns:
        Deterministically sorted unique managed-installation findings.

    Raises:
        ConfigError: If inspection results do not use the canonical three-slot order.
    """
    canonical = render_managed_artifacts(repository.display, running_version)
    if len(installed) != len(canonical):
        raise ConfigError("managed artifact inspection must contain exactly three slots")
    for index, artifact in enumerate(installed):
        if artifact is None:
            continue
        expected = canonical[index]
        if (
            artifact.expected.role != expected.role
            or artifact.expected.relative_path != expected.relative_path
        ):
            raise ConfigError(
                "managed artifact inspection must use canonical order: offline, linear, bootstrap"
            )

    findings: list[AuditFinding] = []
    if not discovery.directory_exists:
        findings.append(
            AuditFinding(
                path=".github/workflows",
                code="MISSING_WORKFLOW_DIRECTORY",
                message="managed GitHub workflow directory is missing",
            )
        )

    documents_by_path = {document.path.as_posix(): document for document in discovery.documents}
    for index, artifact in enumerate(installed):
        canonical_artifact = canonical[index]
        if artifact is None:
            path = canonical_artifact.relative_path.as_posix()
            findings.append(
                AuditFinding(
                    path=path,
                    code="MISSING_MANAGED_ARTIFACT",
                    message=f"managed {canonical_artifact.role} artifact is missing",
                )
            )
            continue
        findings.extend(
            _audit_installed_artifact(
                artifact,
                documents_by_path,
                repository,
                running_version,
            )
        )
    return _sorted_unique(findings)


def _audit_installed_artifact(
    artifact: InstalledArtifact,
    documents_by_path: dict[str, WorkflowDocument],
    repository: RepositoryIdentity,
    running_version: str,
) -> list[AuditFinding]:
    """Audit one present canonical artifact after positional validation."""
    path = artifact.expected.relative_path.as_posix()
    marker = artifact.marker
    if marker is None:
        return [
            AuditFinding(
                path=path,
                code="MANAGED_MARKER",
                message=artifact.marker_error or "managed ownership marker is invalid",
            )
        ]

    findings: list[AuditFinding] = []
    if marker.version != running_version:
        findings.append(
            AuditFinding(
                path=path,
                code="STALE_GENERATOR",
                message=(
                    f"managed artifact uses generator version {marker.version!r}, not "
                    f"{running_version!r}; run `doc-lattice ci refresh`"
                ),
            )
        )
    if marker.repository.comparison_key != repository.comparison_key:
        findings.append(
            AuditFinding(
                path=path,
                code="REPOSITORY_IDENTITY",
                message=(
                    f"managed artifact repository {marker.repository.display!r} does not "
                    f"match {repository.display!r}; run `doc-lattice ci refresh`"
                ),
            )
        )
    if artifact.expected.role == "bootstrap":
        return findings

    document = documents_by_path.get(path)
    if document is None:
        findings.append(
            AuditFinding(
                path=path,
                code="MISSING_MANAGED_WORKFLOW",
                message="present managed workflow was not discovered as workflow YAML",
            )
        )
        return findings
    expected_document = _expected_workflow_document(
        artifact.expected.role,
        marker.repository,
        marker.version,
    )
    findings.extend(
        AuditFinding(path=path, code=code, message=_MANAGED_MESSAGES[code])
        for code in _managed_semantic_codes(document, expected_document)
    )
    return findings


def direct_doc_lattice_invocations(script: str) -> tuple[tuple[str, bool], ...]:
    """Find conservative direct doc-lattice shell invocations without executing the shell.

    The detector recognizes a direct executable token, a path whose final component is
    ``doc-lattice``, and the equivalent payload launched by ``uvx`` or ``uv run``. Shell
    syntax is intentionally approximated: complete simple commands before malformed quoting
    are retained, while the malformed fragment is ignored.

    Args:
        script: GitHub Actions ``run`` script text.

    Returns:
        One ``(subcommand, has_dry_run)`` pair per recognized simple command in source order.
    """
    normalized = script.replace("\\\r\n", "").replace("\\\n", "")
    normalized = _strip_heredoc_bodies(normalized)
    normalized = _separate_legacy_command_substitutions(normalized)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\n", "\n;\n")
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""

    invocations: list[tuple[str, bool]] = []
    command: list[str] = []
    try:
        for token in lexer:
            if token and all(character in _COMMAND_SEPARATORS for character in token):
                invocations.extend(_invocations_in_simple_command(command))
                command = []
            else:
                command.append(token)
    except ValueError:
        return tuple(invocations)
    invocations.extend(_invocations_in_simple_command(command))
    return tuple(invocations)


def _invocations_in_simple_command(command: list[str]) -> tuple[tuple[str, bool], ...]:
    index = _skip_shell_prefixes(command, 0)
    if index >= len(command):
        return ()

    executable_index = _doc_lattice_payload_index(command, index)
    if executable_index is None or executable_index + 1 >= len(command):
        return ()
    arguments = command[executable_index + 1 :]
    return ((arguments[0], "--dry-run" in arguments),)


def _skip_shell_prefixes(command: list[str], start: int) -> int:
    index = start
    while index < len(command):
        word = command[index]
        if word in _SHELL_PREFIXES or _SHELL_ASSIGNMENT_RE.fullmatch(word):
            index += 1
            continue
        if word != "env":
            redirection_end = _skip_redirection(command, index)
            if redirection_end != index:
                index = redirection_end
                continue
            wrapper_end = _skip_command_wrapper(command, index)
            if wrapper_end != index:
                index = wrapper_end
                continue
            break
        index = _skip_env_prefix(command, index + 1)
    return index


def _skip_redirection(command: list[str], start: int) -> int:
    index = start
    if (
        index + 1 < len(command)
        and command[index].isdigit()
        and _is_redirection_token(command[index + 1])
    ):
        index += 1
    if index >= len(command) or not _is_redirection_token(command[index]):
        return start
    index += 1
    return min(index + 1, len(command))


def _is_redirection_token(word: str) -> bool:
    return (
        bool(word)
        and all(character in "<>&|" for character in word)
        and ("<" in word or ">" in word)
    )


def _skip_command_wrapper(command: list[str], start: int) -> int:
    wrapper = command[start]
    if wrapper == "command":
        return _skip_command_builtin(command, start + 1)
    if wrapper == "exec":
        return _skip_exec_wrapper(command, start + 1)
    return start


def _skip_command_builtin(command: list[str], start: int) -> int:
    index = start
    while index < len(command):
        word = command[index]
        if word == "--":
            return index + 1
        if not word.startswith("-"):
            break
        if "v" in word[1:] or "V" in word[1:]:
            return len(command)
        index += 1
    return index


def _skip_exec_wrapper(command: list[str], start: int) -> int:
    index = start
    while index < len(command):
        word = command[index]
        if word == "--":
            return index + 1
        if word == "-a":
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            break
    return index


def _skip_env_prefix(command: list[str], start: int) -> int:
    index = start
    while index < len(command):
        word = command[index]
        if _ENV_ASSIGNMENT_RE.fullmatch(word):
            index += 1
        elif word in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            break
    return index


def _doc_lattice_payload_index(command: list[str], executable_index: int) -> int | None:
    executable = _basename(command[executable_index])
    if executable == "doc-lattice":
        return executable_index
    payload_index: int | None = None
    if executable == "uvx":
        payload_index = _skip_options(
            command,
            executable_index + 1,
            _UVX_OPTIONS_WITH_ARGUMENTS,
        )
    elif executable == "uv":
        run_index = executable_index + 1
        if run_index < len(command) and command[run_index] == "run":
            payload_index = _skip_options(
                command,
                run_index + 1,
                _UV_RUN_OPTIONS_WITH_ARGUMENTS,
                non_command_options=_UV_RUN_NON_COMMAND_OPTIONS,
            )
    if (
        payload_index is not None
        and payload_index < len(command)
        and _basename(command[payload_index]) == "doc-lattice"
    ):
        return payload_index
    return None


def _skip_options(
    command: list[str],
    start: int,
    options_with_arguments: frozenset[str],
    *,
    non_command_options: frozenset[str] = frozenset(),
) -> int | None:
    index = start
    while index < len(command):
        word = command[index]
        if word == "--":
            return index + 1
        option_name = word.split("=", 1)[0]
        non_command_short_value = any(
            word.startswith(option) and word != option
            for option in non_command_options
            if option.startswith("-") and not option.startswith("--")
        )
        if option_name in non_command_options or non_command_short_value:
            return None
        attached_short_value = any(
            word.startswith(option) and word != option
            for option in options_with_arguments
            if option.startswith("-") and not option.startswith("--")
        )
        if option_name in options_with_arguments:
            index += 1 if "=" in word else 2
        elif attached_short_value or word.startswith("-"):
            index += 1
        else:
            return index
    return index


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _strip_heredoc_bodies(script: str) -> str:
    lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    retained: list[str] = []
    pending: list[tuple[str, bool]] = []
    quote: str | None = None
    arithmetic_depth = 0
    for line in lines:
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                pending.pop(0)
            continue
        retained_line, delimiters, quote, arithmetic_depth = _heredoc_delimiters(
            line,
            quote,
            arithmetic_depth,
        )
        retained.append(retained_line)
        pending.extend(delimiters)
    return "\n".join(retained)


def _heredoc_delimiters(
    line: str,
    quote: str | None,
    arithmetic_depth: int,
) -> tuple[str, list[tuple[str, bool]], str | None, int]:
    delimiters: list[tuple[str, bool]] = []
    index = 0
    word_start = quote is None
    while index < len(line):
        character = line[index]
        if quote is not None:
            index, quote = _advance_quoted_character(line, index, quote)
            word_start = False
            continue
        if character in {" ", "\t"}:
            word_start = True
            index += 1
            continue
        if character == "#" and word_start:
            return line[:index], delimiters, quote, arithmetic_depth
        if character in {"'", '"'}:
            quote = character
            word_start = False
            index += 1
            continue
        if character == "\\":
            word_start = False
            index += 2
            continue
        arithmetic_step = _arithmetic_scan_step(line, index, arithmetic_depth)
        if arithmetic_step is not None:
            index, arithmetic_depth = arithmetic_step
            word_start = False
            continue
        if line.startswith("<<<", index):
            index += 3
            word_start = True
            continue
        if not line.startswith("<<", index):
            word_start = character in ";&|()<>"
            index += 1
            continue
        index += 2
        strip_tabs = index < len(line) and line[index] == "-"
        if strip_tabs:
            index += 1
        while index < len(line) and line[index] in {" ", "\t"}:
            index += 1
        delimiter, index = _read_heredoc_delimiter(line, index)
        if delimiter:
            delimiters.append((delimiter, strip_tabs))
        word_start = False
    return line, delimiters, quote, arithmetic_depth


def _advance_quoted_character(
    text: str,
    index: int,
    quote: str,
) -> tuple[int, str | None]:
    character = text[index]
    if character == quote:
        return index + 1, None
    if character == "\\" and quote == '"' and index + 1 < len(text):
        return index + 2, quote
    return index + 1, quote


def _arithmetic_scan_step(
    line: str,
    index: int,
    depth: int,
) -> tuple[int, int] | None:
    if line.startswith("$((", index):
        return index + 3, depth + 1
    if line.startswith("((", index):
        return index + 2, depth + 1
    if depth == 0:
        return None
    if line.startswith("))", index):
        return index + 2, depth - 1
    return index + 1, depth


def _read_heredoc_delimiter(line: str, start: int) -> tuple[str, int]:
    quote: str | None = None
    index = start
    characters: list[str] = []
    while index < len(line):
        character = line[index]
        if quote is not None:
            index, quote = _collect_quoted_delimiter_character(
                line,
                index,
                quote,
                characters,
            )
            continue
        if character.isspace() or character in ";&|()<>":
            break
        if line.startswith("$'", index):
            segment, index, closed = _read_ansi_c_quoted_segment(line, index)
            if not closed:
                return "", index
            characters.extend(segment)
            continue
        if line.startswith('$"', index):
            quote = '"'
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "\\" and index + 1 < len(line):
            index += 1
            characters.append(line[index])
        else:
            characters.append(character)
        index += 1
    if quote is not None:
        return "", index
    return "".join(characters), index


def _read_ansi_c_quoted_segment(
    line: str,
    start: int,
) -> tuple[str, int, bool]:
    characters: list[str] = []
    index = start + 2
    while index < len(line):
        character = line[index]
        if character == "'":
            return "".join(characters), index + 1, True
        if character != "\\":
            characters.append(character)
            index += 1
            continue
        escaped, index = _read_ansi_c_escape(line, index + 1)
        characters.append(escaped)
    return "", index, False


def _read_ansi_c_escape(line: str, start: int) -> tuple[str, int]:
    if start >= len(line):
        return "\\", start
    character = line[start]
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
        result = _read_ansi_c_numeric_escape(line, start, _OCTAL_BASE, 3)
    elif character == "x":
        result = _read_ansi_c_prefixed_escape(line, start, 16, 2)
    elif character == "u":
        result = _read_ansi_c_prefixed_escape(line, start, 16, 4)
    elif character == "U":
        result = _read_ansi_c_prefixed_escape(line, start, 16, 8)
    elif character == "c" and start + 1 < len(line):
        controlled = line[start + 1]
        value = 127 if controlled == "?" else ord(controlled.upper()) & 0x1F
        result = (chr(value), start + 2)
    else:
        result = (f"\\{character}", start + 1)
    return result


def _read_ansi_c_prefixed_escape(
    line: str,
    prefix_index: int,
    base: int,
    limit: int,
) -> tuple[str, int]:
    value, end = _read_ansi_c_digits(line, prefix_index + 1, base, limit)
    if end == prefix_index + 1:
        return f"\\{line[prefix_index]}", end
    return _valid_ansi_c_character(value, line[prefix_index:end]), end


def _read_ansi_c_numeric_escape(
    line: str,
    start: int,
    base: int,
    limit: int,
) -> tuple[str, int]:
    value, end = _read_ansi_c_digits(line, start, base, limit)
    return _valid_ansi_c_character(value, line[start:end]), end


def _read_ansi_c_digits(
    line: str,
    start: int,
    base: int,
    limit: int,
) -> tuple[int, int]:
    valid = "01234567" if base == _OCTAL_BASE else "0123456789abcdefABCDEF"
    index = start
    while index < len(line) and index - start < limit and line[index] in valid:
        index += 1
    value = int(line[start:index], base) if index != start else 0
    return value, index


def _valid_ansi_c_character(value: int, source: str) -> str:
    if value > _UNICODE_MAX or _SURROGATE_MIN <= value <= _SURROGATE_MAX:
        return f"\\{source}"
    return chr(value)


def _collect_quoted_delimiter_character(
    line: str,
    index: int,
    quote: str,
    characters: list[str],
) -> tuple[int, str | None]:
    character = line[index]
    if character == quote:
        return index + 1, None
    if quote == '"' and character == "\\" and index + 1 < len(line):
        escaped = line[index + 1]
        if escaped in {"$", '"', "\\", "`"}:
            characters.append(escaped)
            return index + 2, quote
    characters.append(character)
    return index + 1, quote


def _separate_legacy_command_substitutions(script: str) -> str:
    state = _SubstitutionScanState(
        separated=[],
        quotes=[None],
        substitution_depths=[],
        parameter_depths=[0],
    )
    index = 0
    while index < len(script):
        character = script[index]
        context_end = _advance_shell_context(script, index, state)
        if context_end is not None:
            index = context_end
            continue
        if character != "`" or state.quotes[-1] == "'":
            state.separated.append(character)
            index += 1
            continue
        closing = _legacy_command_substitution_end(script, index + 1)
        if closing is None:
            state.separated.append(character)
            index += 1
            continue
        body = script[index + 1 : closing]
        state.separated.extend(_legacy_substitution_fragments(body, state.quotes))
        index = closing + 1
    return "".join(state.separated)


def _advance_shell_context(
    script: str,
    index: int,
    state: _SubstitutionScanState,
) -> int | None:
    character = script[index]
    end: int | None = None
    if character == "\\":
        end = min(index + 2, len(script))
        state.separated.append(script[index:end])
    elif character in {"'", '"'}:
        state.quotes[-1] = _updated_shell_quote(state.quotes[-1], character)
        state.separated.append(character)
        end = index + 1
    elif script.startswith("${", index) and state.quotes[-1] != "'":
        state.parameter_depths[-1] += 1
        state.separated.append("${")
        end = index + 2
    elif state.parameter_depths[-1] and state.quotes[-1] is None and character == "}":
        state.parameter_depths[-1] -= 1
        state.separated.append(character)
        end = index + 1
    elif (
        script.startswith("$(", index)
        and not script.startswith("$((", index)
        and state.quotes[-1] != "'"
    ):
        state.separated.append("$(")
        state.quotes.append(None)
        state.substitution_depths.append(1)
        state.parameter_depths.append(0)
        end = index + 2
    elif (
        state.substitution_depths
        and state.parameter_depths[-1] == 0
        and state.quotes[-1] is None
        and character in {"(", ")"}
    ):
        _update_substitution_context(character, state)
        state.separated.append(character)
        end = index + 1
    return end


def _update_substitution_context(
    character: str,
    state: _SubstitutionScanState,
) -> None:
    if character == "(":
        state.substitution_depths[-1] += 1
        return
    state.substitution_depths[-1] -= 1
    if state.substitution_depths[-1] == 0:
        state.substitution_depths.pop()
        state.quotes.pop()
        state.parameter_depths.pop()


def _updated_shell_quote(quote: str | None, character: str) -> str | None:
    if quote is None:
        return character
    if quote == character:
        return None
    return quote


def _legacy_substitution_fragments(
    body: str,
    quotes: list[str | None],
) -> tuple[str, str, str]:
    quote_boundaries = '"' * sum(quote == '"' for quote in quotes)
    return f"{quote_boundaries} ; ", body, f" ; {quote_boundaries}"


def _legacy_command_substitution_end(script: str, start: int) -> int | None:
    index = start
    while index < len(script):
        if script[index] == "\\":
            index += 2
        elif script[index] == "`":
            return index
        else:
            index += 1
    return None


def _expected_workflow_document(
    role: ArtifactRole,
    repository: RepositoryIdentity,
    version: str,
) -> WorkflowDocument:
    workflows = render_workflows(repository.display, version)
    if role == "offline":
        artifact = workflows[0]
    elif role == "linear":
        artifact = workflows[1]
    else:
        raise ConfigError("bootstrap artifact cannot be parsed as managed workflow YAML")
    return parse_workflow(Path(artifact.relative_path.as_posix()), artifact.text)


def _managed_semantic_codes(
    document: WorkflowDocument,
    expected: WorkflowDocument,
) -> frozenset[str]:
    codes: set[str] = set()
    if document.triggers != expected.triggers:
        codes.add("MANAGED_TRIGGERS")
    if document.permissions != expected.permissions:
        codes.add("MANAGED_PERMISSIONS")

    if tuple(job.job_id for job in document.jobs) != tuple(job.job_id for job in expected.jobs):
        codes.add("MANAGED_JOB")
        return frozenset(codes)

    for job, expected_job in zip(document.jobs, expected.jobs, strict=True):
        if job.permissions != expected_job.permissions:
            codes.add("MANAGED_PERMISSIONS")
        if job.if_condition != expected_job.if_condition:
            codes.add("MANAGED_COMMAND")
        if job.environment != expected_job.environment or job.runs_on != expected_job.runs_on:
            codes.add("MANAGED_JOB")
        if job.env != expected_job.env:
            codes.add("MANAGED_COMMAND")
        codes.update(_managed_step_codes(job, expected_job))

    if _has_linear_secret_reference(document):
        codes.add("MANAGED_SECRET")
    codes.update(_managed_structure_codes(document, expected))
    return frozenset(codes)


def _managed_step_codes(job: WorkflowJob, expected: WorkflowJob) -> set[str]:
    codes: set[str] = set()
    current_steps_without_cache = tuple(
        step for step in job.steps if _action_name(step.uses) != "actions/cache"
    )
    if len(current_steps_without_cache) != len(job.steps):
        codes.add("MANAGED_CACHE")

    expected_actions = tuple(step.uses for step in expected.steps if step.uses is not None)
    current_actions = tuple(
        step.uses for step in current_steps_without_cache if step.uses is not None
    )
    if current_actions != expected_actions:
        codes.add("MANAGED_ACTION")

    expected_runs = tuple(step.run for step in expected.steps if step.run is not None)
    current_runs = tuple(step.run for step in job.steps if step.run is not None)
    if current_runs != expected_runs:
        codes.add("MANAGED_COMMAND")

    expected_kinds = tuple(_step_kind(step) for step in expected.steps)
    current_kinds = tuple(_step_kind(step) for step in current_steps_without_cache)
    if (
        current_kinds != expected_kinds
        and current_actions == expected_actions
        and current_runs == expected_runs
    ):
        code = "MANAGED_ACTION" if "action" in current_kinds else "MANAGED_JOB"
        codes.add(code)

    expected_checkout = _find_action_step(expected.steps, "actions/checkout")
    current_checkout = _find_action_step(job.steps, "actions/checkout")
    if (
        expected_checkout is not None
        and current_checkout is not None
        and current_checkout.with_values != expected_checkout.with_values
    ):
        codes.add("MANAGED_CHECKOUT")

    expected_setup_uv = _find_action_step(expected.steps, "astral-sh/setup-uv")
    current_setup_uv = _find_action_step(job.steps, "astral-sh/setup-uv")
    if (
        expected_setup_uv is not None
        and current_setup_uv is not None
        and current_setup_uv.with_values != expected_setup_uv.with_values
    ):
        codes.add("MANAGED_CACHE")

    return codes


def _find_action_step(
    steps: tuple[WorkflowStep, ...],
    action_name: str,
) -> WorkflowStep | None:
    return next((step for step in steps if _action_name(step.uses) == action_name), None)


def _action_name(uses: str | None) -> str | None:
    if uses is None:
        return None
    return uses.split("@", 1)[0]


def _step_kind(step: WorkflowStep) -> str:
    if step.uses is not None:
        return "action"
    if step.run is not None:
        return "command"
    return "other"


def _managed_structure_codes(
    document: WorkflowDocument,
    expected: WorkflowDocument,
) -> set[str]:
    codes: set[str] = set()
    current = _structure_map(document.structure, include_steps=False)
    desired = _structure_map(expected.structure, include_steps=False)
    all_current = _structure_map(document.structure, include_steps=True)
    all_desired = _structure_map(expected.structure, include_steps=True)
    for path in current.keys() | desired.keys():
        if current.get(path) != desired.get(path):
            codes.add(_structure_code(path, current, desired))

    for job, expected_job in zip(document.jobs, expected.jobs, strict=True):
        if len(job.steps) != len(expected_job.steps):
            step_code_added = False
            if tuple(step.uses for step in job.steps if step.uses is not None) != tuple(
                step.uses for step in expected_job.steps if step.uses is not None
            ):
                code = (
                    "MANAGED_CACHE"
                    if any(_action_name(step.uses) == "actions/cache" for step in job.steps)
                    else "MANAGED_ACTION"
                )
                codes.add(code)
                step_code_added = True
            if tuple(step.run for step in job.steps if step.run is not None) != tuple(
                step.run for step in expected_job.steps if step.run is not None
            ):
                codes.add("MANAGED_COMMAND")
                step_code_added = True
            if not step_code_added:
                codes.add("MANAGED_JOB")
            continue
        if tuple(_step_kind(step) for step in job.steps) != tuple(
            _step_kind(step) for step in expected_job.steps
        ):
            continue
        for step in job.steps:
            base = ("jobs", job.job_id, "steps", str(step.index))
            step_current = _subtree_map(document.structure, base)
            step_desired = _subtree_map(expected.structure, base)
            for relative_path in step_current.keys() | step_desired.keys():
                if step_current.get(relative_path) != step_desired.get(relative_path):
                    full_path = (*base, *relative_path)
                    codes.add(_structure_code(full_path, all_current, all_desired))
    return codes


def _structure_map(
    structure: tuple[WorkflowStructureEntry, ...],
    *,
    include_steps: bool,
) -> dict[tuple[str, ...], tuple[str, str | None]]:
    return {
        entry.path: (entry.kind, entry.value)
        for entry in structure
        if not _is_display_name_path(entry.path)
        and (include_steps or not _is_step_path(entry.path))
    }


def _subtree_map(
    structure: tuple[WorkflowStructureEntry, ...],
    base: tuple[str, ...],
) -> dict[tuple[str, ...], tuple[str, str | None]]:
    return {
        entry.path[len(base) :]: (entry.kind, entry.value)
        for entry in structure
        if entry.path[: len(base)] == base and not _is_display_name_path(entry.path)
    }


def _is_step_path(path: tuple[str, ...]) -> bool:
    return len(path) >= _JOB_ENV_PATH_LENGTH and path[0] == "jobs" and path[2] == "steps"


def _is_display_name_path(path: tuple[str, ...]) -> bool:
    return (
        path == ("name",)
        or (len(path) == _JOB_FIELD_PATH_LENGTH and path[0] == "jobs" and path[2] == "name")
        or (
            len(path) == _STEP_FIELD_PATH_LENGTH
            and path[0] == "jobs"
            and path[2] == "steps"
            and path[4] == "name"
        )
    )


def _structure_code(
    path: tuple[str, ...],
    current: dict[tuple[str, ...], tuple[str, str | None]],
    desired: dict[tuple[str, ...], tuple[str, str | None]],
) -> str:
    if path and path[0] == "on":
        code = "MANAGED_TRIGGERS"
    elif "permissions" in path:
        code = "MANAGED_PERMISSIONS"
    elif path and path[-1] == "uses":
        values = (current.get(path), desired.get(path))
        if any(value is not None and _action_name(value[1]) == "actions/cache" for value in values):
            code = "MANAGED_CACHE"
        else:
            code = "MANAGED_ACTION"
    elif "env" in path and _structure_values_reference_secret(path, current, desired):
        code = "MANAGED_SECRET"
    elif _is_command_behavior_path(path):
        code = "MANAGED_COMMAND"
    elif "with" in path:
        code = _with_structure_code(path, current, desired)
    else:
        code = "MANAGED_JOB"
    return code


def _is_command_behavior_path(path: tuple[str, ...]) -> bool:
    command_fields = {
        "run",
        "if",
        "continue-on-error",
        "shell",
        "working-directory",
        "defaults",
        "env",
        "strategy",
        "timeout-minutes",
    }
    return any(component in command_fields for component in path)


def _with_structure_code(
    path: tuple[str, ...],
    current: dict[tuple[str, ...], tuple[str, str | None]],
    desired: dict[tuple[str, ...], tuple[str, str | None]],
) -> str:
    uses_path = (*path[: path.index("with")], "uses")
    uses_values = (current.get(uses_path), desired.get(uses_path))
    actions = {
        _action_name(value[1])
        for value in uses_values
        if value is not None and value[1] is not None
    }
    if "actions/checkout" in actions:
        return "MANAGED_CHECKOUT"
    if actions & {"actions/cache", "astral-sh/setup-uv"}:
        return "MANAGED_CACHE"
    return "MANAGED_ACTION"


def _structure_values_reference_secret(
    path: tuple[str, ...],
    current: dict[tuple[str, ...], tuple[str, str | None]],
    desired: dict[tuple[str, ...], tuple[str, str | None]],
) -> bool:
    if path and path[-1].casefold() in {name.casefold() for name in SECRET_NAMES}:
        return True
    return any(
        value is not None and value[1] is not None and _SECRET_NAME_RE.search(value[1]) is not None
        for value in (current.get(path), desired.get(path))
    )


def _has_linear_secret_reference(document: WorkflowDocument) -> bool:
    exempt_path = _canonical_linear_secret_path(document)
    for scalar in document.scalars:
        if scalar.path == exempt_path and scalar.value == _CANONICAL_LINEAR_ENV_VALUE:
            continue
        if _SECRET_NAME_RE.search(scalar.value) is not None:
            return True

    secret_keys = {name.casefold() for name in SECRET_NAMES}
    for entry in document.structure:
        if not _is_environment_key_path(entry.path):
            continue
        if entry.path == exempt_path and entry.value == _CANONICAL_LINEAR_ENV_VALUE:
            continue
        if entry.path[-1].casefold() in secret_keys:
            return True
    return False


def _is_environment_key_path(path: tuple[str, ...]) -> bool:
    return (
        (len(path) == _ROOT_ENV_PATH_LENGTH and path[0] == "env")
        or (len(path) == _JOB_ENV_PATH_LENGTH and path[0] == "jobs" and path[2] == "env")
        or (
            len(path) == _STEP_ENV_PATH_LENGTH
            and path[0] == "jobs"
            and path[2] == "steps"
            and path[4] == "env"
        )
    )


def _canonical_linear_secret_path(document: WorkflowDocument) -> tuple[str, ...] | None:
    if document.path.as_posix() != _CANONICAL_LINEAR_PATH:
        return None
    linear_job = next((job for job in document.jobs if job.job_id == "linear"), None)
    if linear_job is None or not linear_job.steps:
        return None
    final_step = linear_job.steps[-1]
    expected_pair = ("LINEAR_API_KEY", _CANONICAL_LINEAR_ENV_VALUE)
    if expected_pair not in final_step.env:
        return None
    return (
        "jobs",
        "linear",
        "steps",
        str(final_step.index),
        "env",
        "LINEAR_API_KEY",
    )


def _finding(document: WorkflowDocument, code: str, message: str) -> AuditFinding:
    return AuditFinding(path=document.path.as_posix(), code=code, message=message)


def _sorted_unique(findings: list[AuditFinding]) -> tuple[AuditFinding, ...]:
    return tuple(sorted(set(findings)))
