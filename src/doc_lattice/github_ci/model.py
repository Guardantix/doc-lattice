"""Typed models shared by GitHub CI rendering, audit, and filesystem adapters."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

ArtifactRole = Literal["offline", "linear", "bootstrap"]
ArtifactAction = Literal["current", "create", "replace"]
TriggerShape = Literal["null", "mapping", "sequence"]
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
class ManagedArtifact:
    """A rendered GitHub CI artifact managed by the generator."""

    role: ArtifactRole
    relative_path: PurePosixPath
    text: str


@dataclass(frozen=True, slots=True)
class InstalledArtifact:
    """A present managed artifact with its expected form and ownership inspection."""

    expected: ManagedArtifact
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
class WorkflowStep:
    """A normalized GitHub Actions workflow step."""

    index: int
    step_id: str | None
    name: str | None
    uses: str | None
    run: str | None
    env: tuple[tuple[str, str], ...]
    with_values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    """A normalized GitHub Actions workflow job."""

    job_id: str
    if_condition: str | None
    environment: str | None
    runs_on: str | None
    permissions: PermissionValue
    env: tuple[tuple[str, str], ...]
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True, slots=True)
class WorkflowDocument:
    """A normalized GitHub Actions workflow document."""

    path: Path
    triggers: tuple[WorkflowTrigger, ...]
    permissions: PermissionValue
    jobs: tuple[WorkflowJob, ...]
    scalars: tuple[WorkflowScalar, ...]


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
