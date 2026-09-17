"""Wire config, discovery, parsing, and loading into a Lattice."""

import os
import warnings
from collections.abc import Callable
from pathlib import Path

from .cache import CacheHit, LookupPolicy, RunState, cache_path, lookup, make_entry, store
from .config import ProjectConfig, SidecarProjectConfig
from .constants import COMMENT_ENVELOPE_OPEN, FrontmatterDisposition
from .discovery import decode_doc, discover_doc_candidates, read_doc_bytes
from .error_types import DocumentError, RegistrationConflictError
from .frontmatter_parser import parse_document
from .loader import build_lattice, derive_file_sections
from .model import DocumentOrigin, ExternalDeclaration, FileFacts, Lattice, ParsedDoc, ParsedMeta
from .path_utils import format_path_for_display
from .sidecar_manifest import RegistrationIndex, build_registration_index


def load_lattice(
    project: ProjectConfig | SidecarProjectConfig,
    *,
    require_verified: bool = False,
    persist_cache: bool = True,
) -> Lattice:
    """Discover, parse, and assemble the lattice for a project.

    With ``cache_key`` unset this is today's full parse of every discovered file. With it set,
    each file is served from the incremental load cache when unchanged and the cache is
    rewritten after a successful build.

    Args:
        project: Loaded ordinary or sidecar-aware config with contained docs roots.
        require_verified: Force the verify tier for every file, disabling the stat fast tier.
            Set only by the reconcile CLI path, whose writes must never derive from stale
            content.
        persist_cache: Whether a cache-enabled load may persist its final cache state. Read-only
            commands pass False while retaining verified cache reads.

    Returns:
        The built Lattice. Registrations enroll files whose inline classifier declines ownership.
        Other id-less files are omitted, with a warning for an id-less fenced mapping.

    Raises:
        FrontmatterError: If a discovered file's frontmatter has an unknown or malformed key,
            declares lattice intent with no ``id``, spells a near-miss comment opener on line 1,
            carries a byte-order mark before a comment opener, holds ``--`` inside a comment
            envelope, or is a comment envelope whose body is not a mapping carrying ``id``.
        UnreadableDocError: If a discovered file cannot be read or decoded, or its frontmatter
            opens a fence or a comment envelope it never closes, or cannot be parsed as YAML.
        DuplicateIdError: If two loaded files, or two headings in one file, register the same id.
        ManifestError: If a fresh manifest or its declared target fails validation.
        RegistrationConflictError: If metadata owners collide or a manifest is also a node.
    """
    declarations = ()
    if isinstance(project, SidecarProjectConfig):
        declarations, project = project.sidecar_manifests, project.project
    registrations = build_registration_index(declarations, project.project_root)
    if project.config.cache_key is None:
        return _assemble(
            project,
            registrations,
            lambda path, target: _parse_file_facts(decode_doc(path, read_doc_bytes(target)), path),
        )
    return _load_cached(
        project,
        registrations,
        require_verified=require_verified,
        persist_cache=persist_cache,
    )


def _report_skip(disposition: FrontmatterDisposition, path: Path) -> None:
    """Report a file skipped for frontmatter that carries no ``id``.

    Every load path funnels through this one call rather than warning where it happens to
    notice the skip. Python renders a warning with the source location it was raised from and
    suppresses repeats by that same location, so a second call site for the cache-replay path
    would change both the rendered line and when it is shown, and a warm run would not match
    the cold run it replays. ``stacklevel`` stays at its default 1 for that reason: it pins the
    location to this function instead of to whichever caller reached it.

    Args:
        disposition: What the parse concluded about the file. Only ``"id-less"`` is reported;
            a tracked file has nothing to say and untracked prose is not a skip.
        path: The discovered path as this checkout sees it, named in the message.
    """
    if disposition != "id-less":
        return
    warnings.warn(
        f"skipping {format_path_for_display(path)}: its frontmatter declares no 'id', so it "
        "is not a lattice node",
        stacklevel=1,
    )


def _report_reused_anchors(reused: bool, path: Path) -> None:
    """Report a tracked file whose frontmatter defines one anchor name more than once.

    A separate function from ``_report_skip`` rather than a branch inside it, for the reason
    that shapes ``_report_skip`` itself: Python renders a warning with its raising location and
    filters repeats by that location, so two diagnostics sharing one site would suppress each
    other. ``stacklevel`` stays at its default 1 for the same reason, and every load path funnels
    here so a warm run reproduces what the cold run it replays said.

    The message deliberately does not open with ``skipping ``. AD-29 records that
    ``PYTHONWARNINGS`` cannot single out the id-less skip because ``discovery.py``'s
    symlink-escape warning shares that prefix; a distinct opening gives this one the targetability
    those two lack, and it is not a skip in any case.

    Args:
        reused: Whether the parse that produced this file's node saw an anchor name defined
            twice. Only ever true for a tracked file: a rebound alias in a file the lattice does
            not hold changes no edge, so there is nothing actionable to say about it.
        path: The discovered path as this checkout sees it, named in the message.
    """
    if not reused:
        return
    warnings.warn(
        f"reused anchor in {format_path_for_display(path)}: its frontmatter defines an anchor "
        "name more than once, so each alias reads the nearest definition above it",
        stacklevel=1,
    )


