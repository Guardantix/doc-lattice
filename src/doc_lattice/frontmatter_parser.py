"""Boundary module: split and validate untyped YAML frontmatter into typed NodeMeta.

Parsing classifies a file and raises on a malformed one, but never reports a benign skip. The
disposition it returns is what the caller reports from, so the cache-free, cold-cache, and
warm-cache paths can all warn from a single site.
"""

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml.error import ReusedAnchorWarning

from .constants import LATTICE_INTENT_KEYS
from .error_types import FrontmatterError, UnreadableDocError
from .model import NodeMeta, ParsedMeta
from .validation_render import format_validation_error
from .yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader

_FENCE = "---"
_BOM = chr(0xFEFF)  # UTF-8 byte-order mark; strip a leading one so the opening fence is detected
# Pinned to the pure Python parser. The two ruamel parsers do not accept the same documents, so
# leaving the choice to ruamel made a document's tracked status depend on whether the optional
# `ruamel.yaml.clib` accelerator happened to be installed alongside this engine. Two spellings
# diverged: an anchor name defined twice was accepted and rebound without the accelerator and
# refused as a duplicate with it, and a declared `%YAML` version steered resolution without it
# and was ignored outright with it, so a block heading itself `%YAML 1.1` read `id: on` as the
# string "on" on the strict path while the reread inside `apply_reconcile` read it as a boolean.
# AD-33 settles both as supported, so the parser that supports them is the one this boundary
# asks for, in every environment; AD-31 layer 2 carries the accepted set itself.
# Constructed at import and deliberately outside the `YAML_LOAD_ERRORS` handler in `parse_meta`:
# `TypeError` is a member of that family, so a lazily built loader missing its argument would be
# reported as the user's frontmatter being unreadable.
_LOADER = SafeYamlLoader(parser="pure")
# The two node-free outcomes are immutable and carry no per-file state, so they are shared.
_UNTRACKED = ParsedMeta(meta=None, disposition="untracked")
_ID_LESS = ParsedMeta(meta=None, disposition="id-less")
# Rendered in place of a field path when pydantic reports no location. NodeMeta declares no
# model-level validator today, and a non-mapping block is returned as untracked before it ever
# reaches validation, so this is defensive: it exists so a future whole-block rule cannot
# render a field name the author never wrote.
_ROOT_LOCATION = "<frontmatter>"


@dataclass(frozen=True, slots=True)
class FrontmatterParts:
    """Every source piece a document's frontmatter block is spelled with.

    Attributes:
        prefix: A leading byte-order mark, which precedes the opening fence.
        open_fence: The opening fence line as written, any surrounding space included.
        raw_meta: The YAML between the fences, empty when the block holds none.
        close_fence: The closing fence line as written.
        close_fence_newline: The newline ending the closing fence, empty at end of file.
        body: Everything after that newline.
    """

    prefix: str
    open_fence: str
    raw_meta: str
    close_fence: str
    close_fence_newline: str
    body: str


def split_frontmatter_parts(text: str, source: Path) -> FrontmatterParts | None:
    """Split a document into every piece its frontmatter block is written with.

    ``split_frontmatter`` returns the two pieces a reader needs. This returns the rest of
    them as well, since a byte-exact rewrite has to put back the fences the author wrote,
    and any byte-order mark before them, rather than the spelling this engine would choose.

    Args:
        text: The full file text.
        source: The file the frontmatter came from, for error messages.

    Returns:
        The document's frontmatter pieces, or None if it does not open with a fence.

    Raises:
        UnreadableDocError: If an opening fence has no closing fence.
    """
    # Strip a leading UTF-8 BOM (U+FEFF) so a file saved with one still has its opening
    # "---" fence recognized on line 0 instead of being read as having no frontmatter.
    stripped = text.lstrip(_BOM)
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None
    for closing_fence_index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FENCE:
            raw_meta = "\n".join(lines[1:closing_fence_index])
            # Splitting on newlines leaves the closing fence as the final element only when
            # the file ends on it, so a last line here is what shows the newline was there.
            trailing = "\n" if closing_fence_index < len(lines) - 1 else ""
            return FrontmatterParts(
                text[: len(text) - len(stripped)],
                lines[0],
                raw_meta + "\n" if raw_meta else "",
                line,
                trailing,
                "\n".join(lines[closing_fence_index + 1 :]),
            )
    raise UnreadableDocError(f"unclosed YAML frontmatter in {source}: add a closing '---' fence")


