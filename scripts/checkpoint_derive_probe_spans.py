"""Derive candidate-command spans for issue #100 checkpoint probes.

Splits certified fixture sources on the frozen floor-grammar statement boundaries (unquoted
newline, ``;``, ``&&``, ``||``) and emits one span per statement whose first word is a
doc-lattice launcher or executable literal. Output is reviewed by hand and frozen; it is
authoring tooling, not the recognizer.
"""

import json
import re
import sys

MARKER = re.compile(r"doc[-_.]+lattice", re.ASCII | re.IGNORECASE)
LAUNCHERS = ("doc-lattice", "uvx", "uv")


def spans_for(source: str) -> list[dict[str, object]]:
    """Return candidate spans as dicts with start, end, and text."""
    spans: list[dict[str, object]] = []
    start = 0
    index = 0
    quote: str | None = None
    length = len(source)
    while index <= length:
        char = source[index] if index < length else "\n"
        two = source[index : index + 2]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        boundary = char in "\n;" or two in ("&&", "||")
        if boundary or index == length:
            text = source[start:index]
            stripped = text.strip()
            first = stripped.split(" ", 1)[0] if stripped else ""
            if first in LAUNCHERS or MARKER.search(first):
                offset = start + (len(text) - len(text.lstrip()))
                spans.append({"start": offset, "end": offset + len(stripped), "text": stripped})
            index += 2 if two in ("&&", "||") else 1
            start = index
            continue
        index += 1
    return spans


if __name__ == "__main__":
    print(json.dumps(spans_for(sys.stdin.read()), indent=2))
