"""The Markdown link gate: select configured sources, then verify every relative link and fragment.

The read-only filesystem boundary for the ``links`` command (AD-2). ``select_link_sources``
expands the ``link_sources`` selectors with a no-follow walk of its own, and ``check_links`` reads
the selected documents and every repository-contained target they link to. Both return data rather
than prose: findings are ``LinkFinding`` records and the ``path[:line]: message`` envelope belongs
to the command adapter.

Containment binds both ends: a source that leaves the project root through a symlink is reported
rather than read, and a target is judged the same way before it is opened. Selection expands a
selector lexically and never enters a symlinked directory, whether ``**`` reaches it or a fixed
segment names it, because following one would let a link to ``/`` turn ``**`` into a filesystem
walk. Every selector has to match at least one lexical path, so a mandatory gate can never pass
over zero files.

Absolute and external destinations are out of scope and skipped, as are image destinations.
Heading fragments are validated only against Markdown targets, against
``markdown_compat.full_heading_inventory``: every heading a GitHub render assigns an id to --
setext, ATX indented one to three spaces, and headings nested in a list item or a block quote --
where the addressable subset the lattice sees is column-zero ATX only. A link to one of the wider
forms renders and resolves on GitHub, so failing it here would fail a correct link; widening the
adapter instead would change which sections the engine sees, which is a cached-derivation change
this gate has no business forcing. Reading the engine's inventory rather than a private walk is
what keeps a fragment resolving to the same id the engine would assign wherever both see the same
heading.

Rendered inline heading text is the one form still out of reach: heading ids are slugged from raw
inline source on both sides, so ``## [Guide](target.md)`` yields ``guidetargetmd`` against
GitHub's ``guide``.

Destinations are read from Markdown link tokens. A destination written as a raw HTML anchor is
reported rather than resolved. Markdown-it normalizes a Markdown destination -- percent-encoding
separators and brackets, trimming surrounding whitespace -- and an attribute value arrives with
none of that done, so resolving one means owning the URL and HTML attribute semantics that
normalization otherwise supplies. That is a wider contract than this gate takes on; reporting the
anchor keeps the gap loud rather than leaving the gate green on a link form it does not model.

Failures fall into two classes. Content the gate cannot read as a document -- bytes that will not
decode, a character reference the parser refuses -- is a finding on that document and the run
continues. A filesystem the gate cannot inspect -- a resolve, stat, scan, or open that fails --
is a tool error: a gate that cannot see its inputs must not pass.
"""

import errno
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .error_types import ConfigError, UnreadableDocError
from .link_selectors import (
    RECURSIVE_SEGMENT,
    SELECTOR_SEPARATOR,
    segment_matches,
    validate_link_selector,
)
from .markdown_compat import full_heading_inventory
from .path_utils import format_path_for_display

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
HTML_ANCHOR_MESSAGE = (
    "raw HTML anchor carries a destination this check cannot resolve; write it as a Markdown link"
)
ESCAPING_SOURCE_MESSAGE = "link source leaves the project root through a symlink"
UNPARSEABLE_SOURCE_MESSAGE = "link source could not be parsed for destinations"
# U+0000 through U+0020: the WHATWG URL Standard's "C0 control or space", which its parser
# strips from both ends of a URL before reading it. ``urlsplit`` strips only the leading half
# and keeps the trailing half on purpose, so the rest is applied here.
_C0_CONTROL_OR_SPACE = "".join(chr(code) for code in range(0x00, 0x20 + 1))
# The errnos that answer "not here" rather than "cannot tell", the pair the init adapter's
# ancestor walk reads the same way. Everything else means the filesystem could not answer, which
# is a tool error rather than a finding. That adapter also absorbs ELOOP, which this one does
# not: a symlink loop names a target the gate genuinely cannot inspect, and reporting it as a
# missing file would put a wrong diagnostic on a link that may well be correct.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})


@dataclass(frozen=True, slots=True)
class Link:
    """One Markdown link destination with the 1-based line its block starts on."""

    href: str
    line: int


