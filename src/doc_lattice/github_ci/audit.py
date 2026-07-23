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
from .path_display import display_path
from .render import (
    BOOTSTRAP_EOL_RULE,
    CANONICAL_ARTIFACT_TARGETS,
    LINEAR_JOB_ID,
    LINEAR_SECRET_ENV_NAME,
    LINEAR_SECRET_ENV_VALUE,
    LINEAR_WORKFLOW_PATH,
    render_workflows,
)
from .shell_scanner import direct_doc_lattice_invocations
from .workflow_parser import parse_workflow

__all__ = [
    "SECRET_NAMES",
    "audit_global_workflows",
    "audit_managed_installation",
    "audit_repository",
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
_SECRET_NAMES_CASEFOLDED = frozenset(name.casefold() for name in SECRET_NAMES)

# Build the reference alternation from SECRET_NAMES so a new secret extends key and value
# detection together. Longest-first ordering keeps the alternation from partially matching a
# name that is a prefix of a longer one.
_SECRET_NAME_ALTERNATION = "|".join(
    re.escape(name) for name in sorted(SECRET_NAMES, key=len, reverse=True)
)
_SECRET_NAME_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{_SECRET_NAME_ALTERNATION})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_SECRETS_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])secrets(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_STATIC_SECRET_DOT_RE = re.compile(r"\s*\.\s*[A-Za-z_][A-Za-z0-9_]*")
_STATIC_SECRET_INDEX_RE = re.compile(r"\s*\[\s*'[A-Za-z_][A-Za-z0-9_]*'\s*\]")
_STATIC_SHELL_TEMPLATE_RE = re.compile(r"[A-Za-z0-9_./{} \t-]+")
_BASH_SHELL_EXECUTABLES = frozenset(
    {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"}
)
_BASH_LONG_FLAGS = frozenset({"--noprofile", "--norc"})
_BASH_SHORT_FLAGS = frozenset({"e", "n", "u", "v", "x"})
_BASH_O_OPTIONS = frozenset({"errexit", "nounset", "pipefail", "xtrace"})
# Keep this exact and fail closed for labels outside GitHub's documented standard runners.
_BASH_DEFAULT_RUNNERS = frozenset(
    {
        "ubuntu-slim",
        "ubuntu-latest",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-26.04",
        "ubuntu-22.04-arm",
        "ubuntu-24.04-arm",
        "ubuntu-26.04-arm",
        "macos-latest",
        "macos-14",
        "macos-15",
        "macos-26",
        "macos-15-intel",
        "macos-26-intel",
    }
)
_SCRIPT_PLACEHOLDER = "{0}"
# The run-body placeholder must be inert: it stands in for the ``{0}`` script argument when the
# shell template is scanned, so it must not itself match the doc-lattice marker and make every
# placeholder-bearing template fail the inverted unresolved-command certification rule.
_SCRIPT_SENTINEL = "__run_body_script__"
_CANONICAL_LINEAR_PATH = LINEAR_WORKFLOW_PATH.as_posix()
_WORKFLOW_DIRECTORY = LINEAR_WORKFLOW_PATH.parent.as_posix()
_COMMAND_BEHAVIOR_FIELDS = frozenset(
    {
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
)
_JOB_FIELD_PATH_LENGTH = 3
_JOB_ENV_PATH_LENGTH = 4
_STEP_FIELD_PATH_LENGTH = 5
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
    "MANAGED_ATTRIBUTES": "managed bootstrap line-ending policy differs from the canonical rule",
}


def audit_repository(
    discovery: WorkflowDiscovery,
    installed: tuple[InstalledArtifact | None, ...],
    identity: RepositoryIdentity,
    running_version: str,
) -> tuple[AuditFinding, ...]:
    """Compose repository-global and managed audits into one deterministic result.

    This owns the "findings are deterministic, sorted, and unique" contract so callers only
    render the merged result.

    Args:
        discovery: Parsed repository workflow discovery state.
        installed: Read-only inspection results aligned with expected managed artifacts.
        identity: Explicit or origin-derived repository identity for this audit.
        running_version: Current generator version used to diagnose stale installations.

    Returns:
        Deterministically sorted unique findings across both audit layers.

    Raises:
        ConfigError: If a shell scan cannot complete or inspection results are misaligned.
    """
    inspected_discovery = _bind_inspected_workflow_snapshot(discovery, installed)
    findings = [
        *audit_global_workflows(inspected_discovery.documents),
        *_audit_managed_installation(
            inspected_discovery,
            installed,
            identity,
            running_version,
        ),
    ]
    return _sorted_unique(findings)


def _bind_inspected_workflow_snapshot(
    discovery: WorkflowDiscovery,
    installed: tuple[InstalledArtifact | None, ...],
) -> WorkflowDiscovery:
    """Make inspected canonical workflow text authoritative for both audit layers."""
    _validate_installed_artifacts(installed)
    documents = {document.path.as_posix(): document for document in discovery.documents}
    directory_exists = discovery.directory_exists
    for expected, artifact in zip(CANONICAL_ARTIFACT_TARGETS, installed, strict=True):
        if expected.relative_path.parent.as_posix() != _WORKFLOW_DIRECTORY:
            continue
        path = expected.relative_path.as_posix()
        if artifact is None:
            documents.pop(path, None)
            continue
        directory_exists = True
        documents[path] = parse_workflow(Path(path), artifact.text)
    return WorkflowDiscovery(
        directory_exists=directory_exists,
        documents=tuple(documents[path] for path in sorted(documents)),
    )


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
                    invocations.extend(_pr_step_invocations(document, job, step))
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


def _pr_step_invocations(
    document: WorkflowDocument,
    job: WorkflowJob,
    step: WorkflowStep,
) -> tuple[tuple[str, bool], ...]:
    """Return direct invocations under the effective shell for one PR run step."""
    context = (
        f"{display_path(document.path)} job {job.job_id!r} step {step.index}: "
        "unsupported shell semantics for pull-request run step"
    )
    shell = step.shell or job.default_shell or document.default_shell
    if shell is None:
        if not _default_run_shell_is_bash(job.runs_on):
            raise ConfigError(context)
        return direct_doc_lattice_invocations(
            step.run or "",
            context=display_path(document.path),
        )

    template = shell.replace(_SCRIPT_PLACEHOLDER, _SCRIPT_SENTINEL)
    template_invocations = direct_doc_lattice_invocations(
        template,
        context=display_path(document.path),
    )
    if _supports_bash_run_body(shell):
        return (
            *template_invocations,
            *direct_doc_lattice_invocations(
                step.run or "",
                context=display_path(document.path),
            ),
        )
    if template_invocations:
        return template_invocations
    raise ConfigError(context)


def _default_run_shell_is_bash(runs_on: str | None) -> bool:
    if runs_on is None:
        return False
    return runs_on.casefold() in _BASH_DEFAULT_RUNNERS


def _supports_bash_run_body(shell: str) -> bool:
    stripped = shell.strip()
    if stripped in {"bash", "sh"}:
        return True
    if _STATIC_SHELL_TEMPLATE_RE.fullmatch(stripped) is None:
        return False
    words = stripped.split()
    if (
        not words
        or words[0] not in _BASH_SHELL_EXECUTABLES
        or words[-1] != _SCRIPT_PLACEHOLDER
        or words.count(_SCRIPT_PLACEHOLDER) != 1
    ):
        return False
    return _supports_bash_options(words[0], words[1:-1])


def _supports_bash_options(executable: str, options: list[str]) -> bool:
    bash = executable.endswith("bash")
    index = 0
    while index < len(options):
        option = options[index]
        if bash and option in _BASH_LONG_FLAGS:
            index += 1
            continue
        if not option.startswith("-") or option.startswith("--") or option == "-":
            return False
        cluster = option[1:]
        if "o" not in cluster:
            if not set(cluster) <= _BASH_SHORT_FLAGS:
                return False
            index += 1
            continue
        if not bash or cluster.count("o") != 1 or not cluster.endswith("o"):
            return False
        if not set(cluster[:-1]) <= _BASH_SHORT_FLAGS or index + 1 >= len(options):
            return False
        if options[index + 1] not in _BASH_O_OPTIONS:
            return False
        index += 2
    return True


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
        ConfigError: If inspection results do not use the canonical four-slot order.
    """
    inspected_discovery = _bind_inspected_workflow_snapshot(discovery, installed)
    return _audit_managed_installation(
        inspected_discovery,
        installed,
        repository,
        running_version,
    )


def _audit_managed_installation(
    discovery: WorkflowDiscovery,
    installed: tuple[InstalledArtifact | None, ...],
    repository: RepositoryIdentity,
    running_version: str,
) -> tuple[AuditFinding, ...]:
    """Audit an already validated and snapshot-bound managed installation."""
    canonical = CANONICAL_ARTIFACT_TARGETS

    findings: list[AuditFinding] = []
    if not discovery.directory_exists:
        findings.append(
            AuditFinding(
                path=_WORKFLOW_DIRECTORY,
                code="MISSING_WORKFLOW_DIRECTORY",
                message="managed GitHub workflow directory is missing",
            )
        )

    documents_by_path = {document.path.as_posix(): document for document in discovery.documents}
    # Render and parse each expected workflow at most once per (repository, version) within this
    # audit call, so multiple installed artifacts do not each re-render both workflows.
    expected_documents: dict[tuple[str, str], dict[ArtifactRole, WorkflowDocument]] = {}
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
                expected_documents,
            )
        )
    return _sorted_unique(findings)


def _validate_installed_artifacts(
    installed: tuple[InstalledArtifact | None, ...],
) -> None:
    """Require managed inspection results in the canonical four-slot order."""
    canonical = CANONICAL_ARTIFACT_TARGETS
    if len(installed) != len(canonical):
        raise ConfigError("managed artifact inspection must contain exactly four slots")
    for index, artifact in enumerate(installed):
        if artifact is None:
            continue
        expected = canonical[index]
        if (
            artifact.expected.role != expected.role
            or artifact.expected.relative_path != expected.relative_path
        ):
            raise ConfigError(
                "managed artifact inspection must use canonical order: "
                "offline, linear, bootstrap, attributes"
            )


def _audit_installed_artifact(
    artifact: InstalledArtifact,
    documents_by_path: dict[str, WorkflowDocument],
    repository: RepositoryIdentity,
    running_version: str,
    expected_documents: dict[tuple[str, str], dict[ArtifactRole, WorkflowDocument]],
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
    if artifact.expected.role == "attributes":
        if _effective_git_attribute_rules(artifact.text) != (BOOTSTRAP_EOL_RULE,):
            findings.append(
                AuditFinding(
                    path=path,
                    code="MANAGED_ATTRIBUTES",
                    message=_MANAGED_MESSAGES["MANAGED_ATTRIBUTES"],
                )
            )
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
    expected_document = _expected_documents_for(
        expected_documents,
        marker.repository,
        marker.version,
    )[artifact.expected.role]
    findings.extend(
        AuditFinding(path=path, code=code, message=_MANAGED_MESSAGES[code])
        for code in _managed_semantic_codes(document, expected_document)
    )
    return findings


def _effective_git_attribute_rules(text: str) -> tuple[str, ...]:
    """Return nonempty, non-comment rules while accepting Git's line separators."""
    return tuple(line for line in text.splitlines() if line and not line.startswith("#"))


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


def _expected_documents_for(
    cache: dict[tuple[str, str], dict[ArtifactRole, WorkflowDocument]],
    repository: RepositoryIdentity,
    version: str,
) -> dict[ArtifactRole, WorkflowDocument]:
    """Return the expected workflow documents for one repository and version, memoized."""
    key = (repository.display, version)
    documents = cache.get(key)
    if documents is None:
        documents = _render_expected_documents(repository, version)
        cache[key] = documents
    return documents


def _render_expected_documents(
    repository: RepositoryIdentity,
    version: str,
) -> dict[ArtifactRole, WorkflowDocument]:
    """Render and parse the expected managed workflows keyed by their role."""
    workflows = render_workflows(repository.display, version)
    return {
        artifact.role: parse_workflow(Path(artifact.relative_path.as_posix()), artifact.text)
        for artifact in workflows
    }


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

    # Classify step drift exactly once per aligned job so both the semantic step rules and the
    # structural step comparison reuse the same result instead of each re-running it.
    job_pairs = tuple(zip(document.jobs, expected.jobs, strict=True))
    step_drift = [
        _classify_step_drift(job.steps, expected_job.steps) for job, expected_job in job_pairs
    ]
    for drift, (job, expected_job) in zip(step_drift, job_pairs, strict=True):
        if job.permissions != expected_job.permissions:
            codes.add("MANAGED_PERMISSIONS")
        if job.if_condition != expected_job.if_condition:
            codes.add("MANAGED_COMMAND")
        if job.environment != expected_job.environment or job.runs_on != expected_job.runs_on:
            codes.add("MANAGED_JOB")
        if job.env != expected_job.env:
            codes.add("MANAGED_COMMAND")
        codes.update(_managed_step_codes(job, expected_job, drift))

    if _has_linear_secret_reference(document):
        codes.add("MANAGED_SECRET")
    codes.update(_managed_structure_codes(document, expected, step_drift))
    return frozenset(codes)


def _classify_step_drift(
    current_steps: tuple[WorkflowStep, ...],
    desired_steps: tuple[WorkflowStep, ...],
) -> tuple[set[str], tuple[WorkflowStep, ...]]:
    """Classify uses and run drift between two step sequences.

    This is the single owner of the cache-step exclusion, the uses-sequence comparison, the
    run-sequence comparison, and the cache-versus-action classification, so a step count
    mismatch and an aligned drift are diagnosed by the same logic. Cache steps are excluded
    from the action comparison and are reported through their own code.

    Args:
        current_steps: Steps discovered in the installed workflow.
        desired_steps: Steps of the expected rendered workflow.

    Returns:
        The managed drift codes implied by the uses and run sequences, together with the
        installed steps with cache steps excluded so callers reuse that one filtered view.
    """
    codes: set[str] = set()
    current_without_cache = tuple(
        step for step in current_steps if _action_name(step.uses) != "actions/cache"
    )
    if len(current_without_cache) != len(current_steps):
        codes.add("MANAGED_CACHE")
    if _uses_sequence(current_without_cache) != _uses_sequence(desired_steps):
        codes.add("MANAGED_ACTION")
    if _run_sequence(current_steps) != _run_sequence(desired_steps):
        codes.add("MANAGED_COMMAND")
    return codes, current_without_cache


def _uses_sequence(steps: tuple[WorkflowStep, ...]) -> tuple[str, ...]:
    return tuple(step.uses for step in steps if step.uses is not None)


def _run_sequence(steps: tuple[WorkflowStep, ...]) -> tuple[str, ...]:
    return tuple(step.run for step in steps if step.run is not None)


def _managed_step_codes(
    job: WorkflowJob,
    expected: WorkflowJob,
    drift: tuple[set[str], tuple[WorkflowStep, ...]],
) -> set[str]:
    drift_codes, current_steps_without_cache = drift
    codes = set(drift_codes)
    expected_kinds = tuple(_step_kind(step) for step in expected.steps)
    current_kinds = tuple(_step_kind(step) for step in current_steps_without_cache)
    if (
        current_kinds != expected_kinds
        and _uses_sequence(current_steps_without_cache) == _uses_sequence(expected.steps)
        and _run_sequence(job.steps) == _run_sequence(expected.steps)
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
    step_drift: list[tuple[set[str], tuple[WorkflowStep, ...]]],
) -> set[str]:
    codes: set[str] = set()
    all_current = _structure_map(document.structure)
    all_desired = _structure_map(expected.structure)
    current = {path: value for path, value in all_current.items() if not _is_step_path(path)}
    desired = {path: value for path, value in all_desired.items() if not _is_step_path(path)}
    for path in current.keys() | desired.keys():
        if current.get(path) != desired.get(path):
            codes.add(_structure_code(path, current, desired))

    for job, expected_job, drift in zip(document.jobs, expected.jobs, step_drift, strict=True):
        drift_codes = drift[0]
        if len(job.steps) != len(expected_job.steps):
            codes.update(drift_codes)
            if not drift_codes:
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
) -> dict[tuple[str, ...], tuple[str, str | None]]:
    return {
        entry.path: (entry.kind, entry.value)
        for entry in structure
        if not _is_display_name_path(entry.path)
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
    return any(component in _COMMAND_BEHAVIOR_FIELDS for component in path)


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
    if path and path[-1].casefold() in _SECRET_NAMES_CASEFOLDED:
        return True
    return any(
        value is not None and value[1] is not None and _SECRET_NAME_RE.search(value[1]) is not None
        for value in (current.get(path), desired.get(path))
    )


def _has_linear_secret_reference(document: WorkflowDocument) -> bool:
    exempt_path = _canonical_linear_secret_path(document)
    for scalar in document.scalars:
        if scalar.path == exempt_path and scalar.value == LINEAR_SECRET_ENV_VALUE:
            continue
        if (
            _is_reusable_workflow_secret_inheritance(scalar.path, scalar.value)
            or _SECRET_NAME_RE.search(scalar.value) is not None
            or _has_unsafe_secret_context_access(scalar.value)
        ):
            return True

    for entry in document.structure:
        if entry.path == exempt_path and entry.value == LINEAR_SECRET_ENV_VALUE:
            continue
        if entry.path and entry.path[-1].casefold() in _SECRET_NAMES_CASEFOLDED:
            return True
    return False


def _is_reusable_workflow_secret_inheritance(path: tuple[str, ...], value: str) -> bool:
    """Return whether a reusable-workflow job forwards the whole secret context."""
    return (
        len(path) == _JOB_FIELD_PATH_LENGTH
        and path[0] == "jobs"
        and path[2] == "secrets"
        and value == "inherit"
    )


def _has_unsafe_secret_context_access(value: str) -> bool:
    """Return whether an expression can access more than one static unrelated secret."""
    cursor = 0
    while True:
        expression_start = value.find("${{", cursor)
        if expression_start < 0:
            return False
        index = expression_start + len("${{")
        while index < len(value):
            if value.startswith("}}", index):
                cursor = index + len("}}")
                break
            if value[index] == "'":
                index = _quoted_expression_string_end(value, index)
                continue
            token = _SECRETS_TOKEN_RE.match(value, index)
            if token is None:
                index += 1
                continue
            access_end = _static_secret_access_end(value, token.end())
            if access_end is None:
                return True
            trailing = access_end
            while trailing < len(value) and value[trailing].isspace():
                trailing += 1
            if trailing < len(value) and value[trailing] in ".[*":
                return True
            index = access_end
        else:
            return False


def _quoted_expression_string_end(value: str, start: int) -> int:
    index = start + 1
    while index < len(value):
        if value[index] != "'":
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "'":
            index += 2
            continue
        return index + 1
    return len(value)


def _static_secret_access_end(value: str, start: int) -> int | None:
    for pattern in (_STATIC_SECRET_DOT_RE, _STATIC_SECRET_INDEX_RE):
        if match := pattern.match(value, start):
            return match.end()
    return None


def _canonical_linear_secret_path(document: WorkflowDocument) -> tuple[str, ...] | None:
    if document.path.as_posix() != _CANONICAL_LINEAR_PATH:
        return None
    linear_job = next((job for job in document.jobs if job.job_id == LINEAR_JOB_ID), None)
    if linear_job is None or not linear_job.steps:
        return None
    final_step = linear_job.steps[-1]
    expected_pair = (LINEAR_SECRET_ENV_NAME, LINEAR_SECRET_ENV_VALUE)
    if expected_pair not in final_step.env:
        return None
    return (
        "jobs",
        LINEAR_JOB_ID,
        "steps",
        str(final_step.index),
        "env",
        LINEAR_SECRET_ENV_NAME,
    )


def _finding(document: WorkflowDocument, code: str, message: str) -> AuditFinding:
    return AuditFinding(path=document.path.as_posix(), code=code, message=message)


def _sorted_unique(findings: list[AuditFinding]) -> tuple[AuditFinding, ...]:
    return tuple(sorted(set(findings)))
