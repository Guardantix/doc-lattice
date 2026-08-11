"""Plan reconcile updates: recompute upstream hashes and build exact-byte rewrites.

Nothing here touches the filesystem: ``plan_rewrites`` reads only through a reader its
caller injects, and ``reconcile_transaction`` owns durable publication of the rewrites
planned here.
"""

import json
from collections import defaultdict
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    Event,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from ruamel.yaml.tokens import (
    AnchorToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    KeyToken,
    ScalarToken,
    TagToken,
    Token,
    ValueToken,
)

from .error_types import BrokenRefError, UnreadableDocError, ValidationError
from .frontmatter_parser import YAML_LOAD_ERRORS, split_frontmatter_parts
from .hashing import normalize_newlines
from .model import Lattice, TargetId, parse_ref
from .resolve import cached_target_hash

# Characters that end or reinterpret a plain scalar in block or flow context, and the
# indicators that only do so in first position. A replacement carrying any of them is
# quoted rather than spliced in bare.
_PLAIN_SCALAR_UNSAFE = frozenset(",[]{}#:\n\t")
_PLAIN_SCALAR_UNSAFE_PREFIX = tuple("-?&*!|>'\"%@`")

# The scalar styles whose token opens on a header line of its own rather than on the value.
_BLOCK_SCALAR_STYLES = frozenset({"|", ">"})

# The tag the loader flattens a mapping key on, which the plain `<<` scalar resolves to.
_MERGE_TAG = "tag:yaml.org,2002:merge"


@dataclass(frozen=True, slots=True)
class Rewrite:
    """Describe one exact-byte reconcile rewrite.

    Attributes:
        path: Document identity path for the rewrite.
        before: Exact source bytes read before planning the rewrite.
        after: UTF-8 replacement bytes with planned updates applied.
        applied: Refs whose seen scalar changed.
    """

    path: Path
    before: bytes
    after: bytes
    applied: frozenset[str]


@dataclass(frozen=True, slots=True)
class _SourceEdit:
    start: int
    end: int
    replacement: str


@dataclass(frozen=True, slots=True)
class _ScalarOccurrence:
    start: int
    end: int
    column: int
    value: str
    style: str | None
    anchor: str | None
    tag: str | None


@dataclass(frozen=True, slots=True)
class _AnchoredSeen:
    """An anchored ``seen`` node being replaced, and the text an alias site inherits.

    Attributes:
        anchor: The anchor name the replaced node defines.
        start: Offset of the replaced node, which bounds the aliases still reading it.
        source: Complete replacement text for an alias site, the anchor included.
    """

    anchor: str
    start: int
    source: str


@dataclass(frozen=True, slots=True)
class _AliasOccurrence:
    start: int
    end: int
    anchor: str


@dataclass(frozen=True, slots=True)
class _MappingOccurrence:
    start: int
    end: int
    flow_style: bool
    anchor: str | None
    value: tuple[tuple["_YamlOccurrence", "_YamlOccurrence"], ...]


@dataclass(frozen=True, slots=True)
class _SequenceOccurrence:
    start: int
    end: int
    flow_style: bool
    anchor: str | None
    value: tuple["_YamlOccurrence", ...]


@dataclass(frozen=True, slots=True)
class _TokenSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _ScalarProperties:
    """The anchor and tag tokens written on one scalar, each where source spells it.

    Attributes:
        scalar_start: Offset of the scalar token the properties apply to.
        anchor: Span of its ``&name``, when it carries one.
        tag: Span of its explicit tag, when it carries one.
    """

    scalar_start: int
    anchor: _TokenSpan | None
    tag: _TokenSpan | None

    def spans(self) -> list[_TokenSpan]:
        """Return the property spans in source order."""
        return sorted(
            (span for span in (self.anchor, self.tag) if span is not None),
            key=lambda span: span.start,
        )


@dataclass(frozen=True, slots=True)
class _BlockIndent:
    start: int
    column: int


@dataclass(frozen=True, slots=True)
class _TokenMarks:
    """Source positions the parse events do not carry, read off the token stream instead.

    Attributes:
        scalar_spans: Span of each scalar token, which excludes the anchor, tag, and quotes
            or block header an event's own marks enclose.
        scalar_properties: The anchor and tag written on each scalar that carries either,
            which a node's own marks do not always enclose.
        block_mapping_indents: Opening offset and indentation column of each block mapping,
            which is where the mapping was opened rather than where its node starts.
        block_sequence_indents: The same for each block sequence, which is where an appended
            item's own ``-`` belongs.
        key_indicators: Offset at which each mapping key opens, in source order.
        value_indicators: Offset of each ``:`` that opens a mapping value.
    """

    scalar_spans: tuple[_TokenSpan, ...]
    scalar_properties: tuple[_ScalarProperties, ...]
    block_mapping_indents: tuple[_BlockIndent, ...]
    block_sequence_indents: tuple[_BlockIndent, ...]
    key_indicators: tuple[int, ...]
    value_indicators: tuple[int, ...]


type _YamlOccurrence = (
    _ScalarOccurrence | _AliasOccurrence | _MappingOccurrence | _SequenceOccurrence
)


@dataclass(frozen=True, slots=True)
class _AnchorIndex:
    """Every anchored occurrence and every alias in one frontmatter, keyed by anchor name.

    Resolving an alias asks for the definitions of one name, and relocating an anchored node
    asks for the aliases of one name. Both are answered from this index, built in a single
    pass, rather than by walking the whole occurrence tree per lookup: a mapping that spells
    its keys through aliases would otherwise pay one walk for every key of every lookup.

    Attributes:
        definitions: Non-alias occurrences carrying each anchor, in source order, since a
            reused name rebinds at each definition rather than naming one node.
        aliases: Alias occurrences reading each anchor.
    """

    definitions: dict[str, tuple[_YamlOccurrence, ...]]
    aliases: dict[str, tuple[_AliasOccurrence, ...]]


@dataclass(frozen=True, slots=True)
class _SourceContext:
    """The frontmatter source text and the three views of it the planners read."""

    raw_meta: str
    root: _YamlOccurrence
    anchors: _AnchorIndex
    marks: _TokenMarks
    version: tuple[int, int] | None


def _yaml() -> YAML:
    """Return a fresh safe loader for one read.

    Nothing here is dumped back, so a round-trip loader would only pay for state this module
    never uses. A loader is built per read rather than shared because a ``YAML`` instance
    carries document state: ``YAML.version`` is sticky, so a ``%YAML`` directive in one
    frontmatter would keep resolving scalars its way for every later read (exactly as
    ``frontmatter_parser`` guards against), and a shared instance binds its reader and scanner
    to one document at a time, so two concurrent callers would measure each other's offsets.
    """
    return YAML(typ="safe")


def _document_version(events: list[Event]) -> tuple[int, int] | None:
    """Return the YAML version this frontmatter declares, or None when it declares none."""
    for event in events:
        if isinstance(event, DocumentStartEvent):
            return event.version
    return None


