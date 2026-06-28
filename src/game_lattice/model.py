"""Domain types for the lattice graph."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .constants import Authority, Layer, LocationKind


def split_ref(ref: str) -> str:
    """Return the stable id a ref points at.

    Args:
        ref: A ref written bare (``accent``) or namespaced (``art-direction#accent``).

    Returns:
        The trailing id after the last ``#``; the namespace prefix is display-only.
    """
    return ref.rsplit("#", 1)[-1]


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


@dataclass(frozen=True, slots=True)
class Edge:
    """A resolved derives_from edge. ``target_id`` is None when the ref is broken."""

    target_ref: str
    target_id: str | None
    seen: str | None

    @classmethod
    def resolve(cls, ref: str, seen: str | None, index: "Mapping[str, Location]") -> "Edge":
        """Build an edge, resolving the ref so target_ref and target_id cannot disagree.

        Args:
            ref: The derives_from ref as written.
            seen: The locked hash from frontmatter, or None if never reconciled.
            index: The id-to-Location index; a ref resolving to no id yields a broken edge.

        Returns:
            An Edge whose target_id is the resolved id, or None when the ref is broken.
        """
        target_id = split_ref(ref)
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

    ``index`` maps every stable id to a Location. ``dependents`` maps a target id
    to the set of source node ids that derive from it. ``ancestors`` maps a section
    anchor id to the anchored sections (outermost to innermost) whose spans contain it.
    ``file_id_by_path`` and ``anchors_by_path`` are path lookups precomputed by the loader
    so resolution, impact, and rendering avoid scanning the index per edge.

    The maps are typed ``Mapping`` to signal that the lattice is read-only once built;
    cross-map consistency is an invariant guaranteed by ``build_lattice``.
    """

    nodes_by_id: Mapping[str, Node]
    index: Mapping[str, Location]
    dependents: Mapping[str, frozenset[str]]
    ancestors: Mapping[str, tuple[str, ...]]
    file_id_by_path: Mapping[Path, str]
    anchors_by_path: Mapping[Path, frozenset[str]]