@dataclass(frozen=True, slots=True)
class LinkFinding:
    """One thing the gate found wrong, located by source document and line.

    ``path`` is the source's project-relative POSIX spelling, raw and unescaped: the human
    renderer applies the display spelling and the GitHub renderer rejoins it to the project root.
    ``line`` is None for a finding about the document itself -- one that escapes the project
    root or will not parse -- since those are refused before any destination is read.
    """

    path: str
    line: int | None
    message: str


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


def _heading_ids(
    document: Path, cache: dict[Path, frozenset[str]], *, text: str | None = None
) -> frozenset[str] | None:
    """Return the link-target GitHub heading ids of a Markdown document, memoized.

    The ids are the engine's own: ``full_heading_inventory`` reads every heading a GitHub render
    assigns an id to and allocates through the pinned document-order collision rule, so this is
    the one inventory rather than a parallel copy of it. The module docstring records why it is
    wider than the addressable subset.

    A target is read here rather than where the sources are, so it needs a refusal of its own,
    and bytes that will not decode as UTF-8 are the whole of it: the inventory parses blocks
    only, so no character reference reaches an entity decoder and the parse itself has no content
    failure to report. An undecodable target is a finding on the link rather than a tool error,
    and it is not memoized, because nothing was learned to memoize.

    That is the narrower half of the source refusal, deliberately: a source is parsed inline as
    well, which is where a character reference wider than the interpreter's integer-conversion
    limit makes the parser raise, and ``check_links`` owns that case for sources.

    ``text`` is the document's already-read content, which the caller has whenever the target is
    the source the link was written in: a ``#fragment`` destination resolves against its own
    document, and re-reading a file ``check_links`` has in hand is a second read of every source
    that carries so much as a table of contents.

    Args:
        document: The Markdown target whose heading ids are wanted.
        cache: Heading ids already read, keyed by target path.
        text: The document's content when the caller already read it, else None to read here.

    Returns:
        The target's link-target heading ids, or None when the file would not decode.

    Raises:
        UnreadableDocError: If the filesystem refused the read.
        RuntimeError: If the pinned parser returned a malformed heading token pair; that is a
            parser invariant failure, not bad content, and is deliberately not caught.
    """
    if document not in cache:
        if text is None:
            try:
                text = document.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return None
            except OSError as exc:
                msg = f"link target {format_path_for_display(document)} could not be read: {exc}"
                raise UnreadableDocError(msg, source=document) from exc
        cache[document] = frozenset(record.github_id for record in full_heading_inventory(text))
    return cache[document]


def _fragment_message(
    fragment: str,
    target: Path,
    root: Path,
    cache: dict[Path, frozenset[str]],
    *,
    text: str | None = None,
) -> str | None:
    """Return a diagnostic when a fragment matches no heading in a Markdown target.

    Both interpolations are neutralized: the target is a repo-controlled filename, and the
    fragment carries whatever the destination percent-encoded, so ``#%1b`` would otherwise put
    a live ESC on stderr. See AD-34.

    ``text`` is passed only for the target that is the source document itself; ``_heading_ids``
    records why.
    """
    heading_ids = _heading_ids(target, cache, text=text)
    displayed_target = format_path_for_display(target.relative_to(root).as_posix())
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


def _resolve_target(raw_path: str, document: Path, root: Path) -> Path | None:
    """Return the repository path a relative destination names, or None when it does not."""
    source_dir = PurePosixPath(document.parent.relative_to(root).as_posix())
    parts = _contained_parts(source_dir, raw_path)
    return None if parts is None else root.joinpath(*parts)


def _resolved(path: Path) -> Path:
    """Resolve a path, turning a filesystem that will not answer into a tool error."""
    try:
        return path.resolve()
    except OSError as exc:
        msg = f"{format_path_for_display(path)} could not be resolved: {exc}"
        raise UnreadableDocError(msg, source=path) from exc


