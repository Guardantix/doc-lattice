#!/usr/bin/env python3
"""Verify every relative link and heading fragment in the maintained root documents.

The link sources are the sorted root ``*.md`` files, which is the mechanical stand-in for the
ownership list in CLAUDE.md and keeps ``docs/`` staging out of the source set. A link target may
be any repository-contained relative path, staged documents included. Containment binds both
ends: a source that leaves the repository through a symlink is reported rather than read.

Absolute and external destinations are out of scope and skipped, as are image destinations.
Heading fragments are validated only against Markdown targets, and only against the heading
grammar the pinned compatibility adapter supports: top-level, column-zero ATX headings. Fragments
resolve through ``markdown_compat.github_heading_ids``, so a repeated heading is addressable at
the document-order id GitHub gives it rather than collapsing onto one base slug.

Destinations are read from Markdown link tokens and from the ``href`` of a raw HTML anchor,
which markdown-it hands over as raw HTML rather than as a link. Both forms resolve through the
same pipeline, so the gate cannot go green on the one link form it does not model.
"""

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from doc_lattice.markdown_compat import extract_headings, github_heading_ids
from doc_lattice.path_utils import format_path_for_display

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PARSER = MarkdownIt("commonmark")
_MARKDOWN_SUFFIX = ".md"
# The plain-Markdown suffixes GitHub renders through Linguist, matched case-blind: a fragment on
# any of them is a heading id exactly as it is on ``.md``, so skipping them would leave a dead
# anchor green. Suffixes whose renderer changes the heading grammar are deliberately absent --
# ``.mdx`` can generate headings from components and ``.rmd``/``.qmd`` are knitted first -- since
# validating those against this adapter's grammar would fail a working link.
_MARKDOWN_TARGET_SUFFIXES = frozenset(
    {_MARKDOWN_SUFFIX, ".markdown", ".mdown", ".mdwn", ".mkd", ".mkdn"}
)
# The WHATWG URL Standard's dot-segment spellings, matched ASCII case-insensitively in its
# path state. A browser normalizes the encoded forms exactly as it does the bare ones, so a
# link written with them resolves and this checker has to agree.
_SINGLE_DOT_SEGMENTS = frozenset({".", "%2e"})
_DOUBLE_DOT_SEGMENTS = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})
_ESCAPING_SOURCE_MESSAGE = "maintained document leaves the repository through a symlink"


@dataclass(frozen=True, slots=True)
class Link:
    """One Markdown link destination with the 1-based line its block starts on."""

    href: str
    line: int


def _walk(tokens: list[Token]) -> Iterator[tuple[int, Token]]:
    """Yield every token with the 1-based line its containing block starts on.

    Flattening block tokens and their inline children into one sequence keeps the destination
    scan a single loop and the line-attribution rule in one place. An inline token's children
    inherit its line, which is the only line the token stream records for them.

    Args:
        tokens: A parsed token stream.

    Yields:
        Each block token followed by its inline children, in document order.
    """
    for token in tokens:
        line = token.map[0] + 1 if token.map is not None else 1
        yield line, token
        if token.type == "inline" and token.children is not None:
            for child in token.children:
                yield line, child


def _destinations_in(tokens: list[Token]) -> list[Link]:
    """Return every link destination in a parsed document, in document order.

    Both forms are collected in the one pass, so a Markdown link and a raw anchor interleave
    by position rather than being grouped by kind and re-sorted afterwards.

    An anchor's line is resolved within its own HTML, so an anchor several lines into a
    ``<details>`` block reports that line rather than the line the block opens on.

    Args:
        tokens: A parsed token stream.

    Returns:
        One entry per destination, in document order.
    """
    links: list[Link] = []
    for line, token in _walk(tokens):
        if token.type == "link_open":
            href = token.attrGet("href")
            if isinstance(href, str) and href:
                links.append(Link(href=href, line=line))
        elif token.type in {"html_block", "html_inline"}:
            links.extend(
                Link(href=href, line=line + offset - 1)
                for offset, href in _anchor_hrefs(token.content)
            )
    return links


def extract_links(markdown: str) -> list[Link]:
    """Return every link destination the pinned parser recognizes, in document order.

    Destinations come from parsed tokens rather than a text scan, so a reference-style link
    is followed to its definition and link-like text inside inline or fenced code is not a
    link at all. Markdown-it classifies a raw ``<a href=...>`` as HTML rather than as a link,
    so those destinations are read out of the HTML it hands over and reported here too.

    Args:
        markdown: Markdown document text.

    Returns:
        One entry per destination with a non-empty value. A Markdown link reports the line
        its containing block starts on, which is the only line the token stream records for
        it; a raw anchor reports its own line.
    """
    return _destinations_in(_PARSER.parse(markdown))


