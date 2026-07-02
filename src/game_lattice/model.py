"""Domain types for the lattice graph."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import Authority, Layer, LocationKind


@dataclass(frozen=True, slots=True)
class TargetId:
    """A resolved target: a whole file, or a file-scoped section anchor.

    ``anchor`` is None for a whole-file target; otherwise it names a section inside
    ``file_id``. The two halves are separate fields, so a file target and a section target
    can never be confused and the ``#`` separator is not overloaded inside a key.
    """

    file_id: str
    anchor: str | None = None

    def as_ref(self) -> str:
        """Return the canonical ref string: ``file`` or ``file#anchor``."""
        return self.file_id if self.anchor is None else f"{self.file_id}#{self.anchor}"


def parse_ref(ref: str) -> TargetId:
    """Parse a derives_from ref into a file-scoped TargetId.

    A ref containing ``#`` is a section ref: it splits on the last ``#`` into a file id and
    an anchor. A bare ref is a whole-file target. Parsing never consults the index and never
    fails; whether the TargetId actually resolves is decided by index membership in
    ``Edge.resolve``.

    Args:
        ref: A derives_from ref as written (``save-format#slot-table`` or ``save-format``).

    Returns:
        The TargetId the ref names.
    """
    if "#" in ref:
        file_id, anchor = ref.rsplit("#", 1)
        return TargetId(file_id, anchor)
    return TargetId(ref)


class RawEdge(BaseModel):
    """One derives_from entry as written in frontmatter."""

    model_config = ConfigDict(strict=True, extra="forbid")

    ref: str
    seen: str | None = None


class NodeMeta(BaseModel):
    """Validated lattice frontmatter for one tracked file."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str
    title: str | None = None
    layer: Layer | None = None
    authority: Authority | None = None
    derives_from: list[RawEdge] = Field(default_factory=list)
    tickets: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_has_no_hash(cls, value: str) -> str:
        """Reject a ``#`` in a node id; it separates a file id from a section anchor in a ref."""
        if "#" in value:
            msg = (
                f"node id {value!r} must not contain '#'; "
                "'#' separates a file id from a section anchor"
            )
            raise ValueError(msg)
        return value


@dataclass(frozen=True, slots=True)
class Edge:
    """A resolved derives_from edge. ``target_id`` is None when the ref is broken."""

    target_ref: str
    target_id: TargetId | None
    seen: str | None

    @classmethod
    def resolve(cls, ref: str, seen: str | None, index: "Mapping[TargetId, Location]") -> "Edge":
        """Build an edge, resolving the ref so target_ref and target_id cannot disagree.

        Args:
            ref: The derives_from ref as written.
            seen: The locked hash from frontmatter, or None if never reconciled.
            index: The TargetId-to-Location index; a ref resolving to no id yields a broken edge.

        Returns:
            An Edge whose target_id is the resolved TargetId, or None when the ref is broken.
        """
        target_id = parse_ref(ref)
        return cls(target_ref=ref, target_id=target_id if target_id in index else None, seen=seen)


@dataclass(frozen=True, slots=True)
class Location:
    """Where an id lives. ``span`` is an inclusive 1-indexed line range."""

    path: Path
    kind: LocationKind
    span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Node:
    """One tracked file assembled from its frontmatter and body."""

    id: str
    title: str | None
    layer: Layer | None
    authority: Authority | None
    path: Path
    body: str
    derives_from: tuple[Edge, ...]
    tickets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """A discovered file with validated frontmatter and its raw body."""

    path: Path
    meta: NodeMeta
    body: str


@dataclass(frozen=True, slots=True)
class Lattice:
    """The whole derived graph.

    ``index`` maps every TargetId to a Location. ``dependents`` maps a target id
    to the set of source node ids that derive from it. ``ancestors`` maps a section
    anchor id to the anchored sections (outermost to innermost) whose spans contain it.
    ``file_id_by_path`` and ``anchors_by_path`` are path lookups precomputed by the loader
    so resolution, impact, and rendering avoid scanning the index per edge.

    The maps are typed ``Mapping`` to signal that the lattice is read-only once built;
    cross-map consistency is an invariant guaranteed by ``build_lattice``.
    """

    nodes_by_id: Mapping[str, Node]
    index: Mapping[TargetId, Location]
    dependents: Mapping[TargetId, frozenset[str]]
    ancestors: Mapping[TargetId, tuple[TargetId, ...]]
    file_id_by_path: Mapping[Path, str]
    anchors_by_path: Mapping[Path, frozenset[TargetId]]