def _escapes_by_symlink(path: Path, root: Path) -> bool:
    """Report whether a project-shaped path leaves the project root once resolved.

    Both ends of a link are judged here. For a target, the lexical pass settles the destination
    as written and this settles where the filesystem actually sends it; both are needed, since a
    symlink is invisible to the first and the second alone would let ``..`` be walked out and
    back before anyone looked. For a source, selection is lexical, so this is the whole
    containment story.

    An in-project symlink stays legitimate, because only its resolved location is judged. One
    that leaves is refused rather than followed, and it is judged before the file is opened, so
    no outside file is read -- neither to answer a fragment nor to harvest link destinations the
    diagnostics would then quote back.

    Args:
        path: The candidate source or lexically contained target path.
        root: The project root, already resolved by the caller so that a checkout reached
            through a symlinked parent does not read as escaping.

    Returns:
        True when the resolved path lies outside the project root.
    """
    return not _resolved(path).is_relative_to(root)


def _stat_mode(path: Path) -> int | None:
    """Return a path's mode following symlinks, None when absent, or a tool error otherwise.

    ``Path.exists`` is not a stable predicate across the supported interpreters: 3.13 re-raises
    an ``OSError`` outside its ignored set while 3.14 answers False for every one, so a target in
    a directory this process cannot search would be "does not exist" on one and a traceback on
    the other. ``stat`` raises on both, and the decision is made here instead.
    """
    try:
        return path.stat().st_mode
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return None
        msg = f"{format_path_for_display(path)} could not be inspected: {exc}"
        raise UnreadableDocError(msg, source=path) from exc


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
    root: Path,
    cache: dict[Path, frozenset[str]],
    source_text: str,
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
        root: The project root every target must stay inside.
        cache: Heading ids already read, keyed by target path.
        source_text: The source document's already-read content, handed on for the one target
            that is the source itself so it is not read a second time.

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
        return (
            _fragment_message(fragment, document, root, cache, text=source_text)
            if fragment
            else None
        )
    target = _resolve_target(parts.path, document, root)
    if target is None:
        return f"link target {format_path_for_display(href)} does not resolve inside the repository"
    displayed = format_path_for_display(href)
    if _escapes_by_symlink(target, root):
        return f"link target {displayed} leaves the repository through a symlink"
    mode = _stat_mode(target)
    if mode is None:
        return f"link target {displayed} does not exist"
    renders_as_markdown = target.suffix.lower() in _MARKDOWN_TARGET_SUFFIXES
    checkable = bool(fragment) and renders_as_markdown and stat.S_ISREG(mode)
    return _fragment_message(fragment, target, root, cache) if checkable else None


