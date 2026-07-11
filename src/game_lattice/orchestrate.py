"""Wire config, discovery, parsing, and loading into a Lattice."""

import os

from .cache import CacheHit, LoadCache
from .config import ProjectConfig
from .discovery import decode_doc, discover_doc_paths, read_doc
from .frontmatter_parser import parse_meta, split_frontmatter
from .loader import build_lattice, derive_file_sections
from .model import Lattice, ParsedDoc


def load_lattice(project: ProjectConfig, *, require_verified: bool = False) -> Lattice:
    """Discover, parse, and assemble the lattice for a project.

    With ``cache_key`` unset this is today's full parse of every discovered file. With it set,
    each file is served from the incremental load cache when unchanged and the cache is
    rewritten after a successful build.

    Args:
        project: The loaded project config with contained docs roots.
        require_verified: Force the verify tier for every file, disabling the stat fast tier.
            Set only by the reconcile CLI path, whose writes must never derive from stale
            content.

    Returns:
        The built Lattice. Files without lattice frontmatter (no ``id``) are skipped.
    """
    if project.config.cache_key is None:
        return _load_uncached(project)
    return _load_cached(project, require_verified=require_verified)


def _load_uncached(project: ProjectConfig) -> Lattice:
    """Today's cache-free load path, unchanged."""
    parsed: list[ParsedDoc] = []
    for path in discover_doc_paths(project.resolved_roots, project.config.ignore_globs):
        text = read_doc(path)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, path)
        if meta is None:
            continue
        parsed.append(ParsedDoc(path=path, meta=meta, body=body))
    return build_lattice(parsed)


def _load_cached(project: ProjectConfig, *, require_verified: bool) -> Lattice:
    """The incremental load path. Writes the cache only after a successful build."""
    config = project.config
    # ty cannot narrow cache_key: str | None from the caller's is-None branch across the call;
    # this assert documents and enforces that invariant for LoadCache.open's str parameter.
    assert config.cache_key is not None  # noqa: S101
    cache = LoadCache.open(
        cache_key=config.cache_key,
        project_root=project.project_root,
        env=os.environ,
        trust_stat=config.cache_trust_stat,
        require_verified=require_verified,
    )
    parsed: list[ParsedDoc] = []
    discovered: set[str] = set()
    for path in discover_doc_paths(project.resolved_roots, config.ignore_globs):
        rel_key = path.relative_to(project.project_root).as_posix()
        discovered.add(rel_key)
        result = cache.lookup(rel_key, path)
        if isinstance(result, CacheHit):
            if result.doc is not None:
                parsed.append(result.doc)
            continue
        text = decode_doc(path, result.data)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, path)
        sections = derive_file_sections(body) if meta is not None else None
        cache.record_miss(rel_key, result.data, meta, body, sections, result.stat)
        if meta is not None:
            parsed.append(ParsedDoc(path=path, meta=meta, body=body, sections=sections))
    lattice = build_lattice(parsed)
    cache.finalize(discovered)
    return lattice
