"""Load and validate .game-lattice.yml, with project-root containment of docs_roots."""

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .error_types import ConfigError
from .path_utils import safe_resolve

DEFAULT_CONFIG_NAME = ".game-lattice.yml"
_YAML = YAML(typ="safe")

# A cache_key is one safe path segment: it must start with an alphanumeric (rejecting ".",
# "..", and hidden-directory names) and thereafter allow only word, dot, and hyphen, so it can
# never express a separator or a traversal. Length capped at 64 characters total.
_CACHE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Config(BaseModel):
    """The validated shape of .game-lattice.yml."""

    model_config = ConfigDict(strict=True, extra="forbid")

    docs_roots: list[str] = Field(default_factory=lambda: ["docs"])
    ignore_globs: list[str] = Field(default_factory=list)
    linear_team: str | None = None
    binding_layers: list[str] | None = None
    cache_key: str | None = None
    cache_trust_stat: bool = False

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
        ConfigError: If the file is missing, invalid, has unknown keys, or names a
            docs root that resolves outside the project root.
    """
    if config_path is not None:
        if not config_path.exists():
            msg = f"config file not found: {config_path}"
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
        msg = f"invalid config: {exc}"
        raise ConfigError(msg) from exc

    roots = _resolve_roots(config.docs_roots, project_root)
    return ProjectConfig(config=config, project_root=project_root, resolved_roots=roots)


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read config {path}: {exc}"
        raise ConfigError(msg) from exc
    # A YAML directive updates the reusable parser's version. Reset it so each config starts
    # with default YAML semantics, matching a fresh safe loader.
    _YAML.version = None
    try:
        data = _YAML.load(text)
    except YAMLError as exc:
        msg = f"cannot parse config {path}: {exc}"
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
            msg = (
                f"docs_roots entry {entry!r} resolves outside the project root "
                f"{project_root}; roots must stay inside the project"
            )
            raise ConfigError(msg) from exc
        resolved.append(safe)
    return tuple(resolved)