def check_links(project_root: Path, sources: Sequence[Path]) -> list[LinkFinding]:
    """Return one finding per unresolvable link, raw anchor, or unreadable source.

    The sources are normally what ``select_link_sources`` returned, but this upholds its own
    contract whatever the caller passed: the list is sorted (a copy, the input is untouched) and
    every source is containment-checked immediately before it is read.

    Args:
        project_root: The project root every source and target must stay inside.
        sources: Unresolved paths under ``project_root``, one per document to check.

    Returns:
        Findings in document order, sources sorted by path. Within a document, link findings
        come first, then raw HTML anchors, and the two are stable-sorted by line, so a link and an
        anchor on one line keep that relative order. A source that leaves the project root, or
        that will not decode or parse, carries no line, since all of those are refused before
        their destinations are read. An empty list means every relative destination and heading
        fragment resolves.

    Raises:
        UnreadableDocError: If the filesystem refused to resolve, inspect, or read a source or a
            link target.
        ValueError: If a source is not lexically under ``project_root``, which is a caller bug
            rather than a document defect.
    """
    root = project_root.resolve()
    cache: dict[Path, frozenset[str]] = {}
    findings: list[LinkFinding] = []
    for document in sorted(sources):
        relative = document.relative_to(root).as_posix()
        if _escapes_by_symlink(document, root):
            # Refused before the read, so an outside file is neither decoded nor quoted back
            # through a diagnostic. Reported rather than skipped, because a configured source
            # nobody checks is the silent green this gate exists to prevent.
            findings.append(LinkFinding(relative, None, ESCAPING_SOURCE_MESSAGE))
            continue
        try:
            text = document.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(LinkFinding(relative, None, UNPARSEABLE_SOURCE_MESSAGE))
            continue
        except OSError as exc:
            msg = f"link source {format_path_for_display(document)} could not be read: {exc}"
            raise UnreadableDocError(msg, source=document) from exc
        try:
            # One parse per document: both kinds of finding read the same token stream. A
            # character reference wider than the interpreter's integer-conversion limit makes
            # the parser raise ValueError rather than answer; reported and stepped over, so one
            # document's content cannot end the run and leave every later document unchecked.
            tokens = _PARSER.parse(text)
            links, anchors = _links_in(tokens), _anchor_lines_in(tokens)
        except ValueError:
            findings.append(LinkFinding(relative, None, UNPARSEABLE_SOURCE_MESSAGE))
            continue
        found: list[tuple[int, str]] = []
        for link in links:
            message = _link_message(link, document, root, cache, text)
            if message is not None:
                found.append((link.line, message))
        found.extend((line, HTML_ANCHOR_MESSAGE) for line in anchors)
        # Stable sort, so a link and an anchor reported on one line keep the order they were
        # collected in and the two kinds of finding interleave by line rather than by kind.
        found.sort(key=lambda entry: entry[0])
        findings.extend(LinkFinding(relative, line, message) for line, message in found)
    return findings