class _AnchorHrefs(HTMLParser):
    """Collect the destination each raw anchor start tag carries.

    A parser rather than a pattern, because the question is which anchors the *document*
    actually has. Anchor text inside a comment or a raw-text element such as ``<script>`` is
    not one, and a pattern cannot tell the difference: it would fail a maintained document for
    carrying a commented-out example of the syntax. ``HTMLParser`` routes a comment to
    ``handle_comment`` and raw-text content to ``handle_data``, neither of which is
    implemented here, so both are ignored by construction rather than by exclusion.

    Parsing also yields the ``href`` itself, so the destination resolves through the same
    pipeline a Markdown link does rather than being reported as unresolvable.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record the destination of one anchor carrying a non-empty ``href``.

        The first ``href`` wins, which is what a browser does with a repeated attribute. A
        valueless or empty one names no destination and is skipped, matching how an empty
        Markdown destination is skipped.
        """
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append((self.getpos()[0], value))
                return


def _anchor_hrefs(html: str) -> list[tuple[int, str]]:
    """Return each raw anchor destination with the 1-based line it sits on inside ``html``."""
    parser = _AnchorHrefs()
    parser.feed(html)
    parser.close()
    return parser.hrefs


def maintained_documents(repo_root: Path) -> list[Path]:
    """Return the candidate link sources: the sorted root Markdown files.

    Selection is by name and by being a file, which is the mechanical stand-in for the CLAUDE.md
    ownership list. Whether a candidate stays inside the repository is judged where the documents
    are read, so one that leaves through a symlink is reported rather than dropped from the source
    set -- a silent drop would take that document's links out of the gate and leave it green.

    Args:
        repo_root: The repository root.

    Returns:
        Every root ``*.md`` file in sorted order. Nested directories are not sources.
    """
    return sorted(path for path in repo_root.glob(f"*{_MARKDOWN_SUFFIX}") if path.is_file())


def _is_single_name(segment: str) -> bool:
    """Report whether a decoded segment is one plain filename and not path structure.

    Judged with ``PureWindowsPath`` on every platform, deliberately. Windows reads a backslash
    as a separator and ``C:`` as a drive where POSIX reads both as ordinary characters, so
    judging by the running platform would let one shared gate accept a destination on CI and
    reject it on a contributor's machine. Taking the stricter grammar everywhere costs only
    filenames nobody writes and makes the verdict the same wherever it runs.

    A NUL is refused here rather than left to the filesystem. No path API accepts one: the
    resolve that follows raises ``ValueError`` instead of answering, which would end the run
    on a traceback and leave every later link in every later document unchecked.

    Args:
        segment: One decoded path segment.

    Returns:
        True when the segment names a single file or directory, with no separator of either
        flavour, no drive, no root, and no NUL.
    """
    if "\0" in segment:
        return False
    windows = PureWindowsPath(segment)
    return not windows.drive and not windows.root and windows.parts == (segment,)


def _contained_parts(base: PurePosixPath, raw_path: str) -> tuple[str, ...] | None:
    """Join a relative destination onto a base directory without leaving the repository.

    Structure is settled on the *encoded* path and each surviving segment is decoded after,
    which is the order a browser uses and the one containment depends on. Decoding first would
    let ``%2Fetc%2Fpasswd`` become an absolute path, and joining an absolute component discards
    the repository root outright.

    Dot segments are resolved before decoding, in their encoded spellings too, because that is
    what the WHATWG path state does: ``docs/%2e%2e/GUIDE.md`` is a working link and normalizes
    to ``GUIDE.md`` rather than naming a directory called ``..``. Containment is unaffected --
    an encoded double-dot pops exactly like a bare one, and popping past the root refuses.

    This pass is lexical so that a destination which escapes on paper is refused before the
    filesystem is touched at all. It is not the whole containment story: a contained path can
    still be a symlink out of the repository, which ``_escapes_by_symlink`` covers.

    Args:
        base: The source document's directory, relative to the repository root.
        raw_path: The still-encoded path component of the destination.

    Returns:
        The target's parts relative to the repository root, or None when the destination does
        not name a repository-contained path -- either because it climbs above the root, or
        because a segment decodes into path structure of its own.
    """
    parts = list(base.parts)
    for raw_segment in raw_path.split("/"):
        folded = raw_segment.lower()
        if raw_segment == "" or folded in _SINGLE_DOT_SEGMENTS:
            continue
        if folded in _DOUBLE_DOT_SEGMENTS:
            if not parts:
                return None
            parts.pop()
            continue
        segment = unquote(raw_segment)
        if not _is_single_name(segment) or segment in (".", ".."):
            return None
        parts.append(segment)
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
    """Return a diagnostic when a fragment matches no heading in a Markdown target.

    Both interpolations are neutralized: the target is a repo-controlled filename, and the
    fragment carries whatever the destination percent-encoded, so ``#%1b`` would otherwise put
    a live ESC on stderr. See AD-34.
    """
    if fragment in _heading_ids(target, cache):
        return None
    displayed_target = format_path_for_display(target.relative_to(repo_root).as_posix())
    displayed_fragment = format_path_for_display("#" + fragment)
    return f"fragment {displayed_fragment} matches no heading in {displayed_target}"


