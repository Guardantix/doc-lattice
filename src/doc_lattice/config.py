"""Load and validate .doc-lattice.yml, with project-root containment of docs_roots."""

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .constants import LATTICE_FORMAT_VERSION
from .error_types import ConfigError
from .path_utils import format_path_for_display, safe_resolve
from .validation_render import format_validation_error
from .yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader
from .yaml_error_render import format_yaml_error_for_display

DEFAULT_CONFIG_NAME = ".doc-lattice.yml"
# Deliberately not pinned to the pure parser, unlike the frontmatter boundary. Config has no
# declared spelling subset, and no rewriter reads it back, so the two parsers disagreeing about
# it costs a config author one clear error rather than changing which documents the lattice
# holds. Whether a given config loads therefore stays environment dependent: that is an accepted
# consequence of AD-33 leaving this boundary out of scope, not a guarantee to rely on.
# Constructed at import rather than per load, and deliberately outside the `YAML_LOAD_ERRORS`
# handler below: `TypeError` is a member of that family, so a lazily built loader missing its
# argument would be reported as the user's config being malformed.
_LOADER = SafeYamlLoader(parser="platform-default")

# A cache_key is one safe path segment: it must start with an alphanumeric (rejecting ".",
# "..", and hidden-directory names) and thereafter allow only word, dot, and hyphen, so it can
# never express a separator or a traversal. Length capped at 64 characters total.
_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Rendered in place of a field path when pydantic reports no location: a validator that runs on
# the whole model, or a config file whose top level is not a mapping at all, which reaches
# validation because _read_yaml passes a list or a scalar straight through.
_ROOT_LOCATION = "<config>"
_BINDING_LAYERS_KEY = "binding_layers"
_BINDING_LAYERS_MIGRATION = (
    "binding_layers has been unsupported since 2.0; delete it from 1.x configs, there is "
    "no replacement."
)


class Config(BaseModel):
    """The validated shape of .doc-lattice.yml."""

    model_config = ConfigDict(strict=True, extra="forbid")

    lattice_format: int | None = None
    docs_roots: list[str] = Field(default_factory=lambda: ["docs"])
    ignore_globs: list[str] = Field(default_factory=list)
    linear_team: str | None = None
    cache_key: str | None = None
    cache_trust_stat: bool = False

    @field_validator("lattice_format")
    @classmethod
    def _validate_lattice_format(cls, value: int | None) -> int | None:
        """Reject a lattice_format this engine does not read."""
        if value is not None and value != LATTICE_FORMAT_VERSION:
            msg = (
                f"lattice_format {value} is not a format this engine reads; it reads "
                f"lattice_format {LATTICE_FORMAT_VERSION}. Install the doc-lattice release that "
                "matches the lattice, or change the key"
            )
            raise ValueError(msg)
        return value

    @field_validator("cache_key")
    @classmethod
    def _validate_cache_key(cls, value: str | None) -> str | None:
        """Reject a cache_key that is not a single safe path segment."""
        if value is not None and _CACHE_KEY_RE.fullmatch(value) is None:
            msg = (
                f"cache_key {value!r} must be one safe path segment matching "
                r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ (no separators or traversal)"
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _trust_stat_requires_cache_key(self) -> "Config":
        """Setting cache_trust_stat without cache_key is a configuration error."""
        if self.cache_trust_stat and self.cache_key is None:
            msg = (
                "cache_trust_stat requires cache_key to be set; set cache_key or remove "
                "cache_trust_stat"
            )
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """A loaded config plus the project root and the resolved, contained docs roots."""

    config: Config
    project_root: Path
    resolved_roots: tuple[Path, ...]


def load_config(config_path: Path | None, cwd: Path) -> ProjectConfig:
    """Load config and resolve docs roots inside the project boundary.

    Args:
        config_path: Explicit ``--config`` path, or None to look in ``cwd``.
        cwd: The current working directory.

    Returns:
        A ProjectConfig with validated config, project root, and contained roots.

    Raises:
        ConfigError: If the file is missing, invalid, has unknown keys, names a docs root
            that resolves outside the project root, or names an existing docs root that is
            neither a directory nor a regular ``.md`` file.
    """
    if config_path is not None:
        if not config_path.exists():
            msg = f"config file not found: {format_path_for_display(config_path)}"
            raise ConfigError(msg)
        source = config_path
    else:
        candidate = cwd / DEFAULT_CONFIG_NAME
        source = candidate if candidate.exists() else None

    if source is not None:
        raw = _read_yaml(source)
        project_root = source.resolve().parent
    else:
        # An explicit --config that is missing is an error (above), but an absent default
        # config is not: the tool runs zero-config using Config's built-in defaults.
        raw = {}
        project_root = cwd.resolve()

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source)) from exc

    # Required only when a config file was actually read: a zero-config run has no file to
    # carry the key, not no skew to catch. Skew lives in each section's `seen` hash, so a
    # zero-config tree reconciled under 6.x will see nested-section edges report STALE under
    # 7.0, not this ConfigError. Follow the v7 migration (fix AMBIGUOUS headings, then
    # `reconcile --all`), or add a config with the key to get this loud guard instead.
    if source is not None and config.lattice_format is None:
        # The remedy names all three migration steps in order. Naming only the re-bless would
        # send an adopter with any duplicate heading straight into reconcile's ambiguity refusal,
        # which writes nothing and fails the whole run.
        msg = (
            f"config {format_path_for_display(source)} does not declare "
            f"'lattice_format: {LATTICE_FORMAT_VERSION}', which doc-lattice 7 requires. Add the "
            "key, then run 'doc-lattice check' and fix every AMBIGUOUS edge, then run "
            "'doc-lattice reconcile --all' to re-bless the lattice under the 7.0 content hash; "
            "see the 7.0.0 migration in CHANGELOG.md"
        )
        raise ConfigError(msg)

    roots = _resolve_roots(config.docs_roots, project_root)
    return ProjectConfig(config=config, project_root=project_root, resolved_roots=roots)


