"""Boundary module: split and validate untyped YAML frontmatter into typed NodeMeta."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .error_types import ConfigError, UnreadableDocError
from .model import NodeMeta

_FENCE = "---"
_BOM = chr(0xFEFF)  # UTF-8 byte-order mark; strip a leading one so the opening fence is detected
_YAML = YAML(typ="safe")


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


def parse_meta(raw_meta: str | None, source: Path) -> NodeMeta | None:
    """Validate a raw frontmatter block into NodeMeta, or None if not a lattice node.

    Args:
        raw_meta: The YAML frontmatter text, or None.
        source: The file the frontmatter came from, for error messages.

    Returns:
        A validated NodeMeta, or None when there is no frontmatter or no ``id`` key.

    Raises:
        UnreadableDocError: If the YAML cannot be parsed.
        ConfigError: If the frontmatter has an unknown or malformed key.
    """
    if raw_meta is None:
        return None
    # A YAML directive can update the reusable parser's version even when parsing fails. Reset it
    # so each document starts with default YAML semantics, matching a fresh safe loader.
    _YAML.version = None
    try:
        data: Any = _YAML.load(raw_meta)
    except YAMLError as exc:
        msg = f"cannot parse frontmatter in {source}: {exc}"
        raise UnreadableDocError(msg) from exc
    if not isinstance(data, dict) or "id" not in data:
        return None
    try:
        return NodeMeta.model_validate(data)
    except ValidationError as exc:
        msg = f"invalid lattice frontmatter in {source}: {exc}"
        raise ConfigError(msg) from exc
