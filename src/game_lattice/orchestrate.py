"""Wire config, discovery, parsing, and loading into a Lattice."""

from .config import ProjectConfig
from .discovery import discover_doc_paths, read_doc
from .frontmatter_parser import parse_meta, split_frontmatter
from .loader import build_lattice
from .model import Lattice, ParsedDoc


def load_lattice(project: ProjectConfig) -> Lattice:
    """Discover, parse, and assemble the lattice for a project.

    Args:
        project: The loaded project config with contained docs roots.

    Returns:
        The built Lattice. Files without lattice frontmatter (no ``id``) are skipped.
    """
    parsed: list[ParsedDoc] = []
    for path in discover_doc_paths(project.resolved_roots, project.config.ignore_globs):
        text = read_doc(path)
        raw_meta, body = split_frontmatter(text)
        meta = parse_meta(raw_meta, path)
        if meta is None:
            continue
        parsed.append(ParsedDoc(path=path, meta=meta, body=body))
    return build_lattice(parsed)
