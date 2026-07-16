"""Pure repository-global and managed GitHub CI audit policy."""

import re
import shlex
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

_PUNCTUATION = frozenset(";&|()")
_SHELL_PREFIXES = frozenset({"if", "then", "do", "!"})
_UVX_OPTIONS_WITH_ARGUMENTS = frozenset({"--from", "--python"})
_UV_RUN_OPTIONS_WITH_ARGUMENTS = frozenset(
    {
        "--directory",
        "--env-file",
        "--project",
        "--python",
        "--with",
        "--with-editable",
    }
)
_SHELL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_SECRET_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:LINEAR_API_KEY|DOC_LATTICE_LINEAR_API_KEY)(?![A-Za-z0-9_])"
)
_CANONICAL_LINEAR_PATH = ".github/workflows/doc-lattice-linear.yml"
_CANONICAL_LINEAR_ENV_VALUE = (
    "${{ secrets.DOC_LATTICE_LINEAR_API_KEY }}"  # pragma: allowlist secret
)
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
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\n", ";")
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True

    invocations: list[tuple[str, bool]] = []
    command: list[str] = []
    try:
        for token in lexer:
            if token and all(character in _PUNCTUATION for character in token):
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
            break
        index = _skip_env_prefix(command, index + 1)
    return index


def _skip_env_prefix(command: list[str], start: int) -> int:
    index = start
    while index < len(command):
        word = command[index]
        if _SHELL_ASSIGNMENT_RE.fullmatch(word):
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
) -> int:
    index = start
    while index < len(command):
        word = command[index]
        if word == "--":
            return index + 1
        option_name = word.split("=", 1)[0]
        if option_name in options_with_arguments:
            index += 1 if "=" in word else 2
        elif word.startswith("-"):
            index += 1
        else:
            return index
    return index


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


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
        if (
            job.if_condition != expected_job.if_condition
            or job.environment != expected_job.environment
            or job.runs_on != expected_job.runs_on
        ):
            codes.add("MANAGED_JOB")
        if job.env != expected_job.env:
            codes.add("MANAGED_SECRET")
        codes.update(_managed_step_codes(job, expected_job))

    if _has_linear_secret_reference(document):
        codes.add("MANAGED_SECRET")
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

    if _step_env_layout(job.steps) != _step_env_layout(expected.steps):
        codes.add("MANAGED_SECRET")
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


def _step_env_layout(
    steps: tuple[WorkflowStep, ...],
) -> tuple[tuple[bool, tuple[tuple[str, str], ...]], ...]:
    return tuple((step.index == len(steps) - 1, step.env) for step in steps if step.env)


def _has_linear_secret_reference(document: WorkflowDocument) -> bool:
    exempt_path = _canonical_linear_secret_path(document)
    for scalar in document.scalars:
        if scalar.path == exempt_path and scalar.value == _CANONICAL_LINEAR_ENV_VALUE:
            continue
        if _SECRET_NAME_RE.search(scalar.value) is not None:
            return True

    for job in document.jobs:
        for key, _value in job.env:
            if key in SECRET_NAMES:
                return True
        for step in job.steps:
            for key, value in step.env:
                if (
                    exempt_path is not None
                    and job.job_id == "linear"
                    and step.index == len(job.steps) - 1
                    and key == "LINEAR_API_KEY"
                    and value == _CANONICAL_LINEAR_ENV_VALUE
                ):
                    continue
                if key in SECRET_NAMES:
                    return True
    return False


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
