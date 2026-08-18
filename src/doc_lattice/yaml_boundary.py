"""Boundary module: the ruamel safe-load mechanics the value-consuming YAML entry points share.

`frontmatter_parser` and `config` each read user-authored YAML through a reusable safe
loader, and `reconcile` catches the same failure family without loading through one. Two
things are genuinely shared: which exceptions a safe load can raise, and the directive state
a reused loader has to clear before each document. Both live here so one module owns them.
Parser implementation is not shared but is stated here, because the mechanism that applies a
caller's choice to every loader it builds is this module's to own.

Parser choice is a required argument rather than a default, because leaving it to ruamel
leaves it to the environment: a plain safe loader switches to the C parser the moment the
optional `ruamel.yaml.clib` accelerator is installed, which any other package in a user's
environment may pull in. The two parsers do not accept the same documents, so an unstated
choice made whether a file counted as tracked depend on what else was installed alongside
this engine (AD-33). Each caller now states which parser its own contract needs:
`frontmatter_parser` asks for the pure Python one so a tracked-document verdict is the same
in both environments, and `config` asks for the default one, whose semantics that pin
deliberately leaves alone.

`reconcile` still builds its own loaders rather than using the one below, and for a reason
this argument does not address: it reads source marks, resolver state, and directive state
off a loader bound to one document, so it needs a fresh instance per read rather than a
shared one. It asks for the pure parser too, for the mark accounting AD-26 records.

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
# ValueError. AssertionError is here for the same reason and not as a broad catch: ruamel's
# safe `construct_yaml_omap` enforces `!!omap` key uniqueness with a bare `assert`, so a
# duplicate key inside an ordered map arrives as an AssertionError rather than a YAMLError.
# Every module loading a user's YAML catches this family and reports a ProjectError, so the
# same typo is a clean error wherever the user writes it.
YAML_LOAD_ERRORS = (YAMLError, ValueError, KeyError, TypeError, AssertionError)


class SafeYamlLoader:
    """A reusable ruamel safe loader that clears directive state before every load.

    Reusing one loader avoids rebuilding the parser per document, but a reused instance
    carries `YAML.version` across loads, so a `%YAML` directive in one document would
    otherwise steer the parse of the next. `load` discards the underlying loader whenever a
    directive touched it, which is what makes a reused instance behave like a fresh one for
    the values these boundaries consume.

    The parser implementation is fixed for the life of the instance and reapplied to every
    replacement `load` builds, so a document read after a directive runs on the same parser as
    one read before it.
    """

    def __init__(self, *, pure: bool) -> None:
        """Build the underlying safe loader this instance reuses for every load.

        Args:
            pure: Whether to require ruamel's pure Python parser. True pins it, so the
                accepted document set does not depend on whether the optional
                `ruamel.yaml.clib` accelerator is installed. False takes ruamel's own
                default, which is that accelerator wherever it is present. There is no
                default, because an unstated choice is what made acceptance environment
                dependent in the first place.
        """
        self._pure = pure
        self._yaml = self._build()

    def _build(self) -> YAML:
        """Return a safe loader on this instance's chosen parser implementation."""
        return YAML(typ="safe", pure=self._pure)

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
        # A YAML directive can update the reusable parser's version even when parsing fails,
        # and a stale version steers the next document's resolution. Clearing `YAML.version`
        # is not enough on its own across the declared `ruamel.yaml` range: 0.18 builds the
        # versioned resolver once, on first access, and never rebuilds it, so the previous
        # document's 1.1 resolution survives the reset there and a later `on:` reads back as
        # `True`. 0.19 rebuilds the resolver when the version no longer matches. Discarding
        # the loader instead is correct under both, and it costs a construction only for the
        # rare document that actually carried a directive. `version` stays None under the
        # optional `ruamel.yaml.clib` parser, which skips directive state entirely (AD-26),
        # so this never fires on a loader built with `pure=False` in an environment that has
        # the accelerator, and nothing leaks there either.
        if self._yaml.version is not None:
            self._yaml = self._build()
        return self._yaml.load(text)
