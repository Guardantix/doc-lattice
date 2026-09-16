"""Boundary module: validate AD-51 sidecar manifests into a typed registration index.

A manifest is untyped YAML naming Markdown files another tool owns, each with the node metadata
doc-lattice keeps for it outside the file. This module reads every manifest the configuration
declares, validates it against the AD-51 schema, and returns typed registrations carrying both
sides of their provenance: the spelling each record and each manifest was declared with, and the
target it resolves to. Everything is refused as an exit-2 ``ProjectError`` naming the most
specific location known, never dropped.

What it checks is exactly what needs no loaded lattice. Loading registered files as nodes, and
the ownership rules that need them (an id declared both inline and externally, duplicate ids, a
manifest reached as a node) belong to the loader that joins this index with discovery.

Every path resolves against the project root, never against the manifest or the working
directory, so moving a manifest never re-points its records (AD-51).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from .error_types import ManifestError, RegistrationConflictError
from .model import NodeMeta
from .path_utils import format_path_for_display, safe_resolve
from .text_utils import describe_first_control_char
from .validation_render import format_validation_error
from .yaml_boundary import YAML_LOAD_ERRORS, SafeYamlLoader
from .yaml_error_render import format_yaml_error_for_display

# AD-51 reads manifests under AD-33's pure parser, so whether a manifest loads never depends on
# whether the optional accelerator is installed. Constructed at import for the reason config.py
# gives: `TypeError` is in `YAML_LOAD_ERRORS`, so a lazily built loader missing its argument
# would be reported as the user's manifest being malformed.
_LOADER = SafeYamlLoader(parser="pure")

_NODES_KEY = "nodes"
_RECORD_KEYS = frozenset({"path", "meta"})
_MARKDOWN_SUFFIX = ".md"
# Rendered in place of a field path when `NodeMeta` reports no location, which a `meta` that is
# not a mapping reaches.
_META_ROOT_LABEL = "<meta>"


@dataclass(frozen=True, slots=True)
class ManifestSource:
    """One manifest as the configuration declared it and as it resolved.

    Attributes:
        declared: The ``sidecar_manifests`` entry exactly as written.
        resolved: The contained, symlink-followed regular file that entry names.
    """

    declared: str
    resolved: Path


@dataclass(frozen=True, slots=True)
class Registration:
    """One validated manifest record.

    Attributes:
        declared_path: The record's ``path`` exactly as written, which is the node's identity.
        target: The contained, symlink-followed regular file ``declared_path`` names, which is
            what ownership compares.
        manifest: The manifest the record was declared in.
        position: The record's 0-based index in that manifest's ``nodes`` sequence.
        meta: The record's validated node metadata.
    """

    declared_path: str
    target: Path
    manifest: ManifestSource
    position: int
    meta: NodeMeta

    @property
    def location(self) -> str:
        """Name this record for a diagnostic: its manifest, position, and declared path."""
        return _record_location(self.manifest.declared, self.position, self.declared_path)


@dataclass(frozen=True, slots=True)
class RegistrationIndex:
    """Every validated registration, with singular ownership of each target already checked.

    Attributes:
        manifests: Each declared manifest, in configuration order.
        by_target: Each registration keyed by its resolved target, in configuration order and
            then manifest order. Exactly one per target, since a second claim is refused rather
            than overwriting the first.
    """

    manifests: tuple[ManifestSource, ...]
    by_target: Mapping[Path, Registration]

    @property
    def registrations(self) -> tuple[Registration, ...]:
        """Return each registration, in configuration order and then manifest order."""
        return tuple(self.by_target.values())


def build_registration_index(manifests: Sequence[str], project_root: Path) -> RegistrationIndex:
    """Read and validate every declared manifest into one registration index.

    Args:
        manifests: The ``sidecar_manifests`` entries, exactly as the configuration spelled them.
        project_root: The project root every manifest and record path resolves against.

    Returns:
        The typed registrations and the manifests they came from.

    Raises:
        ManifestError: If a manifest cannot be resolved, read, or parsed, breaks the schema, or
            spells an anchor, alias, or merge key, or if one of its records is malformed or names
            an unusable target.
        RegistrationConflictError: If two records, in one manifest or in two, resolve to the
            same target.
    """
    sources: list[ManifestSource] = []
    by_target: dict[Path, Registration] = {}
    for declared in manifests:
        # The file-type check runs before anything opens the manifest, so a FIFO or other
        # special file is refused rather than read, which could block the run.
        resolved = _resolve_regular_file(
            declared,
            project_root,
            subject=f"manifest {format_path_for_display(declared)}",
            remedy="restore it, or remove it from sidecar_manifests to unregister its records",
        )
        source = ManifestSource(declared=declared, resolved=resolved)
        sources.append(source)
        for registration in _read_manifest(source, project_root):
            earlier = by_target.get(registration.target)
            if earlier is not None:
                msg = (
                    f"{earlier.location} and {registration.location} both register "
                    f"{format_path_for_display(registration.target)}; a Markdown file is owned "
                    "by exactly one record, so remove one of them"
                )
                raise RegistrationConflictError(msg)
            by_target[registration.target] = registration
    return RegistrationIndex(manifests=tuple(sources), by_target=by_target)


def _read_manifest(source: ManifestSource, project_root: Path) -> list[Registration]:
    """Load one manifest and validate each record in it."""
    shown = format_path_for_display(source.declared)
    try:
        text = source.resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read manifest {shown}: {exc}"
        raise ManifestError(msg) from exc
    data = _load(text, shown)
    if not isinstance(data, dict):
        msg = (
            f"manifest {shown} does not hold a YAML mapping; a manifest is a mapping whose "
            f"one key is '{_NODES_KEY}'"
        )
        raise ManifestError(msg)
    extra = sorted(repr(key) for key in data if key != _NODES_KEY)
    if extra:
        msg = (
            f"manifest {shown} has unknown top-level keys {', '.join(extra)}; its one key "
            f"is '{_NODES_KEY}'"
        )
        raise ManifestError(msg)
    nodes = data.get(_NODES_KEY)
    if not isinstance(nodes, list) or not nodes:
        msg = (
            f"manifest {shown} must declare '{_NODES_KEY}' as a non-empty list of records; "
            "to unregister every record, remove the manifest from sidecar_manifests instead"
        )
        raise ManifestError(msg)
    return [
        _validate_record(record, source, position, project_root)
        for position, record in enumerate(nodes)
    ]


def _load(text: str, shown: str) -> Any:
    """Refuse node-reuse spellings, then load the manifest's untyped value.

    Args:
        text: The manifest source.
        shown: The manifest's display spelling, for diagnostics.

    Returns:
        The loaded value, still untyped.

    Raises:
        ManifestError: If the manifest cannot be parsed or spells an anchor, alias, or merge key.
    """
    try:
        reuse = _LOADER.first_reuse_spelling(text)
        if reuse is not None:
            # Refused anywhere, not only across records: a merge key inheriting `seen` would let
            # reconciling one node rewrite another's acknowledgement (AD-51). ManifestError is
            # not in YAML_LOAD_ERRORS, so the handler below does not re-wrap it.
            msg = (
                f"manifest {shown} spells a YAML {reuse.kind} at line {reuse.line}, column "
                f"{reuse.column}; manifests refuse anchors, aliases, and merge keys, so write "
                "each value out in full"
            )
            raise ManifestError(msg)
        return _LOADER.load(text)
    except YAML_LOAD_ERRORS as exc:
        detail = format_yaml_error_for_display(exc)
        msg = f"cannot parse manifest {shown}: {detail}"
        raise ManifestError(msg) from exc


def _validate_record(
    record: object, source: ManifestSource, position: int, project_root: Path
) -> Registration:
    """Validate one record's keys, path spelling, target, and metadata, in that order."""
    if not isinstance(record, dict):
        where = _record_location(source.declared, position, None)
        msg = f"{where} is not a mapping; a record is a mapping of exactly 'path' and 'meta'"
        raise ManifestError(msg)
    raw_path = record.get("path")
    declared_path = raw_path if isinstance(raw_path, str) else None
    where = _record_location(source.declared, position, declared_path)
    keys = set(record)
    if keys != _RECORD_KEYS:
        missing = sorted(repr(key) for key in _RECORD_KEYS - keys)
        extra = sorted(repr(key) for key in keys - _RECORD_KEYS)
        problems = []
        if missing:
            problems.append(f"missing {', '.join(missing)}")
        if extra:
            problems.append(f"unknown {', '.join(extra)}")
        msg = (
            f"{where} has the wrong keys ({'; '.join(problems)}); a record is a mapping of "
            "exactly 'path' and 'meta'"
        )
        raise ManifestError(msg)
    if declared_path is None:
        msg = f"{where} has a 'path' that is not a string; write a relative '.md' path"
        raise ManifestError(msg)
    _check_path_spelling(declared_path, where)
    target = _resolve_regular_file(
        declared_path, project_root, subject=where, remedy="restore it, or remove the record"
    )
    try:
        meta = NodeMeta.model_validate(record.get("meta"))
    except ValidationError as exc:
        msg = format_validation_error(
            exc,
            header=f"invalid metadata in {where}:",
            model=NodeMeta,
            root_label=_META_ROOT_LABEL,
        )
        raise ManifestError(msg) from exc
    return Registration(
        declared_path=declared_path,
        target=target,
        manifest=source,
        position=position,
        meta=meta,
    )


