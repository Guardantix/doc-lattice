"""Domain types for the lattice graph."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .constants import Authority, Layer, LocationKind


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
    """

    nodes_by_id: dict[str, Node]
    index: dict[str, Location]
    dependents: dict[str, frozenset[str]]
    ancestors: dict[str, tuple[str, ...]]