def _is_out_of_scope(parts: SplitResult) -> bool:
    """Report whether a destination is external or absolute rather than repository-relative.

    Args:
        parts: The split destination.

    Returns:
        True for any scheme, for the protocol-relative form, and for a root-absolute path.
    """
    return bool(parts.scheme) or bool(parts.netloc) or parts.path.startswith("/")


def _resolve_target(raw_path: str, document: Path, repo_root: Path) -> Path | None:
    """Return the repository path a relative destination names, or None when it does not."""
    source_dir = PurePosixPath(document.parent.relative_to(repo_root).as_posix())
    parts = _contained_parts(source_dir, raw_path)
    return None if parts is None else repo_root.joinpath(*parts)


def _escapes_by_symlink(path: Path, repo_root: Path) -> bool:
    """Report whether a repository-shaped path leaves the repository once resolved.

    Both ends of a link are judged here. For a target, the lexical pass settles the destination
    as written and this settles where the filesystem actually sends it; both are needed, since a
    symlink is invisible to the first and the second alone would let ``..`` be walked out and
    back before anyone looked. For a source, the root ``*.md`` glob is the only structural
    filter there is, so this is the whole containment story.

    An in-repository symlink stays legitimate, because only its resolved location is judged.
    One that leaves is refused rather than followed, and it is judged before the file is opened,
    so no outside file is read -- neither to answer a fragment nor to harvest link destinations
    the diagnostics would then quote back.

    Args:
        path: The candidate source or lexically contained target path.
        repo_root: The repository root, already resolved by the caller so that a checkout
            reached through a symlinked parent does not read as escaping.

    Returns:
        True when the resolved path lies outside the repository.
    """
    return not path.resolve().is_relative_to(repo_root)


def _target_message(href: str, target: Path, repo_root: Path) -> str | None:
    """Return a diagnostic when a contained path cannot serve as a link target.

    Args:
        href: The destination as written, for the diagnostic.
        target: The lexically contained candidate path.
        repo_root: The resolved repository root.

    Returns:
        The diagnostic text, or None when the target is usable.
    """
    displayed = format_path_for_display(href)
    if _escapes_by_symlink(target, repo_root):
        return f"link target {displayed} leaves the repository through a symlink"
    if not target.exists():
        return f"link target {displayed} does not exist"
    return None


def _selects_plain_view(query: str) -> bool:
    """Report whether a query selects GitHub's plain-source view.

    That view lists source lines, so its fragments are line references such as ``#L5`` and no
    heading id could match one. Every other query still renders the document, where a fragment
    is an ordinary heading id and must be validated.

    Args:
        query: The destination's query component.

    Returns:
        True only for GitHub's ``plain=1``.
    """
    return "1" in parse_qs(query).get("plain", [])


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
    target is resolved. Only the plain-source view suppresses heading validation, and it does
    so for a query-only destination too, which resolves against the current document.

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
    # The plain-source view drops the fragment from validation but not the target from
    # existence checking: the file still has to be there for the view to render it.
    fragment = "" if _selects_plain_view(parts.query) else unquote(parts.fragment)
    if not parts.path:
        return _fragment_message(fragment, document, repo_root, cache) if fragment else None
    target = _resolve_target(parts.path, document, repo_root)
    if target is None:
        return f"link target {format_path_for_display(href)} does not resolve inside the repository"
    unusable = _target_message(href, target, repo_root)
    if unusable is not None:
        return unusable
    renders_as_markdown = target.suffix.lower() in _MARKDOWN_TARGET_SUFFIXES
    if not fragment or not renders_as_markdown or not target.is_file():
        return None
    return _fragment_message(fragment, target, repo_root, cache)


def check_repository_links(repo_root: Path) -> list[str]:
    """Return one message per unresolvable link in the maintained documents.

    Args:
        repo_root: The repository root, which is both the link-source directory and the
            containment boundary every relative target must stay inside.

    Returns:
        Messages in document order, sources sorted by name. Each names the source document,
        the line the destination sits on, and the destination as written. A source that leaves
        the repository carries no line, since it is refused before it is read. An empty list
        means every relative destination and heading fragment resolves.
    """
    root = repo_root.resolve()
    cache: dict[Path, frozenset[str]] = {}
    messages: list[str] = []
    for document in maintained_documents(root):
        source = format_path_for_display(document.relative_to(root).as_posix())
        if _escapes_by_symlink(document, root):
            # Refused before the read, so an outside file is neither decoded nor quoted back
            # through a diagnostic. Reported rather than skipped, because a root Markdown file
            # nobody checks is the silent green this gate exists to prevent.
            messages.append(f"{source}: {_ESCAPING_SOURCE_MESSAGE}")
            continue
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
