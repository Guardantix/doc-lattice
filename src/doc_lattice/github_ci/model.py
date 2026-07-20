"""Typed models shared by GitHub CI rendering, audit, and filesystem adapters."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, get_args

from doc_lattice.constants import (
    AuditDiagnosticCode,
    AuditSourceKind,
    BlockScanStatus,
    ScanReasonCategory,
)

ArtifactRole = Literal["offline", "linear", "bootstrap", "attributes"]
ArtifactAction = Literal["current", "create", "replace"]

VALID_ARTIFACT_ROLES: frozenset[str] = frozenset(get_args(ArtifactRole))

# Single source of the managed-artifact ownership grammar. The renderer builds every
# header line from these constants and the filesystem adapter parses installed markers
# with them, so writer and parser can never silently diverge.
MANAGED_SCHEMA_VERSION = "github-ci-v1"
MANAGED_MARKER_PREFIX = "# doc-lattice-managed:"
ARTIFACT_MARKER_PREFIX = "# doc-lattice-artifact:"
VERSION_MARKER_PREFIX = "# doc-lattice-version:"
REPOSITORY_MARKER_PREFIX = "# doc-lattice-repository:"
MANAGED_SCHEMA_LINE = f"{MANAGED_MARKER_PREFIX} {MANAGED_SCHEMA_VERSION}"
MARKER_PREFIXES = (
    MANAGED_MARKER_PREFIX,
    ARTIFACT_MARKER_PREFIX,
    VERSION_MARKER_PREFIX,
    REPOSITORY_MARKER_PREFIX,
)
BOOTSTRAP_SHEBANG = "#!/usr/bin/env bash"
TriggerShape = Literal["null", "mapping", "sequence"]
WorkflowStructureKind = Literal[
    "mapping",
    "sequence",
    "null",
    "string",
    "boolean",
    "integer",
    "float",
]
PermissionValue = str | tuple[tuple[str, str], ...] | None


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """A validated GitHub.com owner/repository identity."""

    display: str
    comparison_key: str


@dataclass(frozen=True, slots=True)
class ManagedMarker:
    """Validated ownership metadata parsed from one managed GitHub CI artifact."""

    role: ArtifactRole
    version: str
    repository: RepositoryIdentity


@dataclass(frozen=True, slots=True)
class ManagedArtifactTarget:
    """One fixed managed artifact identity used for read-only inspection."""

    role: ArtifactRole
    relative_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class ManagedArtifact(ManagedArtifactTarget):
    """A rendered GitHub CI artifact managed by the generator."""

    text: str


@dataclass(frozen=True, slots=True)
class InstalledArtifact:
    """A present managed artifact with its expected form and ownership inspection."""

    expected: ManagedArtifactTarget
    text: str
    marker: ManagedMarker | None
    marker_error: str | None


@dataclass(frozen=True, slots=True)
class ArtifactChange:
    """A planned filesystem change for one managed artifact."""

    artifact: ManagedArtifact
    root: Path
    destination: Path
    action: ArtifactAction
    before: bytes | None


@dataclass(frozen=True, slots=True)
class WorkflowTrigger:
    """A normalized workflow trigger and its optional branch filters."""

    name: str
    shape: TriggerShape
    branches: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class WorkflowScalar:
    """A string scalar discovered at a YAML path in a workflow."""

    path: tuple[str, ...]
    value: str


@dataclass(frozen=True, slots=True)
class WorkflowStructureEntry:
    """One deterministic typed YAML value at a workflow path."""

    path: tuple[str, ...]
    kind: WorkflowStructureKind
    value: str | None


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """A normalized GitHub Actions workflow step."""

    index: int
    step_id: str | None
    name: str | None
    uses: str | None
    run: str | None
    shell: str | None
    env: tuple[tuple[str, str], ...]
    with_values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    """A normalized GitHub Actions workflow job."""

    job_id: str
    if_condition: str | None
    environment: str | None
    runs_on: str | None
    default_shell: str | None
    permissions: PermissionValue
    env: tuple[tuple[str, str], ...]
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True, slots=True)
class WorkflowDocument:
    """A normalized GitHub Actions workflow document."""

    path: Path
    triggers: tuple[WorkflowTrigger, ...]
    permissions: PermissionValue
    default_shell: str | None
    jobs: tuple[WorkflowJob, ...]
    scalars: tuple[WorkflowScalar, ...]
    structure: tuple[WorkflowStructureEntry, ...]


@dataclass(frozen=True, slots=True)
class WorkflowDiscovery:
    """Read-only discovery state for the repository workflow directory."""

    directory_exists: bool
    documents: tuple[WorkflowDocument, ...]


@dataclass(frozen=True, slots=True, order=True)
class AuditFinding:
    """An ordered policy finding emitted by the local audit."""

    path: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BlockScan:
    """Certification outcome for one execution source under the D4 contract.

    Invariants (spec D4): not_applicable carries no invocations and no reason; certified
    carries no reason; uninspectable requires reason, reason_category, and offset, and may
    retain invocations proven before the failure (monotonic evidence).
    """

    status: BlockScanStatus
    invocations: tuple[tuple[str, bool], ...]
    reason_category: ScanReasonCategory | None
    reason: str | None
    offset: int | None
    work_charged: int

    def __post_init__(self) -> None:
        """Enforce the D4 status invariants at construction time."""
        if self.status == "not_applicable" and (
            self.invocations or self.reason is not None or self.reason_category is not None
        ):
            raise ValueError("not_applicable blocks carry no invocations and no reason")
        if self.status == "certified" and (
            self.reason is not None or self.reason_category is not None
        ):
            raise ValueError("certified blocks carry no reason")
        if self.status == "uninspectable" and (
            self.reason is None or self.reason_category is None or self.offset is None
        ):
            raise ValueError("uninspectable blocks require reason, category, and offset")


@dataclass(frozen=True, slots=True)
class AuditDiagnostic:
    """One uninspectability or composition diagnostic attributed to an execution source."""

    path: str
    job_id: str
    step_index: int
    source_kind: AuditSourceKind
    code: AuditDiagnosticCode
    reason: str
    offset: int | None


def diagnostic_sort_key(diagnostic: AuditDiagnostic) -> tuple[str, str, int, str, str, int]:
    """Return the D5 deterministic ordering key; diagnostics without offsets sort first."""
    return (
        diagnostic.path,
        diagnostic.job_id,
        diagnostic.step_index,
        diagnostic.source_kind,
        diagnostic.code,
        -1 if diagnostic.offset is None else diagnostic.offset,
    )


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Aggregated repository audit outcome under the D5 contract."""

    findings: tuple[AuditFinding, ...]
    diagnostics: tuple[AuditDiagnostic, ...]