def select_link_sources(project_root: Path, selectors: Sequence[str]) -> list[Path]:
    """Expand the ``link_sources`` selectors into the documents the gate will check.

    Each selector is expanded lexically by a no-follow walk from the project root. The matches
    are unioned, sorted by project-relative POSIX spelling, and then judged in that order:
    a spelling that resolves outside the project root is kept, so ``check_links`` reports every
    bad configured source; a contained spelling has to be, or resolve to, a regular file, and
    aliases of one file are collapsed onto the first spelling in sorted order. YAML order,
    overlapping selectors, and filesystem order therefore cannot change what is returned.

    Args:
        project_root: The project root the selectors are relative to.
        selectors: The ``link_sources`` entries, already validated by config load or not.

    Returns:
        Unresolved paths under the resolved project root, sorted and deduplicated.

    Raises:
        ConfigError: If a selector is malformed, matches no lexical path, or the walk meets a
            directory it cannot scan.
        UnreadableDocError: If a contained match is a dangling symlink, a directory reached
            through a symlink, or a special file. None of those is opened: the classification is
            a ``stat``, because opening a FIFO or a device can block indefinitely.
    """
    root = project_root.resolve()
    matched: set[str] = set()
    for entry in selectors:
        try:
            segments = validate_link_selector(entry)
        except ValueError as exc:
            msg = f"link_sources entry {format_path_for_display(entry)} {exc}"
            raise ConfigError(msg) from exc
        state = _WalkState(found=set(), visited=set())
        _walk(root, "", segments, 0, state)
        if not state.found:
            msg = (
                f"link_sources entry {format_path_for_display(entry)} matches no file under "
                f"the project root {format_path_for_display(root)}; the links command refuses "
                "to run over a selector that selects nothing"
            )
            raise ConfigError(msg)
        matched.update(state.found)
    seen: set[Path] = set()
    sources: list[Path] = []
    for relative in sorted(matched):
        candidate = root.joinpath(*relative.split(SELECTOR_SEPARATOR))
        resolved = _resolved(candidate)
        if not resolved.is_relative_to(root):
            sources.append(candidate)
            continue
        _require_regular_file(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        sources.append(candidate)
    return sources


def _require_regular_file(candidate: Path) -> None:
    """Refuse a contained match that is not, or does not resolve to, a regular file."""
    mode = _stat_mode(candidate)
    if mode is None:
        msg = f"link source {format_path_for_display(candidate)} is a symlink to nothing"
        raise UnreadableDocError(msg, source=candidate)
    if not stat.S_ISREG(mode):
        msg = f"link source {format_path_for_display(candidate)} is not a regular file"
        raise UnreadableDocError(msg, source=candidate)


def _scan(directory: Path) -> list[os.DirEntry[str]]:
    """List one directory, turning a scan the filesystem refuses into a config error."""
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError as exc:
        msg = f"link_sources selection could not scan {format_path_for_display(directory)}: {exc}"
        raise ConfigError(msg) from exc


def _is_directory(entry: os.DirEntry[str]) -> bool:
    """Report whether an entry is a directory in its own right, never through a symlink."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError as exc:
        displayed = format_path_for_display(entry.path)
        msg = f"link_sources selection could not inspect {displayed}: {exc}"
        raise ConfigError(msg) from exc


def _join(prefix: str, name: str) -> str:
    return name if prefix == "" else f"{prefix}{SELECTOR_SEPARATOR}{name}"


@dataclass(slots=True)
class _WalkState:
    """The mutable state one ``select_link_sources`` walk threads through ``_walk``.

    ``found`` collects matched project-relative spellings. ``visited`` memoizes ``(prefix,
    index)`` states already scanned, as the ``_walk`` docstring explains; it is created fresh
    per selector, so one selector's memoization never suppresses a scan another selector needs.
    """

    found: set[str]
    visited: set[tuple[str, int]]


def _walk(  # noqa: PLR0913
    directory: Path,
    prefix: str,
    segments: tuple[str, ...],
    index: int,
    state: _WalkState,
    entries: list[os.DirEntry[str]] | None = None,
) -> None:
    """Match ``segments[index:]`` beneath one directory, collecting project-relative spellings.

    A directory is a traversal node and never a match; only a non-directory entry can satisfy
    the last segment. ``**`` matches zero or more directories, so it is tried against the
    remaining segments here before descending with itself still current.

    ``state.visited`` memoizes ``(prefix, index)`` states for the one selector this walk is
    expanding. Adjacent ``**`` segments are valid grammar, and without memoization a directory
    at depth d reached through k of them is scanned about C(d+k-1, k-1) times: the non-last
    ``**`` branch both descends into each child directory at the same index and hands the same
    directory to ``index + 1``, and those two spreads converge on the same ``(prefix, index)``
    state from multiple call paths lower in the tree. Recording each state before it is scanned
    bounds the whole walk to at most one scan per directory per segment index. What it removes
    is a state being re-entered from several call paths, not the handoff to ``index + 1``
    itself: that lands on a different key, so the bound stays directories times segments rather
    than directories alone. The bound does not change what is found: revisiting a state a second
    time could only add matches the first visit already added.

    ``entries`` is that handoff's own listing, passed rather than re-read. The handoff is the one
    recursion that stays in the same directory, so it is the one that can reuse what this frame
    already scanned, and reusing it is what keeps the common ``docs/**/*.md`` shape at one
    ``scandir`` per directory instead of two. Every other recursion descends and passes None.
    """
    key = (prefix, index)
    if key in state.visited:
        return
    state.visited.add(key)
    segment = segments[index]
    last = index == len(segments) - 1
    if entries is None:
        entries = _scan(directory)
    if segment == RECURSIVE_SEGMENT:
        if not last:
            _walk(directory, prefix, segments, index + 1, state, entries)
        for entry in entries:
            if _is_directory(entry):
                _walk(Path(entry.path), _join(prefix, entry.name), segments, index, state)
            elif last:
                state.found.add(_join(prefix, entry.name))
        return
    for entry in entries:
        if not segment_matches(entry.name, segment):
            continue
        if last:
            if not _is_directory(entry):
                state.found.add(_join(prefix, entry.name))
        elif _is_directory(entry):
            _walk(Path(entry.path), _join(prefix, entry.name), segments, index + 1, state)
