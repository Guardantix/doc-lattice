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

Destinations are read from Markdown link tokens. A destination written as a raw HTML anchor is
reported rather than resolved. Markdown-it normalizes a Markdown destination -- percent-encoding
separators and brackets, trimming surrounding whitespace -- and an attribute value arrives with
none of that done, so resolving one means owning the URL and HTML attribute semantics that
normalization otherwise supplies. That is a wider contract than this gate takes on; reporting the
anchor keeps the gap loud rather than leaving the gate green on a link form it does not model.
"""

import sys
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
_HTML_ANCHOR_MESSAGE = (
    "raw HTML anchor carries a destination this check cannot resolve; write it as a Markdown link"
)
_ESCAPING_SOURCE_MESSAGE = "maintained document leaves the repository through a symlink"
_UNPARSEABLE_SOURCE_MESSAGE = "maintained document could not be parsed for destinations"
# U+0000 through U+0020: the WHATWG URL Standard's "C0 control or space", which its parser
# strips from both ends of a URL before reading it. ``urlsplit`` strips only the leading half
# and keeps the trailing half on purpose, so the rest is applied here.
_C0_CONTROL_OR_SPACE = "".join(chr(code) for code in range(0x00, 0x20 + 1))


@dataclass(frozen=True, slots=True)
class Link:
    """One Markdown link destination with the 1-based line its block starts on."""

    href: str
    line: int


def _block_line(token: Token) -> int:
    """Return the 1-based line a block token starts on.

    The one place the line-attribution rule lives, because both kinds of finding answer to it.
    Markdown-it records no source position for an inline child, so a child is reported at the
    line its containing block starts on, which is the only line the token stream has for it.

    Locating a child by searching the parent's raw source was tried and withdrawn. It is a
    heuristic, not a position: a child's content is only sometimes a verbatim slice of the
    source -- an entity is decoded, a code span's newline is folded, a reference link's title
    is not in the paragraph at all -- and each of those either mis-locates the child or lets
    it consume a later identical fragment. A documented block line beats a precise-looking
    line that is sometimes wrong.

    Args:
        token: A token from a parsed stream.

    Returns:
        The token's own start line, or 1 for a token the parser gave no source map.
    """
    return token.map[0] + 1 if token.map is not None else 1


def _links_in(tokens: list[Token]) -> list[Link]:
    """Return every Markdown link destination in a parsed document, in document order.

    Destinations come from parsed link tokens rather than a text scan, so a reference-style
    link is followed to its definition and link-like text inside inline or fenced code is not
    a link at all. A raw HTML anchor is not a link token and is not returned here; the module
    docstring records why its destination is reported rather than resolved.

    Args:
        tokens: A parsed token stream.

    Returns:
        One entry per link with a non-empty destination, at its containing block's line.
    """
    links: list[Link] = []
    for token in tokens:
        if token.type != "inline" or token.children is None:
            continue
        line = _block_line(token)
        for child in token.children:
            if child.type != "link_open":
                continue
            href = child.attrGet("href")
            if isinstance(href, str) and href:
                links.append(Link(href=href, line=line))
    return links


def _anchor_lines_in(tokens: list[Token]) -> list[int]:
    """Return the lines carrying a raw HTML anchor with a destination, in document order.

    Markdown-it emits raw HTML as ``html_block`` and ``html_inline`` rather than as link
    tokens, so an anchor's destination never reaches ``_links_in``. It is reported rather than
    resolved, for the reason the module docstring records, and reporting needs the line rather
    than the destination -- the ``href`` is read only to tell an anchor that carries one from
    ``<a name="top">``, which names a destination instead.

    An anchor in an ``html_block`` is located within that block's own HTML, so one several
    lines into a ``<details>`` block reports that line rather than the line the block opens
    on. An inline anchor reports its containing block's line, which is the only line the token
    stream records for it.

    Args:
        tokens: A parsed token stream.

    Returns:
        One 1-based line per raw anchor found.
    """
    lines: list[int] = []
    for token in tokens:
        if token.type == "html_block":
            line = _block_line(token)
            lines.extend(line + offset - 1 for offset, _ in _anchor_hrefs(token.content))
        elif token.type == "inline" and token.children is not None:
            fragments = [c.content for c in token.children if c.type == "html_inline"]
            # Guarded rather than fed unconditionally: an inline token carrying no raw HTML at
            # all is every ordinary paragraph, heading and list item in the document, and each
            # would otherwise build and close an HTML parser that has nothing to read.
            if fragments:
                lines.extend(_block_line(token) for _ in _anchor_hrefs(*fragments))
    return lines


class _AnchorHrefs(HTMLParser):
    """Collect the destination each raw anchor start tag carries.

    A parser rather than a pattern, because the question is which anchors the *document*
    actually has. Anchor text inside a comment or a raw-text element such as ``<script>`` is
    not one, and a pattern cannot tell the difference: it would fail a maintained document for
    carrying a commented-out example of the syntax. ``HTMLParser`` routes a comment to
    ``handle_comment`` and raw-text content to ``handle_data``, neither of which is
    implemented here, so both are ignored by construction rather than by exclusion.

    The ``href`` is collected rather than merely counted so that an anchor carrying a
    destination is told from ``<a name="top">``, which names one. Its value is not resolved:
    the module docstring records why that stays outside this gate's contract.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record the destination of one anchor carrying a non-empty ``href``.

        Only the first ``href`` is considered, because that is what the HTML parse rules do
        with a repeated attribute: the later one is a duplicate-attribute parse error and is
        dropped from the tag. An empty or valueless first one resolves to the current
        document rather than naming a target, so it records nothing -- and, crucially, it
        does not fall through to a duplicate no browser would ever navigate to, which would
        fail this gate on a link that works.
        """
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href":
                if value:
                    self.hrefs.append((self.getpos()[0], value))
                return


def _anchor_hrefs(*fragments: str) -> list[tuple[int, str]]:
    """Return each raw anchor destination with the 1-based line it sits on.

    Every fragment is fed to one parser, in order, because raw-text state has to carry across
    them. Markdown-it splits an inline ``<script>``, its content, and ``</script>`` into
    separate ``html_inline`` tokens, so a fresh parser per fragment would leave the CDATA mode
    the opening tag entered and read anchor-shaped text inside the script as a live anchor --
    failing a mandatory gate on a string literal.

    Args:
        *fragments: Raw HTML in document order, from one block or one inline token.

    Returns:
        One entry per anchor carrying a destination, with its 1-based line within the fed
        text.
    """
    parser = _AnchorHrefs()
    for fragment in fragments:
        parser.feed(fragment)
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


def _heading_ids(document: Path, cache: dict[Path, frozenset[str]]) -> frozenset[str] | None:
    """Return the addressable GitHub heading ids of a Markdown document, memoized.

    A target is read here rather than where the sources are, so it needs the same refusal the
    sources have: a document that will not decode as UTF-8, or that carries a character
    reference wider than the interpreter's integer-conversion limit, raises ``ValueError``
    instead of answering. Left unguarded that ends the run on a traceback and leaves every
    later link in every later document unchecked -- and a target is the wider set of the two,
    since it may be any repository-contained path rather than a root ``*.md`` file.

    A failed read is not memoized, because nothing is learned to memoize; the same target
    reached from a second link is read again and refused again.

    Args:
        document: The Markdown target whose heading ids are wanted.
        cache: Heading ids already read, keyed by target path.

    Returns:
        The target's addressable heading ids, or None when it could not be read for them.
    """
    if document not in cache:
        try:
            headings = extract_headings(document.read_text(encoding="utf-8"))
        except ValueError:
            return None
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
    heading_ids = _heading_ids(target, cache)
    displayed_target = format_path_for_display(target.relative_to(repo_root).as_posix())
    if heading_ids is None:
        return f"link target {displayed_target} could not be read for its headings"
    if fragment in heading_ids:
        return None
    displayed_fragment = format_path_for_display("#" + fragment)
    return f"fragment {displayed_fragment} matches no heading in {displayed_target}"


def _split_destination(href: str) -> SplitResult | None:
    """Split a destination the way a URL parser reads it, or None when it is malformed.

    Surrounding C0-control-or-space is stripped first. A URL parser removes it from both ends
    before reading, and ``urlsplit`` deliberately keeps the trailing half -- its own comment
    says applications rely on that -- so ``href="GUIDE.md "`` would otherwise be looked up as
    a filename that ends in a space and a working link would fail the hook. Only a raw HTML
    anchor can carry one: markdown-it drops trailing space from a Markdown destination even in
    the ``<...>`` form, and a space meant to be kept survives as ``%20``, which is not
    whitespace here and still names a file that really ends in one. Stripping the whole
    destination rather than its path also covers a fragment, which the same rule reaches.
    Interior tab, newline and carriage return are removed by ``urlsplit`` already.

    ``urlsplit`` raises rather than answering for an unparseable authority such as
    ``http://[`` or ``//[oops]/x``. Only a raw HTML anchor reaches here carrying one, because
    markdown-it percent-encodes the bracket in a Markdown destination; an unguarded raise
    would end the run on a traceback and leave every later link in every later document
    unchecked, which is the failure the percent-encoded NUL refusal already closed once.

    Raising requires parsing an authority, so every destination refused here has a scheme or
    is protocol-relative, and is external either way. None therefore means the same verdict
    ``_is_out_of_scope`` would reach: outside this gate's contract, not silently accepted as
    a repository target.

    Args:
        href: The destination as written.

    Returns:
        The split destination, or None when it is not well formed enough to classify.
    """
    try:
        return urlsplit(href.strip(_C0_CONTROL_OR_SPACE))
    except ValueError:
        return None


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
    parts = _split_destination(href)
    if parts is None or _is_out_of_scope(parts):
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
        Messages in document order, sources sorted by name, with unresolvable links and raw
        HTML anchors interleaved by line rather than grouped by kind. Each names the source
        document and the line the finding sits on. A source that leaves the repository, or
        that will not decode or parse, carries no line, since all of those are refused before
        their destinations are read. An empty list means every relative destination and
        heading fragment resolves.
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
        try:
            # One parse per document: both kinds of finding read the same token stream.
            tokens = _PARSER.parse(document.read_text(encoding="utf-8"))
            links, anchors = _links_in(tokens), _anchor_lines_in(tokens)
        except ValueError:
            # Two causes, both spelled ``ValueError``: bytes that will not decode as UTF-8,
            # and a character reference wider than the interpreter's integer-conversion limit,
            # which makes the parsers raise rather than answer. Reported and stepped over, so
            # one document's content cannot end the run and leave every later document
            # unchecked -- the failure the NUL and malformed-authority refusals also close.
            messages.append(f"{source}: {_UNPARSEABLE_SOURCE_MESSAGE}")
            continue
        found: list[tuple[int, str]] = []
        for link in links:
            message = _link_message(link, document, root, cache)
            if message is not None:
                found.append((link.line, message))
        found.extend((line, _HTML_ANCHOR_MESSAGE) for line in anchors)
        # Stable sort, so a link and an anchor reported on one line keep the order they were
        # collected in and the two kinds of finding interleave by line rather than by kind.
        found.sort(key=lambda entry: entry[0])
        messages.extend(f"{source}:{line}: {message}" for line, message in found)
    return messages


def main() -> None:
    """Check every maintained document and exit non-zero on any unresolvable link."""
    messages = check_repository_links(_REPO_ROOT)
    for message in messages:
        print(message, file=sys.stderr)
    sys.exit(1 if messages else 0)


if __name__ == "__main__":
    main()