def _parse_yaml_occurrence(events: list[Event], index: int) -> tuple[_YamlOccurrence, int]:
    event = events[index]
    if isinstance(event, ScalarEvent):
        return (
            _ScalarOccurrence(
                event.start_mark.index,
                event.end_mark.index,
                event.start_mark.column,
                event.value,
                event.style,
                event.anchor,
                event.tag,
            ),
            index + 1,
        )
    if isinstance(event, AliasEvent):
        return (
            _AliasOccurrence(event.start_mark.index, event.end_mark.index, event.anchor),
            index + 1,
        )
    if isinstance(event, MappingStartEvent):
        pairs: list[tuple[_YamlOccurrence, _YamlOccurrence]] = []
        next_index = index + 1
        while not isinstance(events[next_index], MappingEndEvent):
            key, next_index = _parse_yaml_occurrence(events, next_index)
            value, next_index = _parse_yaml_occurrence(events, next_index)
            pairs.append((key, value))
        end_event = events[next_index]
        return (
            _MappingOccurrence(
                event.start_mark.index,
                end_event.end_mark.index,
                bool(event.flow_style),
                event.anchor,
                tuple(pairs),
            ),
            next_index + 1,
        )
    if isinstance(event, SequenceStartEvent):
        values: list[_YamlOccurrence] = []
        next_index = index + 1
        while not isinstance(events[next_index], SequenceEndEvent):
            value, next_index = _parse_yaml_occurrence(events, next_index)
            values.append(value)
        end_event = events[next_index]
        return (
            _SequenceOccurrence(
                event.start_mark.index,
                end_event.end_mark.index,
                bool(event.flow_style),
                event.anchor,
                tuple(values),
            ),
            next_index + 1,
        )
    raise UnreadableDocError("frontmatter structure is malformed; cannot reconcile")


def _source_occurrence_tree(events: list[Event]) -> _YamlOccurrence:
    for index, event in enumerate(events):
        if isinstance(event, (ScalarEvent, AliasEvent, MappingStartEvent, SequenceStartEvent)):
            root, _ = _parse_yaml_occurrence(events, index)
            return root
    raise UnreadableDocError("frontmatter structure is malformed; cannot reconcile")


def _scalar_properties(tokens: list[Token]) -> tuple[_ScalarProperties, ...]:
    """Pair each scalar token with the anchor and tag written on it.

    A property applies to the node that follows it, and only the other property may come
    between the two, so a property followed by anything else belongs to a collection rather
    than to a scalar. The pairing is read off the token stream because a scalar's own marks
    do not delimit its properties: a node whose tag precedes its anchor starts at the anchor,
    leaving the tag outside it, and a comment may sit between either one and the value.
    """
    properties: list[_ScalarProperties] = []
    anchor: _TokenSpan | None = None
    tag: _TokenSpan | None = None
    for token in tokens:
        if isinstance(token, AnchorToken):
            anchor = _TokenSpan(token.start_mark.index, token.end_mark.index)
        elif isinstance(token, TagToken):
            tag = _TokenSpan(token.start_mark.index, token.end_mark.index)
        else:
            if (anchor is not None or tag is not None) and isinstance(token, ScalarToken):
                properties.append(_ScalarProperties(token.start_mark.index, anchor, tag))
            anchor = tag = None
    return tuple(properties)


def _token_marks(tokens: list[Token]) -> _TokenMarks:
    return _TokenMarks(
        tuple(
            _TokenSpan(token.start_mark.index, token.end_mark.index)
            for token in tokens
            if isinstance(token, ScalarToken)
        ),
        _scalar_properties(tokens),
        tuple(
            _BlockIndent(token.start_mark.index, token.start_mark.column)
            for token in tokens
            if isinstance(token, BlockMappingStartToken)
        ),
        tuple(
            _BlockIndent(token.start_mark.index, token.start_mark.column)
            for token in tokens
            if isinstance(token, BlockSequenceStartToken)
        ),
        tuple(token.start_mark.index for token in tokens if isinstance(token, KeyToken)),
        tuple(token.start_mark.index for token in tokens if isinstance(token, ValueToken)),
    )


def _walk_occurrences(occurrence: _YamlOccurrence) -> Iterator[_YamlOccurrence]:
    yield occurrence
    if isinstance(occurrence, _MappingOccurrence):
        for key, value in occurrence.value:
            yield from _walk_occurrences(key)
            yield from _walk_occurrences(value)
    elif isinstance(occurrence, _SequenceOccurrence):
        for value in occurrence.value:
            yield from _walk_occurrences(value)


def _build_anchor_index(root: _YamlOccurrence) -> _AnchorIndex:
    """Index every anchor definition and alias in one walk of the occurrence tree."""
    definitions: dict[str, list[_YamlOccurrence]] = defaultdict(list)
    aliases: dict[str, list[_AliasOccurrence]] = defaultdict(list)
    for occurrence in _walk_occurrences(root):
        if isinstance(occurrence, _AliasOccurrence):
            aliases[occurrence.anchor].append(occurrence)
        elif occurrence.anchor is not None:
            definitions[occurrence.anchor].append(occurrence)
    return _AnchorIndex(
        {
            anchor: tuple(sorted(found, key=lambda occurrence: occurrence.start))
            for anchor, found in definitions.items()
        },
        {anchor: tuple(found) for anchor, found in aliases.items()},
    )


def _resolve_occurrence(anchors: _AnchorIndex, occurrence: _YamlOccurrence) -> _YamlOccurrence:
    """Return the definition an alias binds to, or ``occurrence`` when it is not an alias.

    A frontmatter document may reuse an anchor name, in which case each alias binds to the
    most recent preceding definition rather than to the name as a whole.
    """
    if not isinstance(occurrence, _AliasOccurrence):
        return occurrence
    preceding = [
        definition
        for definition in anchors.definitions.get(occurrence.anchor, ())
        if definition.start < occurrence.start
    ]
    return preceding[-1] if preceding else occurrence


def _mapping_pair(
    anchors: _AnchorIndex, mapping: _MappingOccurrence, name: str
) -> tuple[_YamlOccurrence, _YamlOccurrence] | None:
    """Return the pair whose key names ``name``, following an alias spelling of that key.

    A key written as an alias is the string its definition holds, so the loaded mapping can
    carry a member that source never spells out. The pair keeps the mapping's own key
    occurrence rather than the definition it resolves to, since only that occurrence sits
    where an edit can be measured from.
    """
    for key, value in mapping.value:
        resolved = _resolve_occurrence(anchors, key)
        if isinstance(resolved, _ScalarOccurrence) and resolved.value == name:
            return key, value
    return None


def _is_merge_key(key: _YamlOccurrence) -> bool:
    """Report whether a mapping key is one the loader flattens into its mapping.

    The loader merges on a key's resolved tag, so an explicit ``!!merge`` spells a merge key
    on any scalar, while a quoted or otherwise tagged ``<<`` is an ordinary key it leaves
    alone. Only a plain ``<<`` resolves to the merge tag on its own.
    """
    if not isinstance(key, _ScalarOccurrence):
        return False
    if key.tag is not None:
        return key.tag == _MERGE_TAG
    return key.style is None and key.value == "<<"