def _misplaced_envelope_message(path: Path) -> str:
    """Build the shared inline warning and external refusal explanation."""
    return (
        f"misplaced doc-lattice envelope in {format_path_for_display(path)}: the "
        f"'{COMMENT_ENVELOPE_OPEN}' opener is only read as the file's first line, so this file "
        "is not a lattice node; move the envelope to the top of the file, removing any '---' "
        "frontmatter fence it would have to displace"
    )


def _report_misplaced_envelope(disposition: FrontmatterDisposition, path: Path) -> None:
    """Report an untracked file carrying the comment envelope where it will not be read.

    A separate function from ``_report_skip`` for the reason that shapes ``_report_skip``
    itself: Python renders a warning with its raising location and filters repeats by that
    location, so two diagnostics sharing one site would suppress each other. ``stacklevel``
    stays at its default 1 for the same reason, and every load path funnels here so a warm run
    reproduces what the cold run it replays said.

    The message deliberately does not open with ``skipping ``, matching
    ``_report_reused_anchors``: AD-29 records that ``PYTHONWARNINGS`` cannot single out the
    id-less skip because ``discovery.py``'s symlink-escape warning shares that prefix.

    Args:
        disposition: What the parse concluded about the file. Only ``"misplaced-envelope"``
            is reported.
        path: The discovered path as this checkout sees it, named in the message.
    """
    if disposition != "misplaced-envelope":
        return
    warnings.warn(_misplaced_envelope_message(path), stacklevel=1)


def _shadowed_envelope_message(path: Path) -> str:
    """Build the shared inline warning and external refusal explanation."""
    return (
        f"shadowed doc-lattice envelope in {format_path_for_display(path)}: this file is tracked "
        f"under its '---' frontmatter fence, so the '{COMMENT_ENVELOPE_OPEN}' envelope below it "
        "is read as body text and its metadata is ignored; replace both delimiters of the fence "
        "in the same edit, or remove the envelope"
    )


def _report_shadowed_envelope(shadowed: bool, path: Path) -> None:
    """Report a file tracked under its fence that also carries a comment envelope below it.

    A separate function from ``_report_misplaced_envelope`` for the reason that shapes both:
    Python renders a warning with its raising location and filters repeats by that location, so
    two diagnostics sharing one site would suppress each other. ``stacklevel`` stays at its
    default 1 for the same reason, and every load path funnels here so a warm run reproduces what
    the cold run it replays said.

    The message is its own rather than a reuse of the misplaced one, whose "so this file is not a
    lattice node" is false here: the file is a node, under the fence, and the envelope is what
    was ignored. This is the half-converted state the 7.0.0 migration warns about, reached by
    adding the envelope and not removing the fence it has to displace.

    The ``shadowed `` opening is deliberate and is the third distinct prefix in this module.
    README documents ``PYTHONWARNINGS=ignore:misplaced`` as targeting exactly one diagnostic, and
    opening this one the same way would silently widen that filter to cover both.

    Args:
        shadowed: Whether the parse found a comment envelope in the body of a file its fence
            already tracked. Only ever true for a tracked file: a file that is not a node has no
            fence metadata for an envelope to be shadowed by, and reaches
            ``_report_misplaced_envelope`` instead.
        path: The discovered path as this checkout sees it, named in the message.
    """
    if not shadowed:
        return
    warnings.warn(_shadowed_envelope_message(path), stacklevel=1)


def _report_load_diagnostics(outcome: ParsedMeta, path: Path) -> None:
    """Report everything one loaded file has to say, from the single site AD-29 requires.

    Every load path (cache-free, cache-miss, and cache-hit) funnels through here, so a warm run
    reproduces exactly what the cold run it replays said. A new diagnostic is added here once
    rather than at each tier, which is what stops the warm path from silently going quiet.

    The individual reporters stay separate functions so each keeps its own ``warnings.warn``
    call site: the per-location dedup their docstrings depend on is unaffected by this extra
    frame, since each warn passes ``stacklevel=1`` and is attributed to its own line.

    Args:
        outcome: The parse result, freshly parsed or reconstructed from the cache.
        path: The discovered path as this checkout sees it.
    """
    _report_skip(outcome.disposition, path)
    _report_reused_anchors(outcome.reused_anchors, path)
    _report_misplaced_envelope(outcome.disposition, path)
    _report_shadowed_envelope(outcome.shadowed_envelope, path)


def _body_first_line(text: str, body: str) -> int:
    """Return the 1-based file line that the body's first line occupies.

    The parse returns the body as a verbatim suffix of the file text, so the prefix the envelope
    consumed is what the two lengths differ by and its newline count is the offset. A BOM
    carries no newline. An envelope-free file hands back the whole text, which lands on 1;
    a recognized fence is consumed even if its contents classify as untracked.

    Args:
        text: The decoded file text handed to the parse.
        body: The verbatim body the parse returned for it.

    Returns:
        The file line number of body line 1.
    """
    return text[: len(text) - len(body)].count("\n") + 1


