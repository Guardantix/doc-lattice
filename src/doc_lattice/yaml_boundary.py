"""Boundary module: the ruamel safe-load mechanics the value-consuming YAML entry points share.

`frontmatter_parser` and `config` each read user-authored YAML through a reusable safe
loader, and `reconcile` catches the same failure family without loading through one. Two
things are genuinely shared: which exceptions a safe load can raise, and the directive state
a reused loader has to clear before each document. Both live here so one module owns them.

`github_ci/workflow_parser` and `reconcile` build their own loaders instead of using the one
below, because AD-26 makes the pure Python parser part of their compatibility surface: they
read source marks, resolver state, and directive state that a plain safe loader gives up the
moment the optional `ruamel.yaml.clib` accelerator is installed. Do not route either through
`SafeYamlLoader`; it would silently drop `pure=True`. `reconcile` does still catch
`YAML_LOAD_ERRORS`, which is why the tuple lives here rather than inside one loader.
`github_ci/workflow_parser` is the exception on both counts: it spells its own handlers and
does not import the tuple at all.

What does not live here is policy. Each caller keeps its own error translation, because a
malformed config and a malformed frontmatter block are different errors to the user, and
config keeps its own empty-document fallback.

Each caller also still builds its own loader rather than sharing one instance across
modules. A cross-module singleton would make one boundary's document state observable from
another, which is the coupling the per-load reset exists to prevent.
"""

from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Everything a safe load of user-authored YAML can raise. Beyond the YAMLError family the
# scanner and parser raise, a constructor building a tagged scalar its type cannot accept
# raises the builtin that construction failed with, so `!!int oops` reaches a caller as a bare
# ValueError. Every module loading a user's YAML catches this family and reports a
# ProjectError, so the same typo is a clean error wherever the user writes it.
YAML_LOAD_ERRORS = (YAMLError, ValueError, KeyError, TypeError)


class SafeYamlLoader:
    """A reusable ruamel safe loader that clears directive state before every load.

    Reusing one loader avoids rebuilding the parser per document, but a reused instance
    carries `YAML.version` across loads, so a `%YAML` directive in one document would
    otherwise steer the parse of the next. `load` resets it every time, which is what makes
    a reused instance behave like a fresh one for the values these boundaries consume.
    """

    def __init__(self) -> None:
        """Build the underlying safe loader this instance reuses for every load."""
        self._yaml = YAML(typ="safe")

    def load(self, text: str) -> Any:
        """Load one YAML document with default semantics.

        Args:
            text: The YAML source to load.

        Returns:
            The loaded value, still untyped: None for an empty document, otherwise whatever
            the root node constructs to. Callers validate it into a typed model.

        Raises:
            YAMLError: If the document cannot be scanned, parsed, or constructed. A
                constructor can also raise the builtin whose type rejected a tagged scalar,
                so callers catch `YAML_LOAD_ERRORS` rather than this one type.
        """
        # A YAML directive can update the reusable parser's version even when parsing fails.
        # Reset it so each document starts with default YAML semantics, matching a fresh
        # safe loader.
        self._yaml.version = None
        return self._yaml.load(text)
