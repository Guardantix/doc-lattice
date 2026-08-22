#!/usr/bin/env python3
"""Verify every relative link and heading fragment in the maintained root documents.

The link sources are the sorted root ``*.md`` files, which is the mechanical stand-in for the
ownership list in CLAUDE.md and keeps ``docs/`` staging out of the source set. A link target may
be any repository-contained relative path, staged documents included.

Absolute and external destinations are out of scope and skipped, as are image destinations.
Heading fragments are validated only against Markdown targets, and only against the heading
grammar the pinned compatibility adapter supports: top-level, column-zero ATX headings. Fragments
resolve through ``markdown_compat.github_heading_ids``, so a repeated heading is addressable at
the document-order id GitHub gives it rather than collapsing onto one base slug.
"""

import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import SplitResult, unquote, urlsplit

from markdown_it import MarkdownIt

from doc_lattice.markdown_compat import extract_headings, github_heading_ids

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PARSER = MarkdownIt("commonmark")
_MARKDOWN_SUFFIX = ".md"


@dataclass(frozen=True, slots=True)
class Link:
    """One Markdown link destination with the 1-based line its block starts on."""

    href: str
    line: int


def extract_links(markdown: str) -> list[Link]:
    """Return every link destination the pinned parser recognizes, in document order.

    Destinations come from parsed link tokens rather than a text scan, so a reference-style
    link is followed to its definition and link-like text inside inline or fenced code is not
    a link at all.

    Args:
        markdown: Markdown document text.

    Returns:
        One entry per link with a non-empty destination. The line is where the link's
        containing block starts, which is what the token stream records.
    """
    links: list[Link] = []
    for token in _PARSER.parse(markdown):
        if token.type != "inline" or token.children is None:
            continue
        line = token.map[0] + 1 if token.map is not None else 1
        for child in token.children:
            if child.type != "link_open":
                continue
            href = child.attrGet("href")
            if isinstance(href, str) and href:
                links.append(Link(href=href, line=line))
    return links


def maintained_documents(repo_root: Path) -> list[Path]:
    """Return the maintained link sources: the sorted root Markdown files.

    Args:
        repo_root: The repository root.

    Returns:
        Every root ``*.md`` file in sorted order. Nested directories are not sources.
    """
    return sorted(path for path in repo_root.glob(f"*{_MARKDOWN_SUFFIX}") if path.is_file())


def _contained_parts(base: PurePosixPath, relative: str) -> tuple[str, ...] | None:
    """Join a relative destination onto a base directory without leaving the repository.

    The join is lexical on purpose: resolving through the filesystem would follow symlinks and
    report a link that GitHub renders perfectly well as escaping the repository.

    Args:
        base: The source document's directory, relative to the repository root.
        relative: The decoded relative destination.

    Returns:
        The target's parts relative to the repository root, or None when the destination
        climbs above it.
    """
    parts = list(base.parts)
    for part in PurePosixPath(relative).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return tuple(parts)


def _heading_ids(document: Path, cache: dict[Path, frozenset[str]]) -> frozenset[str]:
    """Return the addressable GitHub heading ids of a Markdown document, memoized."""
    if document not in cache:
        headings = extract_headings(document.read_text(encoding="utf-8"))
        cache[document] = frozenset(github_heading_ids(headings))
    return cache[document]


def _fragment_message(
    fragment: str,
    target: Path,
    repo_root: Path,
    cache: dict[Path, frozenset[str]],
) -> str | None:
    """Return a diagnostic when a fragment matches no heading in a Markdown target."""
    if fragment in _heading_ids(target, cache):
        return None
    return (
        f"fragment '#{fragment}' matches no heading in {target.relative_to(repo_root).as_posix()}"
    )


def _is_out_of_scope(parts: SplitResult) -> bool:
    """Report whether a destination is external or absolute rather than repository-relative.

    Args:
        parts: The split destination.

    Returns:
        True for any scheme, for the protocol-relative form, and for a root-absolute path.
    """
    return bool(parts.scheme) or bool(parts.netloc) or parts.path.startswith("/")


def _resolve_target(raw_path: str, document: Path, repo_root: Path) -> Path | None:
    """Return the repository path a relative destination names, or None when it escapes."""
    source_dir = PurePosixPath(document.parent.relative_to(repo_root).as_posix())
    parts = _contained_parts(source_dir, unquote(raw_path))
    return None if parts is None else repo_root.joinpath(*parts)


def _link_message(
    link: Link,
    document: Path,
    repo_root: Path,
    cache: dict[Path, frozenset[str]],
) -> str | None:
    """Return a diagnostic for one link, or None when it resolves.

    A destination that does not exist yields exactly one message: the fragment is not also
    reported, because a fragment on a target nobody can open says nothing new.

    A query is a view parameter, not part of the filename, so it is split off before the
    target is resolved. It also suppresses heading validation: GitHub's ``?plain=1`` renders
    source rather than headings, where a fragment is a line reference such as ``#L5`` and no
    heading id could match it.

    Args:
        link: The link to resolve.
        document: The source document the link was written in.
        repo_root: The repository root every target must stay inside.
        cache: Heading ids already read, keyed by target path.

    Returns:
        The diagnostic text without its ``file:line`` prefix, or None.
    """
    href = link.href
    parts = urlsplit(href)
    if _is_out_of_scope(parts):
        return None
    fragment = unquote(parts.fragment)
    if not parts.path:
        return _fragment_message(fragment, document, repo_root, cache) if fragment else None
    target = _resolve_target(parts.path, document, repo_root)
    if target is None:
        return f"link target {href!r} escapes the repository"
    if not target.exists():
        return f"link target {href!r} does not exist"
    if not fragment or parts.query or target.suffix != _MARKDOWN_SUFFIX or not target.is_file():
        return None
    return _fragment_message(fragment, target, repo_root, cache)


def check_repository_links(repo_root: Path) -> list[str]:
    """Return one message per unresolvable link in the maintained documents.

    Args:
        repo_root: The repository root, which is both the link-source directory and the
            containment boundary every relative target must stay inside.

    Returns:
        Messages in document order, sources sorted by name. Each names the source document,
        the line its link's block starts on, and the destination as written. An empty list
        means every relative destination and heading fragment resolves.
    """
    root = repo_root.resolve()
    cache: dict[Path, frozenset[str]] = {}
    messages: list[str] = []
    for document in maintained_documents(root):
        source = document.relative_to(root).as_posix()
        for link in extract_links(document.read_text(encoding="utf-8")):
            message = _link_message(link, document, root, cache)
            if message is not None:
                messages.append(f"{source}:{link.line}: {message}")
    return messages


def main() -> None:
    """Check every maintained document and exit non-zero on any unresolvable link."""
    messages = check_repository_links(_REPO_ROOT)
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(1 if messages else 0)


if __name__ == "__main__":
    main()