def _merge_source_mappings(
    anchors: _AnchorIndex, value: _YamlOccurrence
) -> list[_MappingOccurrence]:
    candidates = list(value.value) if isinstance(value, _SequenceOccurrence) else [value]
    resolved = [_resolve_occurrence(anchors, candidate) for candidate in candidates]
    return [candidate for candidate in resolved if isinstance(candidate, _MappingOccurrence)]


def _resolved_mapping_member(
    anchors: _AnchorIndex,
    mapping: _MappingOccurrence,
    name: str,
    visited: frozenset[int] = frozenset(),
) -> _YamlOccurrence | None:
    """Look ``name`` up in ``mapping``, following aliases and ``<<`` merge keys.

    The loaded data resolves aliases and merge keys for free, so the source tree has to
    follow them too or an edge reachable in the data would have no editable source.
    """
    if mapping.start in visited:
        return None
    pair = _mapping_pair(anchors, mapping, name)
    if pair is not None:
        return _resolve_occurrence(anchors, pair[1])
    for key, value in mapping.value:
        if not _is_merge_key(key):
            continue
        for source in _merge_source_mappings(anchors, value):
            member = _resolved_mapping_member(anchors, source, name, visited | {mapping.start})
            if member is not None:
                return member
    return None


def _omap_member(
    anchors: _AnchorIndex, sequence: _SequenceOccurrence, name: str
) -> _YamlOccurrence | None:
    """Look ``name`` up among the one-pair mappings an ordered map is written as.

    The loader builds an ordered map from its items directly rather than through mapping
    construction, so it never flattens a merge inside one and refuses a document that writes
    one there. No merge is followed here for that reason: following one would find a member
    no document reaching this far can carry.
    """
    for item in sequence.value:
        resolved = _resolve_occurrence(anchors, item)
        # The loader refuses an ordered map holding an item that is not a one-pair mapping,
        # so a document reaching this far has none.
        if not isinstance(resolved, _MappingOccurrence):  # pragma: no cover
            continue
        pair = _mapping_pair(anchors, resolved, name)
        if pair is not None:
            return _resolve_occurrence(anchors, pair[1])
    return None


def _resolved_member(
    anchors: _AnchorIndex, node: _MappingOccurrence | _SequenceOccurrence, name: str
) -> _YamlOccurrence | None:
    """Look ``name`` up in a node the loader reads as a mapping, however it is written.

    An ``!!omap`` loads as a mapping while its source is a sequence of one-pair mappings, so
    a member of one has to be found among those items rather than in a mapping that is not
    there.
    """
    if isinstance(node, _MappingOccurrence):
        return _resolved_mapping_member(anchors, node, name)
    return _omap_member(anchors, node, name)


def _inherited_seen_origin(
    anchors: _AnchorIndex,
    mapping: _MappingOccurrence,
    updated: frozenset[int],
    visited: frozenset[int] = frozenset(),
) -> int | None:
    """Return the offset of the mapping a ``<<`` merge will supply ``seen`` from after a rewrite.

    A merge key gives a mapping the members it does not spell itself, so editing one entry's
    ``seen`` changes every entry inheriting it, and the rewrite has to expect that. The loader
    takes the first source that provides the member, which after a rewrite means the first
    that either spells ``seen`` in source or is having one written into it. The caller has
    already established that ``mapping`` spells no ``seen`` of its own.
    """
    if mapping.start in visited:
        return None
    for key, value in mapping.value:
        if not _is_merge_key(key):
            continue
        for source in _merge_source_mappings(anchors, value):
            if source.start in updated or _mapping_pair(anchors, source, "seen") is not None:
                return source.start
            deeper = _inherited_seen_origin(anchors, source, updated, visited | {mapping.start})
            if deeper is not None:
                return deeper
    return None


def _value_indicator_after(marks: _TokenMarks, key_end: int, limit: int) -> int | None:
    """Return the offset of the ``:`` opening the value of the key ending at ``key_end``.

    Only the scanner's own indicators are reliable here: a colon inside a comment on an
    explicit key is not one. The search stops at the next key, so a key written without any
    indicator, which is a valid spelling of a null value, cannot claim the following pair's.
    """
    bound = min(next((index for index in marks.key_indicators if index > key_end), limit), limit)
    return next((index for index in marks.value_indicators if key_end <= index < bound), None)


def _null_seen_source_edit(
    context: _SourceContext,
    entry: _MappingOccurrence,
    seen_key: _YamlOccurrence,
    new_seen: str,
) -> _SourceEdit:
    raw_meta = context.raw_meta
    colon_at = _value_indicator_after(context.marks, seen_key.end, entry.end)
    if colon_at is None:
        return _missing_value_indicator_edit(context, entry, seen_key, new_seen)
    value_start = colon_at + 1
    boundary = entry.end - 1 if entry.flow_style else entry.end
    markers = [raw_meta.find(marker, value_start, entry.end) for marker in ("#", "\n", ",", "}")]
    value_end = min((marker for marker in markers if marker != -1), default=boundary)
    suffix = " " if value_end < len(raw_meta) and raw_meta[value_end] == "#" else ""
    return _SourceEdit(value_start, value_end, f" {new_seen}{suffix}")


def _missing_value_indicator_edit(
    context: _SourceContext,
    entry: _MappingOccurrence,
    seen_key: _YamlOccurrence,
    new_seen: str,
) -> _SourceEdit:
    """Plan the edit for an explicit ``seen`` key written without a value indicator.

    ``? seen`` on its own is a key whose value is null, so the hash needs a ``:`` written
    for it rather than written after one. A flow entry takes the indicator directly after
    the key; a block entry needs it to open the next line at the mapping's own indentation,
    which is also where any comment trailing the key stays behind.
    """
    if entry.flow_style:
        return _SourceEdit(seen_key.end, seen_key.end, f": {new_seen}")
    insert_at = _line_start_after(context.raw_meta, seen_key.end)
    indent = " " * _block_indent(context.marks.block_mapping_indents, entry)
    return _SourceEdit(insert_at, insert_at, f"{indent}: {new_seen}\n")


def _flow_has_trailing_comma(raw_meta: str, final_member_end: int, closing_bracket: int) -> bool:
    trailing_content = raw_meta[final_member_end:closing_bracket]
    # This begins after the final parsed member, so hashes in this segment introduce comments.
    without_comments = "".join(line.partition("#")[0] for line in trailing_content.splitlines())
    return "," in without_comments


def _final_member_end(entry: _MappingOccurrence | _SequenceOccurrence) -> int:
    """Return the offset just past a flow collection's last parsed member."""
    if isinstance(entry, _MappingOccurrence):
        return entry.value[-1][1].end
    return entry.value[-1].end


