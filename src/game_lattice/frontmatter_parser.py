"""Boundary module: split and validate untyped YAML frontmatter into typed NodeMeta."""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .error_types import ConfigError, UnreadableDocError
from .model import NodeMeta

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a document into its YAML frontmatter block and body.

    Args:
        text: The full file text.

    Returns:
        ``(raw_meta, body)`` where ``raw_meta`` is the YAML between the opening and
        closing ``---`` fences (or None if the file does not open with a fence), and
        ``body`` is everything after the closing fence (the whole text if no fence).
    """
    stripped = text.lstrip("﻿")
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            raw_meta = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            return raw_meta + "\n" if raw_meta else "", body
    return None, text


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
    yaml = YAML(typ="safe")
    try:
        data: Any = yaml.load(raw_meta)
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
