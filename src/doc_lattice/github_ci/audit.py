"""Pure repository-global and managed GitHub CI audit policy."""

import re
from pathlib import Path

from doc_lattice.error_types import ConfigError

from .identity import validate_final_release_version
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
from .render import CANONICAL_ARTIFACT_TARGETS, render_workflows
from .shell_scanner import direct_doc_lattice_invocations, scan_doc_lattice_invocations
from .workflow_parser import parse_workflow

__all__ = [
    "SECRET_NAMES",
    "audit_global_workflows",
    "audit_managed_installation",
    "direct_doc_lattice_invocations",
]

PR_EVENTS = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
    }
)
SECRET_NAMES = frozenset({"LINEAR_API_KEY", "DOC_LATTICE_LINEAR_API_KEY"})

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
            invocations: list[tuple[str, bool]] = []
            for job in document.jobs:
                for step in job.steps:
                    if step.run is None:
                        continue
                    scan = scan_doc_lattice_invocations(step.run)
                    if scan.incomplete_reason is not None:
                        raise ConfigError(
                            f"{document.path.as_posix()}: shell scan incomplete: "
                            f"{scan.incomplete_reason}"
                        )
                    invocations.extend(scan.invocations)
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
    canonical = CANONICAL_ARTIFACT_TARGETS
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
                message=_stale_generator_message(marker.version, running_version),
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
    if marker.version != running_version:
        # The expected document is rendered from the running release's templates, so it is
        # only a valid semantic baseline when the marker records that same version. On a
        # version mismatch STALE_GENERATOR already reports the actionable state; comparing
        # against a chimera baseline would emit spurious managed-drift findings.
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


def _stale_generator_message(marker_version: str, running_version: str) -> str:
    """Advise refresh or an upgrade based on the marker and running version ordering."""
    detail = (
        f"managed artifact uses generator version {marker_version!r}, not {running_version!r}; "
    )
    try:
        marker_release = validate_final_release_version(marker_version)
        running_release = validate_final_release_version(running_version)
    except ConfigError:
        return detail + "run `doc-lattice ci refresh`"
    if marker_release > running_release:
        return detail + (
            f"upgrade your local doc-lattice to at least {marker_version} and rerun the audit"
        )
    return detail + "run `doc-lattice ci refresh`"


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