def _scalar_span(
    occurrence: _ScalarOccurrence, scalar_spans: tuple[_TokenSpan, ...]
) -> _TokenSpan | None:
    matches = [
        span
        for span in scalar_spans
        if occurrence.start <= span.start and span.end <= occurrence.end
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise UnreadableDocError("frontmatter derives_from entry seen is malformed")
    return matches[0]


def _scalar_source_end(raw_meta: str, occurrence: _ScalarOccurrence, span: _TokenSpan) -> int:
    if occurrence.style in _BLOCK_SCALAR_STYLES and raw_meta[span.end - 1 : span.end] == "\n":
        return span.end - 1
    return span.end


def _block_header_comment(raw_meta: str, occurrence: _ScalarOccurrence, span: _TokenSpan) -> str:
    """Return the comment written on a block scalar's header line, its own spacing included.

    A block scalar's token opens at its indicator and closes past its contents, so a comment
    written on the header sits inside the span a replacement overwrites, unlike the comment
    after a plain or quoted scalar, which sits outside it and survives untouched. The header
    carries nothing but the style, chomping and indentation indicators, so the first ``#`` on
    that line opens a comment. Its text moves onto the replacement's own line, which is where
    a one-line hash leaves it, and reload verification cannot see it go if it does not.
    """
    if occurrence.style not in _BLOCK_SCALAR_STYLES:
        return ""
    line_end = raw_meta.find("\n", span.start)
    header = raw_meta[span.start : len(raw_meta) if line_end == -1 else line_end]
    comment_at = header.find("#")
    if comment_at == -1:
        return ""
    indicators = header[:comment_at].rstrip()
    # A comment opens on whitespace, so the run before it is the author's own spacing; the
    # fallback covers a header no scanner accepts rather than emitting a hash bare.
    return f"{header[len(indicators) : comment_at] or ' '}{header[comment_at:]}"


def _probe_loader(version: tuple[int, int] | None) -> YAML:
    """Return the loader the round-trip probe for a document's replacement scalars reads with.

    One loader serves every entry of a document: the probe input is a bare scalar this module
    generated rather than user frontmatter, so it carries no directive that could make
    ``YAML.version`` sticky, and the version each probe needs is the document's own, which is
    fixed for the whole call.
    """
    loader = _yaml()
    loader.version = version
    return loader


def _seen_scalar_source(new_seen: str, loader: YAML) -> str:
    """Return source text for ``new_seen`` that reloads as the same string.

    An all-digit hash reloads as an integer when spliced in bare, which fails frontmatter
    validation on the next read, so anything that does not round-trip plain is quoted. The
    round-trip is tested under the document's own YAML version, since a 1.1 document reads
    scalars such as ``y`` and ``on`` as booleans that 1.2 leaves as strings.
    """
    if (
        new_seen
        and new_seen == new_seen.strip()
        and not (_PLAIN_SCALAR_UNSAFE & set(new_seen))
        and not new_seen.startswith(_PLAIN_SCALAR_UNSAFE_PREFIX)
    ):
        try:
            if loader.load(new_seen) == new_seen:
                return new_seen
        except YAML_LOAD_ERRORS:
            pass
    return json.dumps(new_seen, ensure_ascii=False)


def _properties_of(marks: _TokenMarks, span: _TokenSpan) -> _ScalarProperties | None:
    """Return the anchor and tag written on the scalar token at ``span``, if it carries either."""
    return next(
        (found for found in marks.scalar_properties if found.scalar_start == span.start), None
    )


def _explicit_tag_span(marks: _TokenMarks, span: _TokenSpan) -> _TokenSpan | None:
    """Return the span of the explicit tag on the scalar token at ``span``, if it carries one."""
    properties = _properties_of(marks, span)
    return None if properties is None else properties.tag


def _anchored_seen_source(
    context: _SourceContext,
    seen: _ScalarOccurrence,
    span: _TokenSpan | None,
    value: object,
) -> str:
    """Return source text that reproduces an anchored ``seen`` node at an alias site.

    The text carries the anchor, and an explicit tag along with it: re-emitting the scalar
    alone would leave the implicit resolver to retype the value on the next read. Strings are
    re-emitted quoted so a relocated block scalar stays on one line, and a tag that made the
    value a string is redundant once it is; every other scalar keeps its own source, since
    rendering it through ``str`` would retype it too. Each part is taken from its own token
    rather than sliced whole, because a line break between them would put the relocated value
    on a line of its own.
    """
    anchor = f"&{seen.anchor}"
    if value is None:
        return f"{anchor} null"
    if isinstance(value, bool):
        return f"{anchor} {'true' if value else 'false'}"
    if not isinstance(value, str) and span is not None:
        source = context.raw_meta[span.start : _scalar_source_end(context.raw_meta, seen, span)]
        if "\n" not in source:
            tag_span = _explicit_tag_span(context.marks, span)
            if tag_span is None:
                return f"{anchor} {source}"
            return f"{anchor} {context.raw_meta[tag_span.start : tag_span.end]} {source}"
    return f"{anchor} {json.dumps(str(value), ensure_ascii=False)}"


def _is_implicit_null(occurrence: _YamlOccurrence) -> bool:
    """Report whether an occurrence is the empty plain scalar YAML reads as null."""
    return (
        isinstance(occurrence, _ScalarOccurrence)
        and occurrence.style is None
        and occurrence.value == ""
    )


def _entry_content_end(marks: _TokenMarks, occurrence: _YamlOccurrence) -> int:
    """Return the offset just past an occurrence's final scalar or alias token.

    A block collection's own end mark is the *next* token's offset, which for a sequence
    entry is the following item's dash, so the last leaf is the only reliable anchor for
    an append. A flow collection ends at its own closing bracket, and recursing past that
    into its final leaf would anchor the append inside a collection that is still open. A
    null value carries that same next-token mark and holds no source of its own beyond the
    ``:`` that opens it, so a key whose value is empty anchors the append at that indicator,
    or at the key itself when the value is spelled without one.
    """
    if isinstance(occurrence, (_MappingOccurrence, _SequenceOccurrence)) and occurrence.flow_style:
        return occurrence.end
    if isinstance(occurrence, _MappingOccurrence) and occurrence.value:
        key, value = occurrence.value[-1]
        if not _is_implicit_null(value):
            return _entry_content_end(marks, value)
        indicator = _value_indicator_after(marks, key.end, occurrence.end)
        return key.end if indicator is None else indicator + 1
    if isinstance(occurrence, _SequenceOccurrence) and occurrence.value:
        return _entry_content_end(marks, occurrence.value[-1])
    return occurrence.end


def _line_start_after(raw_meta: str, index: int) -> int:
    if index > 0 and raw_meta[index - 1] == "\n":
        return index
    newline = raw_meta.find("\n", index)
    return len(raw_meta) if newline == -1 else newline + 1


def _block_indent(
    indents: tuple[_BlockIndent, ...], entry: _MappingOccurrence | _SequenceOccurrence
) -> int:
    """Return the column a block collection's own members are indented to.

    A node's start mark is its first character, which is not the member column whenever an
    anchor or a tag precedes the first one, nor when an explicit-key entry starts at its `?`
    indicator one indent short of the key. The scanner opens the collection at its
    indentation in every one of those shapes, so its own mark is the only column an appended
    key or item can safely take.
    """
    for indent in indents:
        if entry.start <= indent.start < entry.end:
            return indent.column
    # The caller has already narrowed to a non-flow collection, and the scanner opens every
    # one of those, so this refuses a shape only a mark-accounting change could produce.
    msg = "frontmatter derives_from entry is malformed; cannot reconcile"
    raise UnreadableDocError(msg)  # pragma: no cover


def _property_removal_edits(context: _SourceContext, span: _TokenSpan) -> list[_SourceEdit]:
    """Plan the edits that drop the properties of a ``seen`` scalar being replaced.

    Neither property can survive the replacement: the anchor is relocated to whichever alias
    still needs the old value, and a leftover tag would retype the new hash on the next read.
    Each is removed from its own token rather than by overwriting one span reaching back from
    the value, because a comment may be written between a property and the value, or between
    two properties, and that comment is the author's. A removal therefore stops at the first
    comment ahead of it, and otherwise runs to the next property or to the value, so the run
    of space a property leaves behind goes with it when there is nothing there to keep.
    """
    properties = _properties_of(context.marks, span)
    if properties is None:
        return []
    spans = properties.spans()
    boundaries = [*(found.start for found in spans[1:]), span.start]
    edits = []
    for found, boundary in zip(spans, boundaries, strict=True):
        comment = context.raw_meta.find("#", found.end, boundary)
        edits.append(_SourceEdit(found.start, boundary if comment == -1 else comment, ""))
    return edits


@dataclass(frozen=True, slots=True)
class _SeenPair:
    """Where an entry's ``seen`` pair sits, resolved once for every planner that needs it.

    Attributes:
        mapping: The mapping the pair belongs among. An entry written as an ordered map is a
            sequence of one-pair mappings rather than a mapping, so its pair sits in a
            mapping of its own inside that sequence; that inner mapping is an ordinary one,
            so every edit is planned from here exactly as it is for a mapping entry.
        key: The pair's own key occurrence, which is where a missing ``:`` is written.
        value: The pair's value occurrence.
        alias: The item an ordered map spells the pair through, when it names one, rather
            than holding the pair itself.
    """

    mapping: _MappingOccurrence
    key: _YamlOccurrence
    value: _YamlOccurrence
    alias: _AliasOccurrence | None


def _seen_source_edits(
    context: _SourceContext,
    pair: _SeenPair,
    span: _TokenSpan | None,
    new_seen: str,
) -> list[_SourceEdit]:
    """Plan every edit one entry's ``seen`` needs: the value itself, and any property it drops."""
    seen = pair.value
    if isinstance(seen, _AliasOccurrence):
        return [_SourceEdit(seen.start, seen.end, new_seen)]
    if isinstance(seen, _ScalarOccurrence):
        if span is None:
            # An empty plain scalar has no source of its own, so the hash is written after
            # the key's own indicator rather than over the value.
            return [_null_seen_source_edit(context, pair.mapping, pair.key, new_seen)]
        comment = _block_header_comment(context.raw_meta, seen, span)
        value = _SourceEdit(
            span.start, _scalar_source_end(context.raw_meta, seen, span), f"{new_seen}{comment}"
        )
        return [*_property_removal_edits(context, span), value]
    raise UnreadableDocError("frontmatter derives_from entry seen is malformed")


def _appended_seen_edit(
    context: _SourceContext, entry: _MappingOccurrence | _SequenceOccurrence, new_seen: str
) -> _SourceEdit:
    """Plan the edit that adds the ``seen`` an entry does not carry yet.

    An entry written as an ordered map is a sequence of one-pair mappings rather than a
    mapping, so its new pair is appended as an item of its own rather than as a key.
    """
    raw_meta = context.raw_meta
    mapping = isinstance(entry, _MappingOccurrence)
    if entry.flow_style:
        insert_at = entry.end - 1
        if _flow_has_trailing_comma(raw_meta, _final_member_end(entry), insert_at):
            separator = "" if raw_meta[insert_at - 1].isspace() else " "
        else:
            separator = ", "
        member = f"seen: {new_seen}" if mapping else f"{{seen: {new_seen}}}"
        return _SourceEdit(insert_at, insert_at, f"{separator}{member}")
    insert_at = _line_start_after(raw_meta, _entry_content_end(context.marks, entry))
    indents = (
        context.marks.block_mapping_indents if mapping else context.marks.block_sequence_indents
    )
    member = f"seen: {new_seen}" if mapping else f"- seen: {new_seen}"
    return _SourceEdit(insert_at, insert_at, f"{' ' * _block_indent(indents, entry)}{member}\n")


def _mapping_alias_source_edit(
    raw_meta: str, alias: _AliasOccurrence, new_seen: str
) -> _SourceEdit:
    alias_source = raw_meta[alias.start : alias.end]
    return _SourceEdit(alias.start, alias.end, f"{{<<: {alias_source}, seen: {new_seen}}}")


def _omap_item_alias_source_edit(alias: _AliasOccurrence, new_seen: str) -> _SourceEdit:
    """Replace the alias an ordered map spells its ``seen`` pair through with that pair.

    The mapping such an alias names is shared with everything else reading that anchor, so
    editing it would rewrite a member this edge does not own. An ordered map item is a
    one-pair mapping and this one holds only ``seen``, so writing the pair out at the item
    keeps the entry's value exact while leaving the definition alone. A merge key cannot
    stand in here the way it does for a whole aliased entry, since the loader requires an
    ordered map item to hold exactly one pair and does not flatten merges inside one.
    """
    return _SourceEdit(alias.start, alias.end, f"{{seen: {new_seen}}}")


def _append_seen_anchor_relocations(
    anchors: _AnchorIndex,
    anchored_seen: list[_AnchoredSeen],
    edits: list[_SourceEdit],
) -> None:
    edited_spans = {(edit.start, edit.end) for edit in edits}
    for anchored in anchored_seen:
        # A reused anchor name rebinds at each definition, so only aliases between this
        # definition and the next one of the same name read the value being replaced.
        later_definitions = [
            definition.start
            for definition in anchors.definitions.get(anchored.anchor, ())
            if definition.start > anchored.start
        ]
        rebound_at = min(later_definitions, default=None)
        untouched_aliases = [
            alias
            for alias in anchors.aliases.get(anchored.anchor, ())
            if alias.start > anchored.start
            and (rebound_at is None or alias.start < rebound_at)
            and (alias.start, alias.end) not in edited_spans
        ]
        if untouched_aliases:
            alias = untouched_aliases[0]
            relocation = _SourceEdit(alias.start, alias.end, anchored.source)
            edits.append(relocation)
            edited_spans.add((relocation.start, relocation.end))


def _apply_source_edits(raw_meta: str, edits: list[_SourceEdit]) -> str:
    # Two entries can only ever plan the same span from the same update, so keeping one
    # edit per span drops the repeat rather than choosing between rival replacements.
    unique_edits = {(edit.start, edit.end): edit for edit in edits}
    for edit in sorted(unique_edits.values(), key=lambda item: item.start, reverse=True):
        raw_meta = raw_meta[: edit.start] + edit.replacement + raw_meta[edit.end :]
    return raw_meta


def _derives_from_member(data: object) -> object:
    return data.get("derives_from") if isinstance(data, MutableMapping) else None


def _verify_reconciled_meta(new_meta: str, expected: object, source: Path) -> None:
    """Reject spliced frontmatter that does not reload as the intended document.

    Source edits are byte-level, so a mis-measured span could publish text that no longer
    parses or that silently changes a value. Relocating a ``seen`` anchor edits bytes
    outside ``derives_from``, so the whole reloaded document is compared rather than the
    edges alone. The commit transaction never re-reads what it stages, so this is the last
    point at which such a rewrite can be refused instead of written durably.
    """
    try:
        reloaded = _yaml().load(new_meta)
    except YAML_LOAD_ERRORS as exc:
        msg = f"reconcile would leave {source} unparseable, so nothing was rewritten: {exc}"
        raise UnreadableDocError(msg) from exc
    try:
        if reloaded == expected:
            return
        changed = (
            "derives_from entries"
            if _derives_from_member(reloaded) != _derives_from_member(expected)
            else "frontmatter outside derives_from"
        )
    except RecursionError as exc:
        # An anchor that contains its own alias loads as a cyclic document, which compares
        # without bound. An unverifiable rewrite is refused rather than published unchecked.
        msg = (
            f"frontmatter of {source} is self-referential, so a reconcile rewrite cannot be"
            " verified and nothing was rewritten"
        )
        raise UnreadableDocError(msg) from exc
    msg = f"reconcile would not reproduce the {changed} of {source}, so nothing was rewritten"
    raise UnreadableDocError(msg)


@dataclass(frozen=True, slots=True)
class _EntryEdit:
    """The source edits one entry's update needs, and the anchored value it displaces.

    Attributes:
        edits: Every edit the update plans, which is the value's own plus one per property
            the replacement drops. An entry already reading the new hash through an alias
            plans none.
        anchored: The anchored ``seen`` being replaced, when the entry carries one, for
            relocation to whichever alias still reads it.
    """

    edits: tuple[_SourceEdit, ...]
    anchored: _AnchoredSeen | None


@dataclass(frozen=True, slots=True)
class _EntryUpdate:
    """One entry's expected ``seen`` after the rewrite, and where to record it.

    Attributes:
        index: Position of the entry in the loaded ``derives_from`` list.
        new_seen: The value the reloaded entry has to hold.
        detached: Whether the rewrite gives this position a mapping of its own, which stops
            it sharing the object the loader gave it and its definition.
    """

    index: int
    new_seen: str
    detached: bool


@dataclass(frozen=True, slots=True)
class _ReconcilePlan:
    edits: tuple[_SourceEdit, ...]
    anchored_seen: tuple[_AnchoredSeen, ...]
    applied: frozenset[str]
    entry_updates: tuple[_EntryUpdate, ...]


def _load_frontmatter_data(raw_meta: str) -> object:
    try:
        return _yaml().load(raw_meta)
    except YAML_LOAD_ERRORS as exc:
        msg = f"cannot parse frontmatter to reconcile: {exc}"
        raise UnreadableDocError(msg) from exc


def _derives_from_occurrences(
    context: _SourceContext, entries: Sequence[object]
) -> _SequenceOccurrence:
    """Return the source occurrences matching the loaded ``derives_from`` entries.

    The caller has established that the document loads as a mapping carrying a
    ``derives_from`` list, which a root written as an ordered map does while its source is a
    sequence, so the member is resolved from whichever shape the root is written in.
    """
    root = context.root
    if not isinstance(root, (_MappingOccurrence, _SequenceOccurrence)):
        # Only a collection loads as the mapping the caller has already seen, so this refuses
        # a root shape only a mark-accounting change could produce.
        msg = "frontmatter source is not a mapping; cannot reconcile"
        raise UnreadableDocError(msg)  # pragma: no cover
    occurrences = _resolved_member(context.anchors, root, "derives_from")
    if not isinstance(occurrences, _SequenceOccurrence):
        raise UnreadableDocError("frontmatter derives_from source is not a list; cannot reconcile")
    if len(occurrences.value) != len(entries):  # pragma: no cover
        # The loaded list and the source list are two readings of the same text, so this
        # refuses a disagreement only a mark-accounting change could produce, rather than
        # measuring an entry's offsets against a different entry.
        msg = (
            f"frontmatter derives_from reads as {len(entries)} entries but is written as"
            f" {len(occurrences.value)}; cannot reconcile"
        )
        raise UnreadableDocError(msg)
    return occurrences


def _seen_pair(
    anchors: _AnchorIndex, entry: _MappingOccurrence | _SequenceOccurrence
) -> _SeenPair | None:
    """Return where an entry's ``seen`` pair is written, or None when it carries none yet."""
    if isinstance(entry, _MappingOccurrence):
        pair = _mapping_pair(anchors, entry, "seen")
        return None if pair is None else _SeenPair(entry, pair[0], pair[1], None)
    for item in entry.value:
        resolved = _resolve_occurrence(anchors, item)
        # The loader refuses an ordered map holding an item that is not a one-pair mapping,
        # so a document reaching this far has none.
        if not isinstance(resolved, _MappingOccurrence):  # pragma: no cover
            continue
        pair = _mapping_pair(anchors, resolved, "seen")
        if pair is not None:
            alias = item if isinstance(item, _AliasOccurrence) else None
            return _SeenPair(resolved, pair[0], pair[1], alias)
    return None


def _plan_entry_edit(
    context: _SourceContext,
    entry_occurrence: _MappingOccurrence | _SequenceOccurrence | _AliasOccurrence,
    old_seen: object,
    new_seen_source: str,
    updated_definitions: frozenset[int],
) -> _EntryEdit:
    """Plan the source edits that set one entry's ``seen`` scalar."""
    if isinstance(entry_occurrence, _AliasOccurrence):
        definition = _resolve_occurrence(context.anchors, entry_occurrence)
        # An entry written as an ordered map is a sequence, so a definition of either shape
        # is one this run may already have updated.
        if (
            isinstance(definition, (_MappingOccurrence, _SequenceOccurrence))
            and definition.start in updated_definitions
        ):
            # This entry aliases an entry the same run just updated, so it already reads the
            # new hash: leaving its source untouched keeps the author's alias intact.
            return _EntryEdit((), None)
        alias_edit = _mapping_alias_source_edit(context.raw_meta, entry_occurrence, new_seen_source)
        return _EntryEdit((alias_edit,), None)
    pair = _seen_pair(context.anchors, entry_occurrence)
    if pair is None:
        # An entry with no `seen` pair yet takes one as a key, or as an item of its own when
        # it is written as an ordered map.
        return _EntryEdit((_appended_seen_edit(context, entry_occurrence, new_seen_source),), None)
    if pair.alias is not None:
        return _EntryEdit((_omap_item_alias_source_edit(pair.alias, new_seen_source),), None)
    seen = pair.value
    span = (
        _scalar_span(seen, context.marks.scalar_spans)
        if isinstance(seen, _ScalarOccurrence)
        else None
    )
    anchored = None
    if isinstance(seen, _ScalarOccurrence) and seen.anchor is not None:
        anchored = _AnchoredSeen(
            seen.anchor,
            seen.start,
            _anchored_seen_source(context, seen, span, old_seen),
        )
    edits = _seen_source_edits(context, pair, span, new_seen_source)
    return _EntryEdit(tuple(edits), anchored)


def _plan_source_edits(
    context: _SourceContext,
    entries: Sequence[object],
    entry_occurrences: _SequenceOccurrence,
    updates: dict[str, str],
) -> _ReconcilePlan:
    """Plan every source edit the requested updates need, validating each entry's shape."""
    edits: list[_SourceEdit] = []
    anchored_seen: list[_AnchoredSeen] = []
    applied: set[str] = set()
    entry_updates: list[_EntryUpdate] = []
    updated_definitions: set[int] = set()
    loader = _probe_loader(context.version)
    for index, (entry, entry_occurrence) in enumerate(
        zip(entries, entry_occurrences.value, strict=True)
    ):
        # An `!!omap` entry loads as a mapping while its source is a sequence of one-pair
        # mappings, so a sequence occurrence is a mapping entry too and is edited as one.
        if not isinstance(entry, MutableMapping) or not isinstance(
            entry_occurrence, (_MappingOccurrence, _SequenceOccurrence, _AliasOccurrence)
        ):
            raise UnreadableDocError("frontmatter derives_from entry is not a mapping")
        ref = entry.get("ref")
        if not isinstance(ref, str):
            raise UnreadableDocError("frontmatter derives_from entry ref is not a string")
        old_seen = entry.get("seen")
        new_seen = updates.get(ref)
        if new_seen is None or old_seen == new_seen:
            continue
        entry_edit = _plan_entry_edit(
            context,
            entry_occurrence,
            old_seen,
            _seen_scalar_source(new_seen, loader),
            frozenset(updated_definitions),
        )
        edits.extend(entry_edit.edits)
        if entry_edit.anchored is not None:
            anchored_seen.append(entry_edit.anchored)
        if not isinstance(entry_occurrence, _AliasOccurrence):
            updated_definitions.add(entry_occurrence.start)
        applied.add(ref)
        entry_updates.append(
            _EntryUpdate(index, new_seen, isinstance(entry_occurrence, _AliasOccurrence))
        )
    entry_updates.extend(_merge_inherited_updates(context, entry_occurrences, entry_updates))
    return _ReconcilePlan(
        tuple(edits), tuple(anchored_seen), frozenset(applied), tuple(entry_updates)
    )


def _merge_inherited_updates(
    context: _SourceContext,
    entry_occurrences: _SequenceOccurrence,
    planned: Sequence[_EntryUpdate],
) -> list[_EntryUpdate]:
    """Return the updates an entry inherits from another through a ``<<`` merge key.

    An entry spelling no ``seen`` of its own reads whichever one its merge source holds, so
    editing that source changes this entry too even though nothing was written at it. The
    reload sees that, so the expectation has to as well or a rewrite that is exactly right
    would be refused. Entries reached through an alias need nothing here: the loader gives an
    alias and its definition one object, which a single assignment already updates.
    """
    updated = {update.index: update.new_seen for update in planned}
    origins = {
        entry_occurrences.value[update.index].start: update.new_seen
        for update in planned
        if isinstance(entry_occurrences.value[update.index], _MappingOccurrence)
    }
    if not origins:
        return []
    inherited: list[_EntryUpdate] = []
    for index, entry_occurrence in enumerate(entry_occurrences.value):
        if index in updated or not isinstance(entry_occurrence, _MappingOccurrence):
            continue
        if _mapping_pair(context.anchors, entry_occurrence, "seen") is not None:
            continue
        origin = _inherited_seen_origin(context.anchors, entry_occurrence, frozenset(origins))
        if origin in origins:
            inherited.append(_EntryUpdate(index, origins[origin], detached=False))
    return inherited


def _expected_frontmatter(raw_meta: str, entry_updates: tuple[_EntryUpdate, ...]) -> object:
    """Return the loaded document the rewritten frontmatter has to reproduce.

    An entry reached through an alias is the same loaded object as its definition, so
    assigning in place keeps every view of it in step with what the reload will see. An alias
    site the rewrite replaces with its own mapping is the one edit that does not propagate,
    so that position is detached from the shared object first. An entry inheriting through a
    merge key is a copy the loader flattened rather than a shared object, so it carries an
    update of its own, planned by ``_merge_inherited_updates``.
    """
    expected = _yaml().load(raw_meta)
    entries = expected["derives_from"]
    for update in entry_updates:
        entry = entries[update.index]
        if update.detached:
            entry = dict(entry)
            entries[update.index] = entry
        entry["seen"] = update.new_seen
    return expected


def reconcile(
    lattice: Lattice, downstream_id: str, *, ref: str | None, reconcile_all: bool
) -> dict[Path, dict[str, str]]:
    """Plan the seen-scalar updates needed to clear drift for the selection.

    Selection: when ``reconcile_all`` is True, every STALE and UNRECONCILED edge across
    the lattice is updated, narrowed to edges matching ``ref`` when one is given; BROKEN
    and already-OK edges are skipped, and no match is a successful no-op rather than an
    error.  When targeting a specific node, all of that node's STALE or UNRECONCILED
    edges are updated (or just the matching edge if ``ref`` is given); an already-OK edge
    is skipped in both modes, since restamping it to the same hash is a no-op.  The match
    uses the parsed TargetId so an identical ref selects the same edge.  A node's BROKEN
    edge is skipped (it does not block the node's reconcilable edges); only a single-node
    ``--ref`` aimed directly at a broken edge is refused, and a single-node ``--ref`` that
    matches no edge on the node is reported rather than silently doing nothing.

    Args:
        lattice: The built lattice (its upstream content is the reconcile snapshot).
        downstream_id: The node whose edges to reconcile (ignored if ``reconcile_all``).
        ref: A single upstream ref to narrow to, or None for all of the node's edges.
        reconcile_all: Reconcile every node's STALE or UNRECONCILED edges.

    Returns:
        A mapping of downstream file path to ``{target_ref: new_seen}`` updates. The
        caller applies these via ``apply_reconcile`` and an atomic write (the CLI does).

    Raises:
        ValidationError: If ``downstream_id`` is not in the lattice, or if ``ref`` is
            given but matches no edge on the node (both only when not ``reconcile_all``).
        BrokenRefError: If ``ref`` targets an edge that has no resolvable target.
    """
    if not reconcile_all and downstream_id not in lattice.nodes_by_id:
        raise ValidationError(f"unknown downstream id {downstream_id!r}; run check to list ids")
    node_ids = sorted(lattice.nodes_by_id) if reconcile_all else [downstream_id]
    requested_target_id = parse_ref(ref) if ref is not None else None
    targeting_specific_ref = ref is not None and not reconcile_all
    plan: dict[Path, dict[str, str]] = defaultdict(dict)
    cache: dict[TargetId, str] = {}
    ref_matched = False
    for node_id in node_ids:
        node = lattice.nodes_by_id[node_id]
        for edge in node.derives_from:
            if requested_target_id is not None:
                # A broken edge has no resolved target_id, so parse its raw ref to compare.
                # Without this fallback a --ref aimed straight at a broken edge would match
                # nothing and get the generic no-such-edge error instead of the
                # BrokenRefError below.
                edge_target_id = (
                    edge.target_id if edge.target_id is not None else parse_ref(edge.target_ref)
                )
                if edge_target_id != requested_target_id:
                    continue
            ref_matched = True
            if edge.target_id is None:
                if targeting_specific_ref:
                    raise BrokenRefError(
                        f"cannot reconcile broken ref {edge.target_ref!r} on {node_id};"
                        " fix the ref first"
                    )
                continue
            new_seen = cached_target_hash(lattice, edge.target_id, cache)
            if edge.seen is not None and new_seen == edge.seen:
                continue
            plan[node.path][edge.target_ref] = new_seen
    if targeting_specific_ref and not ref_matched:
        raise ValidationError(
            f"node {downstream_id!r} has no edge matching ref {ref!r}; run check to list its edges"
        )
    return dict(plan)


def apply_reconcile(
    current_file_text: str, updates: dict[str, str], source: Path
) -> tuple[str, set[str]]:
    """Return ``current_file_text`` with matching edges' seen scalars set.

    The fresh read is parsed defensively: a concurrent edit that leaves the frontmatter
    unparseable or in an unexpected shape (not a mapping, a non-list ``derives_from``, a
    non-mapping entry, a non-string entry ``ref``, a ``seen`` that is a collection rather
    than a scalar) raises ``UnreadableDocError`` (a ``ProjectError``) so the CLI exits
    cleanly instead of crashing with a traceback.

    Args:
        current_file_text: A fresh read of the downstream file at write time.
        updates: ``{target_ref: new_seen}`` for edges in this file.
        source: The downstream file the frontmatter came from, for error messages.

    Returns:
        A pair of the rewritten file text and the set of refs from ``updates`` whose
        ``seen`` was changed; a ref already holding its planned value is left untouched
        and excluded from the set. When nothing changed (for example a ref was edited
        away between load and write, or already held the planned hash) the original text
        is returned unchanged and the set is empty, so the caller does not report a write
        that did not happen. Everything around the edited frontmatter is reattached verbatim
        from ``current_file_text``: a leading byte-order mark, both fences as they were
        written, and the body after the closing one.

    Raises:
        UnreadableDocError: If the fresh frontmatter cannot be parsed or is malformed, or
            if the planned source edits would not reproduce the intended frontmatter.
    """
    parts = split_frontmatter_parts(current_file_text, source)
    if parts is None:
        return current_file_text, set()
    raw_meta = parts.raw_meta
    data = _load_frontmatter_data(raw_meta)
    if data is None:
        return current_file_text, set()
    if not isinstance(data, MutableMapping):
        raise UnreadableDocError("frontmatter is not a mapping; cannot reconcile")
    entries = data.get("derives_from")
    if entries is None:
        return current_file_text, set()
    if not isinstance(entries, list):
        raise UnreadableDocError("frontmatter derives_from is not a list; cannot reconcile")
    events = list(_yaml().parse(raw_meta))
    source_root = _source_occurrence_tree(events)
    context = _SourceContext(
        raw_meta,
        source_root,
        _build_anchor_index(source_root),
        _token_marks(list(_yaml().scan(raw_meta))),
        _document_version(events),
    )
    entry_occurrences = _derives_from_occurrences(context, entries)
    plan = _plan_source_edits(context, entries, entry_occurrences, updates)
    if not plan.applied:
        return current_file_text, set()
    edits = list(plan.edits)
    _append_seen_anchor_relocations(context.anchors, list(plan.anchored_seen), edits)
    new_meta = _apply_source_edits(raw_meta, edits)
    _verify_reconciled_meta(new_meta, _expected_frontmatter(raw_meta, plan.entry_updates), source)
    rewritten = (
        f"{parts.prefix}{parts.open_fence}\n{new_meta}"
        f"{parts.close_fence}{parts.close_fence_newline}{parts.body}"
    )
    return rewritten, set(plan.applied)


def _line_ending(text: str) -> str:
    """Return the ending to restore to spliced text, or LF when there is nothing to restore.

    ``apply_reconcile`` measures source offsets against LF text, so a file written with
    another ending is normalized to plan against and restored to its own ending afterwards.
    A file that mixes endings has no single ending to restore, so normalizing it is the
    outcome, which is what the hashes have always compared anyway.
    """
    if "\r\n" in text and not set(text.replace("\r\n", "")) & {"\r", "\n"}:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def plan_rewrites(
    plan: dict[Path, dict[str, str]],
    read_bytes: Callable[[Path], bytes],
) -> list[Rewrite]:
    """Compute exact-byte fresh-read reconcile rewrites before any write lands.

    The injected reader retains the exact source bytes for later fingerprinting and
    restoration while UTF-8 text is decoded and newline-normalized only for
    ``apply_reconcile``. A file written entirely in CRLF or in lone CR is rewritten in that
    same ending, so updating one ``seen`` does not restyle every other line.

    Args:
        plan: The planned mapping of downstream file path to ``{ref: new_seen}``.
        read_bytes: Reader injected by the caller for fresh downstream file bytes.

    Returns:
        Rewrite records for files whose fresh content changed. Files whose planned
        updates are already applied are skipped.

    Raises:
        UnreadableDocError: If the injected reader cannot read a downstream file, or
            if the fresh frontmatter cannot be parsed or is malformed.
    """
    rewrites: list[Rewrite] = []
    for path, updates in plan.items():
        try:
            before = read_bytes(path)
            decoded = before.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read {path} to reconcile: {exc}"
            raise UnreadableDocError(msg) from exc
        new_text, applied = apply_reconcile(normalize_newlines(decoded), updates, path)
        if applied:
            ending = _line_ending(decoded)
            after = new_text if ending == "\n" else new_text.replace("\n", ending)
            rewrites.append(Rewrite(path, before, after.encode("utf-8"), frozenset(applied)))
    return rewrites