def _format_validation_error(exc: ValidationError, source: Path | None) -> str:
    """Render a config validation failure as the diagnostic this project owns.

    Args:
        exc: The validation error raised by ``Config.model_validate``.
        source: The config file the raw mapping was read from. None is defensive only:
            zero-config validates ``{}``, and every ``Config`` field has a default, so
            ``model_validate`` cannot fail on that path.

    Returns:
        A multi-line message: a header naming the config file, then one line per error.
    """
    # Built in two branches rather than through one alias. The display guard prunes only the
    # subtree rooted at the helper call, so a conditional inside the f-string would leave the
    # `source is not None` test outside it and be reported; binding `where = str(source)` first
    # would instead hide the path behind a name the guard does not classify. Two branches keep
    # `source` itself inside the guarded expression, which is the shape the guard can see.
    if source is None:
        header = "invalid config <no config file>:"
    else:
        header = f"invalid config {format_path_for_display(source)}:"
    return format_validation_error(
        exc,
        header=header,
        model=Config,
        root_label=_ROOT_LOCATION,
        extra_note=_binding_layers_note,
    )


def _binding_layers_note(location: tuple[int | str, ...]) -> str | None:
    """Supply the 1.x migration sentence when the forbidden key is the retired one.

    ``extra="forbid"`` is the only thing that catches ``binding_layers``, so the diagnostic is
    where the migration has to be said. The match is on the whole location rather than its last
    segment: were ``Config`` ever to nest a model, a field of the same name inside it would not
    be the retired top-level key.

    Args:
        location: The ``loc`` tuple from one ``extra_forbidden`` error.

    Returns:
        The migration sentence for the retired top-level key, else None.
    """
    if location == (_BINDING_LAYERS_KEY,):
        return _BINDING_LAYERS_MIGRATION
    return None


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read config {format_path_for_display(path)}: {exc}"
        raise ConfigError(msg) from exc
    try:
        data = _LOADER.load(text)
    except YAML_LOAD_ERRORS as exc:
        # The parser is named because this boundary is the one whose acceptance still depends on
        # the environment, so "it loads for me and fails for my teammate" is a reachable state
        # and nothing else at runtime reveals which parser ran. What is named is the parser in
        # hand rather than `_LOADER.parser`: the request reads "platform-default" in both
        # environments, so printing it would tell the two apart no better than printing nothing.
        parser = "pure" if _LOADER.running_pure else "ruamel.yaml.clib"
        # Only `path` is wrapped here. `detail` is already the display spelling AD-37 owns, and
        # re-rendering it would quote the parser's own message a second time.
        detail = format_yaml_error_for_display(exc)
        msg = (
            f"cannot parse config {format_path_for_display(path)} (YAML parser: {parser}): {detail}"
        )
        raise ConfigError(msg) from exc
    return data if data is not None else {}


def _resolve_roots(roots: list[str], project_root: Path) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for entry in roots:
        candidate = Path(entry)
        absolute_path = candidate if candidate.is_absolute() else project_root / candidate
        try:
            safe = safe_resolve(absolute_path, project_root)
        except ValueError as exc:
            # `entry` is the recorded config string, passed to the helper as text: routing it
            # through Path() first would normalize away a trailing separator or a leading "./",
            # and a diagnostic that rejects a configured value has to show what it rejected.
            msg = (
                f"docs_roots entry {format_path_for_display(entry)} resolves outside the "
                f"project root {format_path_for_display(project_root)}; roots must stay "
                "inside the project"
            )
            raise ConfigError(msg) from exc
        # A missing entry stays tolerated: discovery skips it, so a docs root that is not
        # checked out is not fatal. An entry that exists must be something discovery can turn
        # into documents -- a directory to walk, or one regular ".md" file. Anything else (a
        # non-".md" file, a FIFO, a socket, a device) would be dropped without a word and let
        # a check pass over documents that were never read. safe_resolve already followed
        # symlinks, so this classifies the link target.
        usable = not safe.exists() or safe.is_dir() or (safe.is_file() and safe.suffix == ".md")
        if not usable:
            msg = (
                f"docs_roots entry {format_path_for_display(entry)} exists but is neither a "
                f"directory nor a regular '.md' file ({format_path_for_display(safe)}); an "
                "existing entry must be one or the other"
            )
            raise ConfigError(msg)
        resolved.append(safe)
    return tuple(resolved)