def split_frontmatter(text: str, source: Path) -> tuple[str | None, str]:
    """Split a document into its YAML frontmatter block and body.

    Args:
        text: The full file text.
        source: The file the frontmatter came from, for error messages.

    Returns:
        ``(raw_meta, body)`` where ``raw_meta`` is the YAML between the opening and
        closing ``---`` fences (or None if the file does not open with a fence), and
        ``body`` is everything after the closing fence (the whole text if there is no
        opening fence).

    Raises:
        UnreadableDocError: If an opening fence has no closing fence.
    """
    parts = split_frontmatter_parts(text, source)
    return (None, text) if parts is None else (parts.raw_meta, parts.body)


def parse_meta(raw_meta: str | None, source: Path) -> ParsedMeta:
    """Classify a raw frontmatter block, validating it into NodeMeta when it names a node.

    A block with no ``id`` is graded by what else it declares. Carrying any of
    ``LATTICE_INTENT_KEYS`` means the file meant to be a node and lost its ``id`` to a typo or
    an edit, which would silently drop it and every edge it declares, so that is a hard error.
    Anything else is metadata this engine does not own, and is skipped as ``"id-less"`` for the
    caller to warn about. A file that never opened a fence is ``"untracked"`` and says nothing.

    Args:
        raw_meta: The YAML frontmatter text, or None when the file opened no fence.
        source: The file the frontmatter came from, for error messages.

    Returns:
        The validated node and its disposition, or a null node and the reason it was skipped.

    Raises:
        UnreadableDocError: If the YAML cannot be parsed.
        FrontmatterError: If the frontmatter has an unknown or malformed key, or declares
            lattice intent with no ``id``.
    """
    if raw_meta is None:
        return _UNTRACKED
    try:
        data, reused_anchors = _load_recording_reused_anchors(raw_meta)
    except YAML_LOAD_ERRORS as exc:
        msg = f"cannot parse frontmatter in {source}: {exc}"
        raise UnreadableDocError(msg) from exc
    # A fenced block holding no mapping (empty, a scalar, a list) declares no keys at all, so it
    # is the same untracked prose as a file with no fence. Warning on it would fire on any
    # document that merely opens with a thematic break.
    if not isinstance(data, dict):
        return _UNTRACKED
    if "id" not in data:
        return _id_less(data, source)
    try:
        meta = NodeMeta.model_validate(data)
    except ValidationError as exc:
        msg = format_validation_error(
            exc,
            header=f"invalid lattice frontmatter in {source}:",
            model=NodeMeta,
            root_label=_ROOT_LOCATION,
        )
        raise FrontmatterError(msg) from exc
    return ParsedMeta(meta=meta, disposition="tracked", reused_anchors=reused_anchors)


def _load_recording_reused_anchors(raw_meta: str) -> tuple[Any, bool]:
    """Load a frontmatter block, recording whether it defines an anchor name more than once.

    The pure parser accepts a reused anchor name and raises ``ReusedAnchorWarning`` about it.
    That warning is captured here rather than allowed to escape, because it is raised from
    inside ruamel: it names no document, and the load it comes from does not run at all on a
    warm cache. AD-29 requires a diagnostic a load emits to be derivable from a cache entry and
    rendered at one shared site, so the fact is returned as data and ``orchestrate`` reports it.
    Every other captured warning is re-emitted at its original location, so nothing but this one
    category is intercepted.

    ``catch_warnings`` mutates process-global filter state and is not thread-safe. This engine
    is single-threaded, and a warning escalated to an error by the caller's filters still
    escalates, on re-emission below rather than mid-parse.

    Args:
        raw_meta: The YAML frontmatter text to load.

    Returns:
        The loaded value, still untyped, and whether an anchor name was defined twice.

    Raises:
        YAMLError: If the document cannot be scanned, parsed, or constructed. Callers catch
            ``YAML_LOAD_ERRORS`` rather than this one type.
    """
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        data: Any = _LOADER.load(raw_meta)
    reused = False
    for entry in captured:
        if issubclass(entry.category, ReusedAnchorWarning):
            reused = True
            continue
        warnings.warn_explicit(entry.message, entry.category, entry.filename, entry.lineno)
    return data, reused


def _id_less(data: dict[Any, Any], source: Path) -> ParsedMeta:
    """Grade an id-less frontmatter mapping into the error tier or the warning tier.

    Args:
        data: The loaded frontmatter mapping, already known to have no ``id`` key.
        source: The file the frontmatter came from, for error messages.

    Returns:
        The id-less disposition with a null node, when the block declares no lattice intent.

    Raises:
        FrontmatterError: If the block declares any lattice intent key.
    """
    declared = sorted(LATTICE_INTENT_KEYS.intersection(data))
    if not declared:
        return _ID_LESS
    keys = ", ".join(repr(key) for key in declared)
    msg = (
        f"frontmatter in {source} declares {keys} but has no 'id' key, so the file and every "
        "edge it declares would be dropped from the lattice; add an 'id' (check it for a typo) "
        "or remove the lattice keys"
    )
    raise FrontmatterError(msg)
