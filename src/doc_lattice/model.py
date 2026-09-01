"""Domain types for the lattice graph."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from .constants import Authority, FrontmatterDisposition, Layer, LocationKind
from .text_utils import first_control_index


def _reject_control_chars(value: str) -> str:
    """Refuse a frontmatter string carrying a terminal control character.

    Args:
        value: One frontmatter scalar, as YAML constructed it.

    Returns:
        The value unchanged when it holds no control character.

    Raises:
        ValueError: If the value holds a C0 control, DEL, or a C1 control. The message names
            the code point and its position rather than echoing the value, so the diagnostic
            cannot carry the character it is refusing. A line break is answered with a fix that
            says how to make the value single-line, because a break is the one member of the
            refused set an author reaches by accident. Which fix depends on where the break is,
            not on how the value was spelled, which this function cannot see: a trailing break is
            what clip or keep chomping leaves behind, while an interior one survives every
            chomping mode and needs the lines joined instead. Answering both with the chomping
            advice would send an author of a multi-line ``|-`` to a ``-`` that is already there.
            The interior fix states the two conditions folding needs rather than naming ``>-``
            alone, because a ``>-`` keeps an interior break when a blank line separates its
            lines or one is indented further than the block, and an author already spelling
            ``>-`` would otherwise read the advice as a step they had taken.
    """
    index = first_control_index(value)
    if index is not None:
        if value[index] != "\n":
            fix = "remove it, since the value reaches terminal output as written"
        elif index == len(value) - 1:
            fix = (
                "frontmatter values are single-line, so drop the trailing line break; '|-' or "
                "'>-' chomps one that a block scalar would keep"
            )
        else:
            fix = (
                "frontmatter values are single-line, so join the lines; '>-' folds a block "
                "scalar's lines with spaces only where they are equally indented and no blank "
                "line separates them"
            )
        msg = (
            f"must not contain a control character; found U+{ord(value[index]):04X} at index "
            f"{index}: {fix}"
        )
        raise ValueError(msg)
    return value


ControlFreeStr = Annotated[str, AfterValidator(_reject_control_chars)]
"""A frontmatter string that carries no terminal control character.

YAML refuses a literal control byte in the source stream but decodes a double-quoted
``\\u001b`` into a real ESC, so every frontmatter string this engine keeps is constrained here
rather than at the sinks that print it. See AD-35.
"""


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

    ref: ControlFreeStr
    seen: ControlFreeStr | None = None


class NodeMeta(BaseModel):
    """Validated lattice frontmatter for one tracked file."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: ControlFreeStr
    title: ControlFreeStr | None = None
    layer: Layer | None = None
    authority: Authority | None = None
    derives_from: list[RawEdge] = Field(default_factory=list)
    tickets: list[ControlFreeStr] = Field(default_factory=list)

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
class CollisionMember:
    """One heading in a slug-collision component, ready to name in any sink.

    ``label`` is already sanitized by ``text_utils.safe_heading_label`` at derivation time, so
    the cached and uncached paths cannot disagree about it. ``line`` is the heading's 1-based
    line in the file, envelope included, because every sink prints it to a reader opening that
    file. It is not a body offset and is not comparable with a ``SectionRecord`` span, which
    stays body-relative for slicing.
    """

    label: str
    line: int


def format_collision_members(members: tuple[CollisionMember, ...]) -> str:
    """Render a collision component's members as the one comma-joined phrase every sink prints.

    This is the single owner of the human-readable phrase fragment naming collision members;
    every sink that lists them calls this instead of re-deriving the join.

    Args:
        members: The component's headings in document order.

    Returns:
        A comma-joined listing of each member's quoted label and line, with no leading verb.
    """
    return ", ".join(f'"{member.label}" (line {member.line})' for member in members)


def format_collision(members: tuple[CollisionMember, ...]) -> str:
    """Render a collision component as the one phrase every sink prints.

    Args:
        members: The component's headings in document order.

    Returns:
        A single-line phrase naming each member and its line. The caller applies its own
        quoting; the labels carry no control character, so the phrase is safe to embed.
    """
    return f"ambiguous with {format_collision_members(members)}"


