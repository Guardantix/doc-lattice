"""Versioned Markdown heading and GitHub-slug compatibility adapter.

The supported Markdown subset is top-level, column-zero ATX headings plus CommonMark
backtick and tilde fences. ``markdown-it-py==4.2.0`` owns heading and fence recognition;
the local state adapter builds only the source maps those rules require. Generated data preserves
``github-slugger@2.0.0`` lowercase and strip behavior under JavaScript Unicode 17.0.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_block import fence as parse_fence
from markdown_it.rules_block import heading as parse_heading
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.token import Token
from markdown_it.utils import EnvType

from ._github_slugger_data import (
    CASE_IGNORABLE_PATTERN,
    CASED_PATTERN,
    JAVASCRIPT_UNICODE_VERSION,
    LOWERCASE_PATCH_TRANSLATION,
    SLUG_STRIP_PATTERN,
)
from .hashing import normalize_newlines

MARKDOWN_COMPAT_VERSION = "markdown-it-py==4.2.0"
SLUG_COMPAT_VERSION = "github-slugger@2.0.0"
SLUG_UNICODE_VERSION = JAVASCRIPT_UNICODE_VERSION

# Two trailing alternatives because the marker can sit at end of line or immediately before an
# ATX closing sequence. extract_headings searches markdown-it's parsed inline content, where the
# closing sequence has already been stripped, so the first branch applies there.
# strip_heading_anchor runs on the raw source line, which may still carry a closing "##"; the
# lookahead in the second branch matches the whitespace before it without consuming the "#"
# characters, so substitution removes only the marker and leaves the closing sequence intact.
_ANCHOR_RE = re.compile(r"(?:^|\s+)\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}(?:\s*$|\s+(?=#+\s*$))")
_CASED_RE = re.compile(CASED_PATTERN)
_CASE_IGNORABLE_RE = re.compile(CASE_IGNORABLE_PATTERN)
_SLUG_STRIP_RE = re.compile(SLUG_STRIP_PATTERN)
_HEADING_TOKEN_COUNT = 3
_GREEK_CAPITAL_SIGMA = "\u03a3"
_GREEK_SMALL_SIGMA = "\u03c3"
_GREEK_FINAL_SIGMA = "\u03c2"


@dataclass(frozen=True, slots=True)
class Heading:
    """One supported ATX heading with a 1-based source line."""

    level: int
    text: str
    anchor: str | None
    line: int


@dataclass(frozen=True, slots=True)
class SluggedHeading:
    """One heading in the full GitHub inventory, with the ids its allocation examined.

    ``probes`` is every candidate id the document-order deduplicator tried for this
    heading, its base slug first and each dedup suffix after, ending with the id it
    kept. Ambiguity is derived from the probes rather than from bases or final ids,
    because dedup examines ids it never emits, and an id a later heading merely probed
    is one a rename can hand it.

    ``level`` is the heading's nesting level whatever form it was written in, so a caller
    can reconstruct the outline a reader sees across forms the addressable TOC cannot.
    """

    text: str
    level: int
    line: int
    github_id: str
    probes: tuple[str, ...]


class _SourceMapState(StateBlock):
    """Minimal line-map state for markdown-it-py's pinned block rules.

    ``StateBlock`` scans every source character to support every CommonMark container.
    Doc-lattice supports only top-level ATX headings and fences, so this adapter builds
    the same fields by scanning line starts and indentation only. Recognition stays in
    markdown-it-py's unmodified ``heading`` and ``fence`` rules.
    """

    def __init__(self, src: str, md: MarkdownIt, env: EnvType, tokens: list[Token]) -> None:
        self.src = src
        self.md = md
        self.env = env
        self.tokens = tokens
        self.bMarks: list[int] = []
        self.eMarks: list[int] = []
        self.tShift: list[int] = []
        self.sCount: list[int] = []
        self.bsCount: list[int] = []
        self.blkIndent = 0
        self.line = 0
        self.lineMax = 0
        self.tight = False
        self.ddIndent = -1
        self.listIndent = -1
        self.parentType = "root"
        self.level = 0
        self.result = ""

        source_lines = src.split("\n")
        if source_lines and source_lines[-1] == "":
            source_lines.pop()
        start = 0
        for source_line in source_lines:
            indent = 0
            expanded_indent = 0
            for character in source_line:
                if character not in (" ", "\t"):
                    break
                indent += 1
                if character == "\t":
                    expanded_indent += 4 - expanded_indent % 4
                else:
                    expanded_indent += 1
            end = start + len(source_line)
            self.bMarks.append(start)
            self.eMarks.append(end)
            self.tShift.append(indent)
            self.sCount.append(expanded_indent)
            self.bsCount.append(0)
            start = end + 1

        length = len(src)
        self.bMarks.append(length)
        self.eMarks.append(length)
        self.tShift.append(0)
        self.sCount.append(0)
        self.bsCount.append(0)
        self.lineMax = len(self.bMarks) - 1
        self._code_enabled = True


_PARSER = MarkdownIt("commonmark")
# The same pinned parser with inline tokenization switched off. Every whole-document consumer in
# this module reads only block structure: heading source lines, an inline token's raw ``content``,
# and code-block spans. The ``inline`` core rule fills each inline token's ``children`` with a
# second tokenization of every paragraph in the document, which nothing here reads, and it is the
# dominant cost of a full parse. ``text_join`` is disabled with it because its only job is to
# merge adjacent text tokens inside those children, so with ``inline`` off it walks every token in
# the document to do nothing. Disabling both leaves ``Token.type``, ``Token.map``, and
# ``Token.content`` untouched, so the derived values are byte-identical.
_BLOCK_PARSER = MarkdownIt("commonmark")
_BLOCK_PARSER.core.ruler.disable(["inline", "text_join"])


def _normalize_for_parse(body: str) -> str:
    """Put document text into the exact form every parse in this module is fed.

    Line endings are normalized so source line numbers agree with the hashing model, and NUL is
    replaced with U+FFFD as CommonMark requires. Both steps belong to feeding the pinned parser,
    not to any one consumer, so every whole-document entry point here shares this one spelling:
    a scan that skipped either would disagree with its siblings about what the document says.

    Args:
        body: Markdown document text.

    Returns:
        The normalized text to hand to a parser or a line scan.
    """
    return normalize_newlines(body).replace("\0", "\ufffd")


def extract_headings(body: str) -> list[Heading]:
    """Extract the supported top-level ATX headings from Markdown.

    Args:
        body: Markdown document text.

    Returns:
        Headings in document order with raw inline content, trailing explicit anchor,
        and exact 1-based source line.

    Raises:
        RuntimeError: If the pinned parser returns a malformed heading token pair.
    """
    normalized = _normalize_for_parse(body)
    tokens: list[Token] = []
    state = _SourceMapState(normalized, _PARSER, {}, tokens)
    headings: list[Heading] = []
    line = 0
    # parse_fence and parse_heading append to the shared `tokens` list through StateBlock.push
    # rather than replacing it, so each branch clears the list once it has consumed the block.
    # Without that, tokens from earlier blocks accumulate and the exact-length check below stops
    # matching, so a second heading in the document would raise instead of being extracted.
    while line < state.lineMax:
        position = state.bMarks[line] + state.tShift[line]
        if position >= state.eMarks[line]:
            line += 1
            continue
        marker = state.src[position]
        if marker in ("`", "~") and parse_fence(state, line, state.lineMax, False):
            line = state.line
            tokens.clear()
            continue
        if marker != "#" or not parse_heading(state, line, state.lineMax, False):
            line += 1
            continue
        if (
            len(tokens) != _HEADING_TOKEN_COUNT
            or tokens[0].type != "heading_open"
            or tokens[1].type != "inline"
        ):
            msg = f"{MARKDOWN_COMPAT_VERSION} returned a malformed heading token pair"
            raise RuntimeError(msg)
        if state.tShift[line] == 0:
            text = tokens[1].content
            anchor_match = _ANCHOR_RE.search(text)
            headings.append(
                Heading(
                    level=len(tokens[0].markup),
                    text=text,
                    anchor=anchor_match.group(1) if anchor_match else None,
                    line=line + 1,
                )
            )
        line = state.line
        tokens.clear()
    return headings


def code_block_line_spans(body: str) -> list[tuple[int, int]]:
    """Return the 1-based inclusive line spans of every code block a render would show.

    Read from the pinned parser's full CommonMark parse rather than from the adapter's
    restricted heading scan, because the caller asks a rendering question ("would a reader see
    this as sample text") rather than an addressing one. Both fenced blocks and indented ones
    count, since either turns the text it holds into a quoted example.

    Args:
        body: Markdown document text.

    Returns:
        ``(start, end)`` line ranges in document order, both bounds inclusive.
    """
    normalized = _normalize_for_parse(body)
    spans: list[tuple[int, int]] = []
    for token in _BLOCK_PARSER.parse(normalized):
        if token.type in ("fence", "code_block") and token.map is not None:
            spans.append((token.map[0] + 1, token.map[1]))
    return spans


def _is_final_sigma(text: str, index: int) -> bool:
    for position in range(index - 1, -1, -1):
        character = text[position]
        if _CASE_IGNORABLE_RE.fullmatch(character):
            continue
        if not _CASED_RE.fullmatch(character):
            return False
        break
    else:
        return False

    for character in text[index + 1 :]:
        if _CASE_IGNORABLE_RE.fullmatch(character):
            continue
        return _CASED_RE.fullmatch(character) is None
    return True


def _lower_with_pinned_unicode(text: str) -> str:
    lowercase: list[str] = []
    for index, character in enumerate(text):
        if character == _GREEK_CAPITAL_SIGMA:
            lowercase.append(
                _GREEK_FINAL_SIGMA if _is_final_sigma(text, index) else _GREEK_SMALL_SIGMA
            )
            continue
        lowercase.append(character.lower().translate(LOWERCASE_PATCH_TRANSLATION))
    return "".join(lowercase)


def github_slug(text: str) -> str:
    """Return a github-slugger 2.0.0 base slug without deduplication.

    Args:
        text: Raw heading content.

    Returns:
        The JavaScript Unicode 17 lowercased, stripped, and space-replaced compatible slug.
    """
    lowercase = text.lower() if text.isascii() else _lower_with_pinned_unicode(text)
    return _SLUG_STRIP_RE.sub("", lowercase).replace(" ", "-")


class _Slugger:
    """Document-order slug deduplicator matching github-slugger 2.0.0.

    ``base_cache`` is an optional memo of the pure ``text -> base slug`` function, shared
    between the sluggers a single document builds. The two inventories see the same heading
    texts on any document written entirely in the addressable subset, which is the ordinary
    case, so without it every such heading is slugged twice. The dedup state is deliberately
    not shared: each inventory allocates ids over its own heading sequence.
    """

    def __init__(self, base_cache: dict[str, str] | None = None) -> None:
        self._seen: dict[str, int] = {}
        self._base_cache = base_cache

    def slug(self, text: str) -> str:
        """Return the next unique slug for heading content."""
        return self.slug_with_probes(text)[0]

    def _base(self, text: str) -> str:
        """Return the base slug for heading content, reusing the shared memo when there is one."""
        if self._base_cache is None:
            return github_slug(text)
        cached = self._base_cache.get(text)
        if cached is None:
            cached = github_slug(text)
            self._base_cache[text] = cached
        return cached

    def slug_with_probes(self, text: str) -> tuple[str, tuple[str, ...]]:
        """Return the next unique slug for heading content and every candidate it examined."""
        base = self._base(text)
        result = base
        probes = [base]
        while result in self._seen:
            self._seen[base] += 1
            result = f"{base}-{self._seen[base]}"
            probes.append(result)
        self._seen[result] = 0
        return result, tuple(probes)


def github_ids_for_texts(texts: Iterable[str]) -> list[str]:
    """Return the GitHub heading id for each raw heading text, in document order.

    The one owner of the pinned document-order collision rule, shared by both heading
    inventories rather than reimplemented per caller. ``github_heading_ids`` delegates here
    for section identity, and ``full_heading_inventory`` allocates through the same slugger for
    the link gate in ``link_check``, whose inventory is deliberately wider than ``Heading``
    describes.
    Sharing the primitive is what keeps a fragment resolving to the same id the engine would
    assign wherever both inventories see the same heading.

    Text rather than ``Heading`` is the parameter because ``Heading`` is one *supported* ATX
    heading with a source line and an explicit anchor, and a setext or nested heading is
    neither. Manufacturing one to reach the collision rule would put values outside that
    type's stated domain into the adapter; a raw text is all the rule reads.

    Args:
        texts: Raw heading content in document order.

    Returns:
        Ids positionally aligned with ``texts``, deduplicated in document order by the pinned
        github-slugger collision rule.
    """
    slugger = _Slugger()
    return [slugger.slug(text) for text in texts]


def github_heading_ids(headings: list[Heading]) -> list[str]:
    """Return the GitHub heading id each heading would render with.

    GitHub has no explicit ``{#anchor}`` syntax, so a marker is slugged as literal heading
    text: ``## Notes {#n}`` renders the id ``notes-n``. This is the namespace a Markdown
    ``#fragment`` link resolves against when the document is viewed on GitHub, and it is
    where ``anchor_ids`` deliberately differs -- that function substitutes doc-lattice's
    explicit identity for the same heading and is not a substitute here. ``github_slug`` is
    not one either: it is a base slug with no deduplication, so repeated headings collapse
    onto a single id.

    The slug is taken from ``Heading.text``, which is raw inline source rather than rendered
    text, matching what ``anchor_ids`` has always slugged. The two agree wherever the pinned
    strip class removes the markup itself -- backticks, emphasis runs, and the brackets in
    ``## [1.0.0] - 2026-01-01`` all fall out on both sides. They diverge for a heading whose
    source carries text GitHub does not display, an inline link being the reachable case:
    ``## [Guide](target.md)`` yields ``guidetargetmd`` here and ``guide`` on GitHub. No
    heading in the supported subset needs that today; closing it means rendering inline
    content inside this adapter, which is a wider change than the heading grammar this module
    declares.

    Args:
        headings: Supported headings in document order.

    Returns:
        Ids positionally aligned with ``headings``, deduplicated in document order by the
        pinned github-slugger collision rule.
    """
    return github_ids_for_texts(heading.text for heading in headings)


def full_heading_inventory(
    body: str, base_cache: dict[str, str] | None = None
) -> list[SluggedHeading]:
    """Return every heading a GitHub render assigns an id to, with its allocation trace.

    Wider than ``extract_headings`` on purpose: this reads the pinned parser's unrestricted
    CommonMark stream, so setext headings, ATX headings indented one to three spaces, and
    headings nested in a list item or a block quote all arrive. That is the inventory
    GitHub allocates ids from, and a collision between a form the engine addresses and one
    it does not is the only way the lattice id and the GitHub fragment can diverge.
    Addressability itself is unchanged; running *allocation* over this inventory is a
    separate follow-up (GTX-277).

    Args:
        body: Markdown document text.
        base_cache: Optional per-document memo of ``text -> base slug``, shared with the
            addressable inventory so a heading both see is slugged once rather than twice.

    Returns:
        One record per heading in document order, ids deduplicated by the pinned
        github-slugger collision rule.

    Raises:
        RuntimeError: If the pinned parser returns a malformed heading token pair.
    """
    normalized = _normalize_for_parse(body)
    tokens = _BLOCK_PARSER.parse(normalized)
    slugger = _Slugger(base_cache)
    inventory: list[SluggedHeading] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        content = tokens[index + 1] if index + 1 < len(tokens) else None
        if content is None or content.type != "inline" or token.map is None:
            msg = f"{MARKDOWN_COMPAT_VERSION} returned a malformed heading token pair"
            raise RuntimeError(msg)
        github_id, probes = slugger.slug_with_probes(content.content)
        inventory.append(
            SluggedHeading(
                text=content.content,
                # ``token.markup`` is ``=`` or ``-`` for a setext heading, so only ``tag``
                # carries the level uniformly across every heading form.
                level=int(token.tag[1:]),
                line=token.map[0] + 1,
                github_id=github_id,
                probes=probes,
            )
        )
    return inventory


def collision_components(
    inventory: list[SluggedHeading],
) -> list[tuple[SluggedHeading, ...]]:
    """Group headings whose ids move together under a reword into collision components.

    During allocation, every candidate id a heading examines (its base slug and each dedup
    suffix tried) links that heading to the id's current holder. The connected components of
    that graph are the collision components, and every generated id in a component is
    ambiguous: rewording any member can hand a member's id to a different heading, which
    resolves without breaking and therefore reads OK or STALE rather than BROKEN.

    Args:
        inventory: Headings in document order, from ``full_heading_inventory``.

    Returns:
        Each component of more than one heading, members in document order, components
        ordered by their first member. A heading in no component is not returned.
    """
    parent = list(range(len(inventory)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    holder: dict[str, int] = {}
    for index, heading in enumerate(inventory):
        for probe in heading.probes:
            if probe in holder:
                union(index, holder[probe])
        holder[heading.github_id] = index

    grouped: dict[int, list[SluggedHeading]] = {}
    for index, heading in enumerate(inventory):
        grouped.setdefault(find(index), []).append(heading)
    return [tuple(members) for _root, members in sorted(grouped.items()) if len(members) > 1]


def addressable_heading_inventory(
    headings: list[Heading], base_cache: dict[str, str] | None = None
) -> list[SluggedHeading]:
    """Return the addressable TOC's own slug-allocation trace, one record per heading.

    ``full_heading_inventory`` traces allocation over the full CommonMark parse, which misses a
    heading the restricted addressable scanner still addresses: a column-zero ``#`` line inside
    an HTML comment or another container the full parse renders as inert is not a heading token
    there, but ``extract_headings`` is not container-aware and reads it as one anyway. This
    traces the same allocation over ``build_toc``'s own output instead, so a caller can union
    both traces' collision components and catch either direction of the mismatch.

    Slug source is ``Heading.text``, the same input ``anchor_ids`` slugs, and allocation runs
    over every heading in document order regardless of an explicit marker, matching
    ``github_heading_ids``'s dedup state -- a marker-set heading still occupies a slot in the
    document-order sequence even though its own id comes from the marker, not the slug.

    Args:
        headings: The addressable ATX subset from ``build_toc``, in document order.
        base_cache: Optional per-document memo of ``text -> base slug``, shared with the full
            inventory so a heading both see is slugged once rather than twice.

    Returns:
        One record per heading in document order, ids deduplicated by the pinned
        github-slugger collision rule.
    """
    slugger = _Slugger(base_cache)
    inventory: list[SluggedHeading] = []
    for heading in headings:
        github_id, probes = slugger.slug_with_probes(heading.text)
        inventory.append(
            SluggedHeading(
                text=heading.text,
                level=heading.level,
                line=heading.line,
                github_id=github_id,
                probes=probes,
            )
        )
    return inventory


def anchor_ids(headings: list[Heading]) -> list[str]:
    """Return one explicit or generated addressable id per heading.

    Args:
        headings: Supported headings in document order.

    Returns:
        Addressable ids positionally aligned with ``headings``.
    """
    return [
        heading.anchor if heading.anchor is not None else generated
        for heading, generated in zip(headings, github_heading_ids(headings), strict=True)
    ]


def strip_heading_anchor(text: str) -> str:
    """Remove a valid trailing explicit anchor from one raw heading line.

    Args:
        text: Raw source line containing the section heading.

    Returns:
        The line with its trailing marker removed and ATX closing sequence retained.
    """
    return _ANCHOR_RE.sub(" ", text).rstrip()
