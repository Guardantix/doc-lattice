"""Wire config, discovery, parsing, and loading into a Lattice."""

import os

from .cache import CacheHit, LookupPolicy, RunState, cache_path, lookup, make_entry, store
from .config import ProjectConfig
from .discovery import decode_doc, discover_doc_paths, read_doc
from .frontmatter_parser import parse_meta, split_frontmatter
from .loader import build_lattice, derive_file_sections
from .model import Lattice, ParsedDoc


def load_lattice(
    project: ProjectConfig,
    *,
    require_verified: bool = False,
    persist_cache: bool = True,
) -> Lattice:
    """Discover, parse, and assemble the lattice for a project.

    With ``cache_key`` unset this is today's full parse of every discovered file. With it set,
    each file is served from the incremental load cache when unchanged and the cache is
    rewritten after a successful build.

    Args:
        project: The loaded project config with contained docs roots.
        require_verified: Force the verify tier for every file, disabling the stat fast tier.
            Set only by the reconcile CLI path, whose writes must never derive from stale
            content.
        persist_cache: Whether a cache-enabled load may persist its final cache state. Read-only
            commands pass False while retaining verified cache reads.

    Returns:
        The built Lattice. Files without lattice frontmatter (no ``id``) are skipped.
    """
    if project.config.cache_key is None:
        return _load_uncached(project)
    return _load_cached(
        project,
        require_verified=require_verified,
        persist_cache=persist_cache,
    )


def _load_uncached(project: ProjectConfig) -> Lattice:
    """Today's cache-free load path, unchanged."""
    parsed: list[ParsedDoc] = []
    for path in discover_doc_paths(
        project.resolved_roots, project.config.ignore_globs, project.project_root
    ):
        text = read_doc(path)
        raw_meta, body = split_frontmatter(text, path)
        meta = parse_meta(raw_meta, path)
        if meta is None:
            continue
        parsed.append(ParsedDoc(path=path, meta=meta, body=body))
    return build_lattice(parsed)


def _load_cached(
    project: ProjectConfig,
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
    parsed: list[ParsedDoc] = []
    for doc_path in discover_doc_paths(
        project.resolved_roots, config.ignore_globs, project.project_root
    ):
        rel_key = doc_path.relative_to(resolved_root).as_posix()
        result = lookup.resolve(state.entry(rel_key), doc_path, policy)
        if isinstance(result, CacheHit):
            state.claim(rel_key, result.refreshed_stat)
            if result.doc is not None:
                parsed.append(result.doc)
            continue
        text = decode_doc(doc_path, result.data)
        raw_meta, body = split_frontmatter(text, doc_path)
        meta = parse_meta(raw_meta, doc_path)
        sections = derive_file_sections(body) if meta is not None else None
        state.replace(
            rel_key,
            make_entry(result.data, meta, body, sections, result.stat, current_root),
        )
        if meta is not None:
            parsed.append(ParsedDoc(path=doc_path, meta=meta, body=body, sections=sections))
    lattice = build_lattice(parsed)
    if persist_cache:
        store.save_if_changed(path, state.complete(), snapshot.baseline)
    return lattice