def collision_members_json(members: tuple[CollisionMember, ...]) -> list[dict[str, str | int]]:
    """Render a collision component's members as the one JSON wire shape every sink emits.

    This is the single owner of the JSON representation of collision members; every sink that
    serializes them calls this instead of re-deriving the shape.

    Args:
        members: The component's headings in document order.

    Returns:
        A list of ``{"label": ..., "line": ...}`` dicts, one per member, in document order.
    """
    return [{"label": member.label, "line": member.line} for member in members]


@dataclass(frozen=True, slots=True)
class SectionRecord:
    """One anchored section: its resolved anchor id, inclusive 1-indexed line span, and any
    slug-collision component it belongs to.

    ``collision`` is None for a section whose id is unambiguous, which includes every id set by
    an explicit ``{#anchor}`` marker: being reword-stable is what the marker is for.
    """

    anchor: str
    start: int
    end: int
    collision: tuple[CollisionMember, ...] | None = None


@dataclass(frozen=True, slots=True)
class FileSections:
    """The section derivation build_lattice consumes: total line count and anchored spans."""

    total_lines: int
    sections: tuple[SectionRecord, ...]


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
class ParsedMeta:
    """What one discovered file's frontmatter turned out to be, and whether it is a node.

    Carrying the disposition alongside the node is what lets a skip stay describable after the
    parse: a caller can tell prose apart from a metadata block missing its ``id`` without
    re-reading the file, and the load cache can persist the distinction. The parse itself never
    reports the skip, so every load path renders the same diagnostic from one place.

    ``meta`` is not None exactly when ``disposition`` is ``"tracked"``.

    ``reused_anchors`` is the same kind of fact: a diagnostic the parse noticed and left for the
    caller to report. It is defaulted, unlike the cached form in ``cache.schema.Entry``, so the
    node-free outcomes stay shareable singletons. It is only ever set on a tracked node, because
    a rebound alias in a file the lattice does not hold changes no edge.
    """

    meta: NodeMeta | None
    disposition: FrontmatterDisposition
    reused_anchors: bool = False


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """A discovered file with validated frontmatter and its raw body.

    ``sections`` holds pre-derived section spans, which every production load path now fills:
    both the cached and the cache-free path derive them ahead of ``build_lattice`` because only
    they know where the body starts in the file, and that offset is what puts
    ``CollisionMember.line`` in file rather than body coordinates. It stays optional for a
    synthetic caller that builds a ``ParsedDoc`` by hand; ``build_lattice`` then derives sections
    itself with no such offset, so a hand-built doc whose body followed an envelope reports
    collision member lines short by the envelope's length.
    """

    path: Path
    meta: NodeMeta
    body: str
    sections: "FileSections | None" = None


@dataclass(frozen=True, slots=True)
class Lattice:
    """The whole derived graph.

    ``index`` maps every TargetId to a Location. ``dependents`` maps a target id
    to the set of source node ids that derive from it. ``ancestors`` maps a section
    anchor id to the anchored sections (outermost to innermost) whose spans contain it.
    ``file_id_by_path`` and ``anchors_by_path`` are path lookups precomputed by the loader
    so resolution, impact, and rendering avoid scanning the index per edge. ``collisions`` maps
    every section TargetId whose id sits in a slug-collision component to that component's
    members, so an edge into one can be classified and named without re-deriving anything.

    The maps are typed ``Mapping`` to signal that the lattice is read-only once built;
    cross-map consistency is an invariant guaranteed by ``build_lattice``.
    """

    nodes_by_id: Mapping[str, Node]
    index: Mapping[TargetId, Location]
    dependents: Mapping[TargetId, frozenset[str]]
    ancestors: Mapping[TargetId, tuple[TargetId, ...]]
    file_id_by_path: Mapping[Path, str]
    anchors_by_path: Mapping[Path, frozenset[TargetId]]
    collisions: Mapping[TargetId, tuple[CollisionMember, ...]] = field(default_factory=dict)