def _parse_file_facts(text: str, path: Path) -> FileFacts:
    """Derive complete file-local facts from normalized text, independent of enrollment."""
    outcome, body = parse_document(text, path)
    first_line = _body_first_line(text, body)
    return FileFacts(
        parsed=outcome,
        body=body,
        body_first_line=first_line,
        sections=derive_file_sections(body, first_line=first_line),
    )


def _ownership_explanation(outcome: ParsedMeta, path: Path) -> str:
    """Describe the inline classification that refuses external ownership."""
    if outcome.shadowed_envelope:
        return _shadowed_envelope_message(path)
    if outcome.disposition == "misplaced-envelope":
        return _misplaced_envelope_message(path)
    return "this file is already tracked by its inline metadata"


def _assemble(
    project: ProjectConfig,
    registrations: RegistrationIndex,
    acquire_facts: Callable[[Path, Path], FileFacts],
) -> Lattice:
    """Join fresh ownership claims and file-local facts before registering any node ids."""
    identities = {
        registration.target: project.project_root / registration.declared_path
        for registration in registrations.registrations
    }
    for candidate in discover_doc_candidates(
        project.resolved_roots, project.config.ignore_globs, project.project_root
    ):
        identities.setdefault(candidate.target, candidate.path)
    manifests = {source.resolved: source for source in registrations.manifests}
    parsed: list[ParsedDoc] = []
    for target, path in sorted(identities.items(), key=lambda item: item[1]):
        registration = registrations.by_target.get(target)
        manifest = manifests.get(target)
        if registration is not None and manifest is not None:
            raise RegistrationConflictError(
                f"manifest {format_path_for_display(manifest.declared)} is also the Markdown "
                f"target of {registration.location}; a manifest must never be a node"
            )
        try:
            # A declared spelling can contain absent/../ components. Read its validated
            # target, while keeping the spelling for identity, cache keys, and parse errors.
            facts = acquire_facts(path, target if registration is not None else path)
        except DocumentError as exc:
            if registration is not None:
                exc.add_note(f"external enrollment declared by {registration.location}")
            raise
        outcome = facts.parsed
        if manifest is not None and outcome.meta is not None:
            raise RegistrationConflictError(
                f"manifest {format_path_for_display(manifest.declared)} is also inline node "
                f"{outcome.meta.id!r} at {format_path_for_display(path)}; "
                "a manifest must never be a node"
            )
        origin = DocumentOrigin(path)
        if registration is not None:
            if outcome.disposition not in {"untracked", "id-less"} or outcome.shadowed_envelope:
                explanation = _ownership_explanation(outcome, path)
                raise RegistrationConflictError(
                    f"Markdown {format_path_for_display(path)} claimed by {registration.location}: "
                    f"{explanation}; remove the inline declaration or the manifest record"
                )
            meta = registration.meta
            origin = DocumentOrigin(
                path,
                ExternalDeclaration(
                    registration.manifest.declared,
                    registration.position,
                    registration.declared_path,
                ),
            )
        else:
            _report_load_diagnostics(outcome, path)
            meta = outcome.meta
        if meta is not None:
            parsed.append(ParsedDoc(path, meta, facts.body, facts.sections, origin))
    return build_lattice(parsed)


def _load_cached(
    project: ProjectConfig,
    registrations: RegistrationIndex,
    *,
    require_verified: bool,
    persist_cache: bool,
) -> Lattice:
    """The incremental load path. Writes the cache only after a successful build."""
    config = project.config
    # ty cannot narrow cache_key: str | None from the caller's is-None branch across the call;
    # this assert documents and enforces that invariant for cache_path's str parameter.
    assert config.cache_key is not None  # noqa: S101
    path = cache_path(config.cache_key, os.environ)
    snapshot = store.load(path)
    resolved_root = project.project_root.resolve()
    current_root = str(resolved_root)
    state = RunState.begin(snapshot.cache, current_root)
    effective_trust = config.cache_trust_stat and not require_verified
    policy = LookupPolicy(current_root=current_root, trust_stat=effective_trust)

    def acquire_facts(doc_path: Path, read_path: Path) -> FileFacts:
        rel_key = doc_path.relative_to(resolved_root).as_posix()
        result = lookup.resolve(state.entry(rel_key), read_path, policy)
        if isinstance(result, CacheHit):
            state.claim(rel_key, result.refreshed_stat)
            return result.facts
        facts = _parse_file_facts(decode_doc(doc_path, result.data), doc_path)
        state.replace(rel_key, make_entry(result.data, facts, result.stat, current_root))
        return facts

    lattice = _assemble(project, registrations, acquire_facts)
    if persist_cache:
        store.save_if_changed(path, state.complete(), snapshot.baseline)
    return lattice