def _check_path_spelling(declared_path: str, where: str) -> None:
    """Require a project-root-relative POSIX spelling of a ``.md`` file, before any resolution.

    Checked on the string as written, because resolving first would normalize away exactly the
    spelling the record keeps as its identity.
    """
    problem = None
    control = describe_first_control_char(declared_path)
    if control is not None:
        problem = f"contains a control character ({control})"
    elif "\\" in declared_path:
        problem = "uses a backslash; separate segments with '/'"
    elif PurePosixPath(declared_path).is_absolute():
        problem = "is absolute; write it relative to the project root"
    elif not declared_path.endswith(_MARKDOWN_SUFFIX):
        problem = f"does not name a '{_MARKDOWN_SUFFIX}' file"
    if problem is not None:
        msg = f"{where} has a 'path' that {problem}"
        raise ManifestError(msg)


def _resolve_regular_file(declared: str, project_root: Path, *, subject: str, remedy: str) -> Path:
    """Resolve a declared path inside the project and require an existing regular file.

    Manifests and record targets share this rule (AD-51). An absolute ``declared`` replaces
    ``project_root`` in the join, so both spellings reach the same containment check.

    Args:
        declared: The path as the configuration or the record spelled it.
        project_root: The root the path resolves against and must stay inside.
        subject: The already-displayed phrase naming what declared the path.
        remedy: What to do when the path does not exist.

    Returns:
        The contained, symlink-followed regular file.

    Raises:
        ManifestError: If the path escapes the project root, does not exist, or is not a regular
            file.
    """
    try:
        resolved = safe_resolve(project_root / declared, project_root)
    except ValueError as exc:
        msg = (
            f"{subject} resolves outside the project root "
            f"{format_path_for_display(project_root)}; it must stay inside the project"
        )
        raise ManifestError(msg) from exc
    if not resolved.exists():
        msg = f"{subject} does not exist; {remedy}"
        raise ManifestError(msg)
    if not resolved.is_file():
        msg = (
            f"{subject} resolves to {format_path_for_display(resolved)}, which is not a regular "
            "file"
        )
        raise ManifestError(msg)
    return resolved


def _record_location(manifest: str, position: int, declared_path: str | None) -> str:
    """Spell a record's location: its manifest, its position, and its path when it has one.

    Args:
        manifest: The manifest's declared spelling.
        position: The record's 0-based index in ``nodes``.
        declared_path: The record's ``path`` string, or None when it carries no string path.

    Returns:
        A phrase such as ``record nodes[2] (path 'skills/a.md') in manifest 'sidecars.yml'``.
    """
    spelled = f"record {_NODES_KEY}[{position}]"
    if declared_path is not None:
        spelled = f"{spelled} (path {format_path_for_display(declared_path)})"
    return f"{spelled} in manifest {format_path_for_display(manifest)}"
