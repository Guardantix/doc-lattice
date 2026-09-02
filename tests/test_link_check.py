"""Tests for the Markdown link gate engine."""

import os
from pathlib import Path

import pytest

from doc_lattice import link_check as link_check_module
from doc_lattice.error_types import ConfigError, UnreadableDocError
from doc_lattice.link_check import (
    _PARSER,
    LinkFinding,
    _anchor_hrefs,
    _links_in,
    _split_destination,
    check_links,
    select_link_sources,
)
from doc_lattice.path_utils import format_path_for_display


# The old script's whole selection, spelled for the engine: the sorted root Markdown files.
# Kept as the fixture for the moved cases so every one of them still reads as it did.
def _root_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.resolve().glob("*.md") if path.is_file())


def _line(finding: LinkFinding) -> str:
    displayed = format_path_for_display(finding.path)
    if finding.line is None:
        return f"{displayed}: {finding.message}"
    return f"{displayed}:{finding.line}: {finding.message}"


def check_repository_links(root: Path) -> list[str]:
    """The old string form of every finding, so the moved cases assert what they always did."""
    return [_line(finding) for finding in check_links(root, _root_sources(root))]


_requires_permission_enforcement = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX filesystem that enforces modes"
)


def _links(markdown: str) -> list:
    """Return the links of a Markdown string, for tests that drive the scan from text."""
    return _links_in(_PARSER.parse(markdown))


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _only_message(root: Path) -> str:
    """Return the single message the check reports, asserting there is exactly one.

    The "exactly one" half is the load-bearing claim in most of these cases -- a bad
    destination must not also cascade a fragment message -- so it is stated once here rather
    than restated in every test.
    """
    messages = check_repository_links(root)

    assert len(messages) == 1, messages
    return messages[0]


def test_valid_relative_link_passes(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\nSee [arch](ARCHITECTURE.md).\n")
    _write(tmp_path, "ARCHITECTURE.md", "# Architecture\n")

    assert check_repository_links(tmp_path) == []


def test_missing_file_target_names_the_source_and_the_link(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\nSee [gone](MISSING.md).\n")

    message = _only_message(tmp_path)
    assert "README.md" in message
    assert "MISSING.md" in message


def test_valid_anchor_passes(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[ad](ARCHITECTURE.md#ad-25-scanner).\n")
    _write(tmp_path, "ARCHITECTURE.md", "# Architecture\n\n## AD-25 scanner\n\nbody\n")

    assert check_repository_links(tmp_path) == []


def test_anchor_matching_no_heading_names_the_source_and_the_link(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[ad](ARCHITECTURE.md#ad-99-ghost).\n")
    _write(tmp_path, "ARCHITECTURE.md", "# Architecture\n\n## AD-25 scanner\n\nbody\n")

    message = _only_message(tmp_path)
    assert "README.md" in message
    assert "ad-99-ghost" in message
    assert "ARCHITECTURE.md" in message


def test_duplicate_headings_resolve_with_a_document_order_suffix(tmp_path):
    # CHANGELOG.md carries repeated '### Fixed' headings. A base-slug implementation resolves
    # every one of them to '#fixed', so it accepts the first link and rejects the second.
    _write(
        tmp_path,
        "CHANGELOG.md",
        "# Changelog\n\n## [2.0.0]\n\n### Fixed\n\na\n\n## [1.0.0]\n\n### Fixed\n\nb\n",
    )
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n[first](CHANGELOG.md#fixed) and [second](CHANGELOG.md#fixed-1).\n",
    )

    assert check_repository_links(tmp_path) == []


def test_third_duplicate_heading_beyond_the_document_is_still_rejected(tmp_path):
    _write(tmp_path, "CHANGELOG.md", "# Changelog\n\n### Fixed\n\na\n\n### Fixed\n\nb\n")
    _write(tmp_path, "README.md", "# Readme\n\n[third](CHANGELOG.md#fixed-2).\n")

    message = _only_message(tmp_path)
    assert "fixed-2" in message


def test_setext_heading_resolves_as_a_fragment_target(tmp_path):
    # GitHub gives a setext heading an id like any other, so a link to one renders and works.
    # Resolving fragments against the section-identity grammar failed it on a correct link.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#overview)\n")
    _write(tmp_path, "GUIDE.md", "Overview\n========\n\nbody\n")

    assert check_repository_links(tmp_path) == []


def test_indented_atx_heading_resolves_as_a_fragment_target(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#indented)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n   ## Indented\n\nbody\n")

    assert check_repository_links(tmp_path) == []


def test_heading_inside_a_list_item_resolves_as_a_fragment_target(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#nested)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n- item\n\n  ## Nested\n\nbody\n")

    assert check_repository_links(tmp_path) == []


def test_heading_inside_a_block_quote_resolves_as_a_fragment_target(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#quoted)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n> ## Quoted\n>\n> body\n")

    assert check_repository_links(tmp_path) == []


def test_mixed_form_duplicates_are_suffixed_in_document_order(tmp_path):
    # The widened inventory changes which heading owns the base slug: the setext 'Overview'
    # comes first, so the ATX one it used to be the only candidate for moves to 'overview-1'.
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n[a](GUIDE.md#overview) [b](GUIDE.md#overview-1)\n",
    )
    _write(tmp_path, "GUIDE.md", "Overview\n========\n\ntext\n\n# Overview\n\nbody\n")

    assert check_repository_links(tmp_path) == []


def test_mixed_form_duplicate_beyond_the_document_is_still_rejected(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[c](GUIDE.md#overview-2)\n")
    _write(tmp_path, "GUIDE.md", "Overview\n========\n\ntext\n\n# Overview\n\nbody\n")

    message = _only_message(tmp_path)
    assert "overview-2" in message


def test_four_space_indented_heading_is_a_code_block_and_not_a_target(tmp_path):
    # Four spaces makes it an indented code block, which GitHub renders as text rather than a
    # heading. Widening to one-to-three spaces must not walk past that boundary.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#code)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n    # Code\n\nbody\n")

    message = _only_message(tmp_path)
    assert "code" in message


def test_heading_inside_a_fence_is_not_a_fragment_target(tmp_path):
    # A fence suppresses headings on GitHub too, so a fenced '# Fenced' is sample text with no
    # id. The inventory reads parsed heading tokens rather than scanning lines, so it agrees.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#fenced)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n```markdown\n# Fenced\n```\n")

    message = _only_message(tmp_path)
    assert "fenced" in message


def test_missing_target_does_not_also_report_a_missing_anchor(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[gone](MISSING.md#whatever).\n")

    message = _only_message(tmp_path)
    assert "MISSING.md" in message


def test_reference_style_link_is_checked(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\nSee [arch][ref].\n\n[ref]: MISSING.md\n")

    message = _only_message(tmp_path)
    assert "MISSING.md" in message


def test_link_like_text_inside_code_is_not_a_link(tmp_path):
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\nWrite `[label](MISSING.md)` inline.\n\n"
        "```markdown\n[fenced](ALSO-MISSING.md)\n```\n",
    )

    assert check_repository_links(tmp_path) == []


def test_fragment_only_link_resolves_against_the_source_document(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[here](#install) and [gone](#nope).\n\n## Install\n")

    message = _only_message(tmp_path)
    assert "nope" in message
    assert "README.md" in message


def test_explicit_marker_heading_resolves_under_its_github_id(tmp_path):
    # GitHub has no {#id} syntax, so '## Notes {#n}' is addressable as '#notes-n', not '#n'.
    _write(tmp_path, "README.md", "# Readme\n\n[a](GUIDE.md#notes-n) [b](GUIDE.md#n).\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Notes {#n}\n\nbody\n")

    message = _only_message(tmp_path)
    assert "'#n'" in message


def test_external_and_root_absolute_links_are_ignored(tmp_path):
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n[a](https://example.invalid/x) [b](mailto:x@example.invalid)\n"
        "[c](/absolute/path.md) [d](//example.invalid/y)\n",
    )

    assert check_repository_links(tmp_path) == []


def test_target_escaping_the_repository_is_reported(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[out](../outside.md)\n")

    message = _only_message(tmp_path)
    assert "../outside.md" in message


def test_fragment_on_a_non_markdown_target_is_existence_checked_only(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[cfg](config.toml#no-such-heading)\n")
    _write(tmp_path, "config.toml", "[tool]\n")

    assert check_repository_links(tmp_path) == []


def test_percent_encoded_target_is_resolved(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[space](my%20notes.md)\n")
    _write(tmp_path, "my notes.md", "# Notes\n")

    assert check_repository_links(tmp_path) == []


def test_query_string_is_not_part_of_the_target_path(tmp_path):
    # GitHub resolves a relative destination against the blob URL, so `?plain=1` is a view
    # parameter rather than part of the filename.
    _write(tmp_path, "README.md", "# Readme\n\n[plain](GUIDE.md?plain=1)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    assert check_repository_links(tmp_path) == []


def test_missing_target_is_still_reported_when_a_query_is_present(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[plain](MISSING.md?plain=1)\n")

    message = _only_message(tmp_path)
    assert "MISSING.md?plain=1" in message


def test_fragment_is_not_heading_checked_when_the_destination_carries_a_query(tmp_path):
    # `?plain=1` renders source rather than headings, so the fragment is a line ref such as
    # `#L5`, not a heading id. Validating it against headings would reject a valid link.
    _write(tmp_path, "README.md", "# Readme\n\n[line](GUIDE.md?plain=1#L5)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Section\n")

    assert check_repository_links(tmp_path) == []


def test_percent_encoded_separator_is_not_a_path_separator(tmp_path):
    # RFC 3986: %2F is a literal character inside one segment, not a separator. Decoding the
    # whole path before splitting it would manufacture structure and hand joinpath an absolute
    # component, which discards the repository root entirely.
    _write(tmp_path, "README.md", "# Readme\n\n[x](%2Fetc%2Fpasswd)\n")

    message = _only_message(tmp_path)
    assert "%2Fetc%2Fpasswd" in message


def test_percent_encoded_dot_segment_does_not_climb_out(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](%2E%2E/outside.md)\n")

    message = _only_message(tmp_path)
    assert "outside.md" in message


def test_encoded_single_dot_segment_normalizes(tmp_path):
    # WHATWG URL, path state: '%2e' is a single-dot path segment, so a browser drops it
    # rather than looking for a file named '.'.
    _write(tmp_path, "README.md", "# Readme\n\n[x](%2E/GUIDE.md)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    assert check_repository_links(tmp_path) == []


def test_encoded_double_dot_segment_normalizes_within_the_repository(tmp_path):
    # '%2e%2e' is a double-dot path segment, so this resolves back to the root and is a
    # working link rather than one that climbs out.
    _write(tmp_path, "README.md", "# Readme\n\n[x](docs/%2E%2E/GUIDE.md)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    assert check_repository_links(tmp_path) == []


def test_mixed_encoding_dot_segment_normalizes(tmp_path):
    # '.%2e' is the third spelling WHATWG lists, and it is matched case-insensitively.
    _write(tmp_path, "README.md", "# Readme\n\n[x](docs/.%2E/GUIDE.md)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    assert check_repository_links(tmp_path) == []


def test_encoded_backslash_does_not_create_path_structure(tmp_path):
    # A decoded backslash is a separator on Windows and an ordinary character on POSIX. The
    # segment check is deterministic across platforms so one shared gate cannot pass here and
    # fail on a contributor's machine.
    _write(tmp_path, "README.md", "# Readme\n\n[x](%2E%2E%5Coutside.md)\n")

    message = _only_message(tmp_path)
    assert "does not resolve inside the repository" in message


def test_encoded_drive_letter_does_not_create_path_structure(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](%43%3A%5CWindows%5Csystem.ini)\n")

    message = _only_message(tmp_path)
    assert "does not resolve inside the repository" in message


def test_same_document_plain_view_link_is_not_heading_checked(tmp_path):
    # A query-only destination resolves against the current document, so this is the
    # same-document form of the plain-source view.
    _write(tmp_path, "README.md", "# Readme\n\n[line](?plain=1#L5)\n")

    assert check_repository_links(tmp_path) == []


def test_ordinary_query_still_validates_the_fragment(tmp_path):
    # Only the plain-source view replaces heading ids with line anchors. Any other query still
    # renders the document, so exempting every query would let a renamed heading pass silently.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md?utm_source=docs#no-such-heading)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Section\n")

    message = _only_message(tmp_path)
    assert "no-such-heading" in message


def test_ordinary_query_accepts_a_resolving_fragment(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md?utm_source=docs#section)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Section\n")

    assert check_repository_links(tmp_path) == []


def test_symlink_leaving_the_repository_is_reported(tmp_path):
    # The lexical join keeps a symlink's own path, so without a resolved check the later
    # exists/is_file/read_text calls would follow it out of the repository.
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\n## Secret Heading\n", encoding="utf-8")
    # Staged rather than root, so this stays a statement about targets: a root symlink is also
    # a maintained source and would report its own escape as well.
    (repo / "docs").mkdir()
    (repo / "docs" / "OUT.md").symlink_to(outside)
    _write(repo, "README.md", "# Readme\n\n[x](docs/OUT.md#secret-heading)\n")

    message = _only_message(repo)
    assert "docs/OUT.md" in message


def test_symlink_staying_inside_the_repository_resolves(tmp_path):
    # An in-repository symlink is legitimate and GitHub renders it, so containment must reject
    # only the ones that leave.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "REAL.md", "# Real\n\n## Inside Heading\n")
    (repo / "ALIAS.md").symlink_to(repo / "REAL.md")
    _write(repo, "README.md", "# Readme\n\n[x](ALIAS.md#inside-heading)\n")

    assert check_repository_links(repo) == []


def test_source_leaving_the_repository_through_a_symlink_is_reported(tmp_path):
    # The source glob follows symlinks, so without a containment check the hook would read a
    # file outside the checkout and quote its link destinations back through a diagnostic.
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\n[secret](CONFIDENTIAL.md)\n", encoding="utf-8")
    (repo / "EVIL.md").symlink_to(outside)
    _write(repo, "README.md", "# Readme\n\n[gone](MISSING.md)\n")

    messages = check_repository_links(repo)

    assert len(messages) == 2
    assert messages[0] == "'EVIL.md': link source leaves the project root through a symlink"
    assert not any("CONFIDENTIAL.md" in message for message in messages)
    # The run continues, so a later document is still checked.
    assert messages[1].startswith("'README.md':3:")


def test_source_symlinked_to_an_undecodable_file_is_reported_rather_than_raised(tmp_path):
    # Reading first would end the run on a UnicodeDecodeError and leave every later document
    # unchecked, the same silent truncation a NUL-bearing target once caused.
    repo = tmp_path / "repo"
    repo.mkdir()
    blob = tmp_path / "blob.md"
    blob.write_bytes(b"\xff\xfe\x00binary")
    (repo / "BIN.md").symlink_to(blob)

    message = _only_message(repo)
    assert "leaves the project root through a symlink" in message


def test_source_symlinked_inside_the_repository_is_still_checked(tmp_path):
    # Containment must refuse only the sources that leave: an in-repository alias renders on
    # GitHub and its links are the link source's links.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "docs/staged.md", "# Staged\n\n[gone](MISSING.md)\n")
    (repo / "ALIAS.md").symlink_to(repo / "docs" / "staged.md")

    message = _only_message(repo)
    assert message.startswith("'ALIAS.md':3:")
    assert "MISSING.md" in message


def test_raw_html_anchor_is_reported_as_unchecked(tmp_path):
    # Markdown-it emits html_inline for a raw anchor, so its href never reaches link
    # extraction. Reporting it keeps the gap loud instead of silently green.
    _write(tmp_path, "README.md", '# Readme\n\nSee <a href="MISSING.md">guide</a>.\n')

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':3:")
    assert "HTML anchor" in message


def test_raw_html_anchor_is_reported_even_when_its_target_exists(tmp_path):
    # The destination is not resolved either way, so an anchor naming a real file is still
    # reported. Reporting is about the form, not about whether this one would have worked.
    _write(tmp_path, "README.md", '# Readme\n\n<a href="GUIDE.md">g</a>\n')
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    assert "HTML anchor" in _only_message(tmp_path)


def test_raw_html_anchor_in_a_block_reports_its_own_line(tmp_path):
    # The anchor sits on line 5, three lines into the html_block that opens on line 3.
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\n<details>\n<summary>x</summary>\n<a href="MISSING.md">g</a>\n</details>\n',
    )

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':5:")
    assert "HTML anchor" in message


def test_inline_anchor_reports_its_containing_block(tmp_path):
    # Markdown-it records no source position for an inline child, so the anchor on line 4
    # reports the paragraph's line 3. Locating it by searching the paragraph's source was
    # tried and withdrawn: see the cases below that such a search gets wrong.
    _write(tmp_path, "README.md", '# Readme\n\npara\n<a href="MISSING.md">x</a>\n')

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':3:")
    assert "HTML anchor" in message


def test_decoded_text_does_not_displace_a_later_raw_anchor(tmp_path):
    # The text child decodes to exactly the raw tag on the next line while being absent from
    # its own source position. A cursor advanced by non-verbatim content would consume the
    # real tag and report it a line early; the block line is unaffected by either.
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\n&lt;a href="MISSING.md"&gt;\n<a href="MISSING.md">y</a>\n',
    )

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':3:")


def test_reference_link_title_does_not_displace_a_raw_anchor(tmp_path):
    # A reference link's title lives in the definition, not in the paragraph, so searching
    # the paragraph for it found the real tag instead and stepped over it.
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\n[x][ref]\n<a href=MISSING.md>y</a>\n\n[ref]: GUIDE.md "<a href=MISSING.md>"\n',
    )
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':3:")


def test_anchor_inside_an_inline_script_is_not_reported(tmp_path):
    # Markdown-it splits the opening tag, the script's content and the closing tag into
    # separate html_inline tokens. A fresh parser per token leaves the CDATA mode the opening
    # tag entered, and the string literal reads as a live anchor.
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\nbefore <script>const x = '<a href=\"MISSING.md\">';</script> after\n",
    )

    assert check_repository_links(tmp_path) == []


def test_markdown_link_still_reports_its_containing_block(tmp_path):
    # A link_open carries no content to locate, so its documented contract is unchanged: the
    # line the containing block starts on, here line 3 rather than the link's own line 4.
    _write(tmp_path, "README.md", "# Readme\n\npara\n[x](MISSING.md)\n")

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':3:")


# _split_destination is the URL-normalizing boundary. Neither behaviour below is reachable
# through a link source now that a raw anchor's href is reported rather than resolved:
# markdown-it hands over a destination it has already trimmed and percent-encoded. They are
# kept and tested directly, because a boundary that claims to split a URL the way a parser
# does should do so whatever reaches it.
@pytest.mark.parametrize(
    ("href", "expected_path", "expected_fragment"),
    [
        # urlsplit lstrips only, and says so: applications rely on a kept trailing space.
        ("GUIDE.md ", "GUIDE.md", ""),
        ("  GUIDE.md  ", "GUIDE.md", ""),
        # Stripping the whole destination rather than its path is what reaches a fragment.
        ("  GUIDE.md#guide  ", "GUIDE.md", "guide"),
        # Interior tab, newline and CR are removed by urlsplit already.
        ("GUI\tDE.md", "GUIDE.md", ""),
        ("GUIDE.md\n", "GUIDE.md", ""),
        # %20 is not whitespace, so a file that really ends in a space stays addressable.
        ("GUIDE%20.md", "GUIDE%20.md", ""),
    ],
)
def test_split_destination_applies_the_url_whitespace_rules(href, expected_path, expected_fragment):
    parts = _split_destination(href)

    assert parts is not None
    assert parts.path == expected_path
    assert parts.fragment == expected_fragment


@pytest.mark.parametrize("href", ["http://[", "https://[oops]/x", "//[", "//[oops]/x"])
def test_split_destination_refuses_a_malformed_authority(href):
    # urlsplit raises rather than answering. Every spelling that can raise has an authority
    # and is external either way, so None reaches the verdict _is_out_of_scope would.
    assert _split_destination(href) is None


def test_an_encoded_space_still_names_a_file_that_ends_in_one(tmp_path):
    # %20 is not whitespace here. Stripping must not reach it, or a file deliberately named
    # with a trailing space would become unaddressable.
    _write(tmp_path, "README.md", "# Readme\n\n[x](<GUIDE%20.md>)\n")
    _write(tmp_path, "GUIDE .md", "# Guide\n")

    assert check_repository_links(tmp_path) == []


def test_an_unparseable_document_does_not_end_the_run(tmp_path):
    # A character reference wider than the interpreter's integer-conversion limit makes the
    # parsers raise rather than answer. AAA.md sorts first, so ZZZ.md is only reached if the
    # run reports and steps over it.
    reference = "&#" + "9" * 5000 + ";"
    _write(tmp_path, "AAA.md", f'# A\n\n<a href="{reference}">x</a>\n')
    _write(tmp_path, "ZZZ.md", "# Z\n\n[later](ALSO-MISSING.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 2
    assert messages[0] == "'AAA.md': link source could not be parsed for destinations"
    assert messages[1].startswith("'ZZZ.md':3:")


def test_an_undecodable_source_does_not_end_the_run(tmp_path):
    # The read is inside the same refusal: UnicodeDecodeError is a ValueError, so a root
    # document that will not decode is reported rather than ending the run on a traceback.
    (tmp_path / "AAA.md").write_bytes(b"# A\n\xff\xfe\x00binary\n")
    _write(tmp_path, "ZZZ.md", "# Z\n\n[later](ALSO-MISSING.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 2
    assert messages[0] == "'AAA.md': link source could not be parsed for destinations"
    assert messages[1].startswith("'ZZZ.md':3:")


def test_an_unreadable_target_is_reported_rather_than_raised(tmp_path):
    # A target is read for its heading ids outside the source refusal, and the target set is
    # the wider of the two: any repository-contained path, staging included. Without the same
    # guard there, a fragment on an undecodable target ends the run and leaves ZZZ.md
    # unchecked.
    _write(tmp_path, "README.md", "# Readme\n\n[x](docs/BIN.md#nope)\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "BIN.md").write_bytes(b"# T\n\xff\xfe\x00binary\n")
    _write(tmp_path, "ZZZ.md", "# Z\n\n[later](ALSO-MISSING.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 2
    assert (
        messages[0] == "'README.md':3: link target 'docs/BIN.md' could not be read for its headings"
    )
    assert messages[1].startswith("'ZZZ.md':3:")


def test_html_anchor_without_an_href_is_not_reported(tmp_path):
    # A bare <a name="..."> names a destination rather than carrying one, so there is nothing
    # to verify and nothing to complain about.
    _write(tmp_path, "README.md", '# Readme\n\n<a name="top"></a>\n')

    assert check_repository_links(tmp_path) == []


def test_html_anchor_with_an_empty_href_is_not_reported(tmp_path):
    # An empty destination names nothing, exactly as an empty Markdown destination does.
    _write(tmp_path, "README.md", '# Readme\n\n<a href="">x</a>\n')

    assert check_repository_links(tmp_path) == []


def test_an_empty_first_href_suppresses_a_duplicate(tmp_path):
    # A repeated attribute is a parse error and the later one is dropped, so a browser reads
    # this as a same-document link and never sees MISSING.md. Validating the duplicate would
    # fail a mandatory gate on a link that works.
    _write(tmp_path, "README.md", '# Readme\n\n<a href="" href="MISSING.md">x</a>\n')

    assert check_repository_links(tmp_path) == []


def test_a_valueless_first_href_suppresses_a_duplicate(tmp_path):
    _write(tmp_path, "README.md", '# Readme\n\n<a href href="MISSING.md">x</a>\n')

    assert check_repository_links(tmp_path) == []


def test_html_anchor_inside_code_is_not_reported(tmp_path):
    _write(tmp_path, "README.md", '# Readme\n\n```html\n<a href="MISSING.md">g</a>\n```\n')

    assert check_repository_links(tmp_path) == []


def test_control_bytes_in_a_source_filename_are_neutralized(tmp_path):
    # A filename is repo-controlled text that reaches stderr without passing any parser, so
    # ESC in one could forge or erase an earlier pre-commit line. AD-34's boundary.
    _write(tmp_path, "A\x1b[2KB.md", "# Evil\n\n[x](MISSING.md)\n")

    message = _only_message(tmp_path)
    assert "\x1b" not in message
    assert "\\x1b" in message


def test_control_bytes_in_a_fragment_are_neutralized(tmp_path):
    # The fragment is repo-controlled too, and percent-encoding smuggles ESC past the parser.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md#%1b[2K)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    message = _only_message(tmp_path)
    assert "\x1b" not in message


def test_control_bytes_in_a_target_filename_are_neutralized(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](A%1B%5B2KB.md#nope)\n")
    _write(tmp_path, "A\x1b[2KB.md", "# Target\n")

    message = _only_message(tmp_path)
    assert "\x1b" not in message


def test_percent_encoded_nul_is_reported_rather_than_raised(tmp_path):
    # unquote yields an embedded NUL, which resolve() raises on rather than comparing. The
    # second link proves the run continued instead of dying on a traceback.
    _write(tmp_path, "README.md", "# Readme\n\n[x](bad%00.md)\n\n[y](ALSO-MISSING.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 2
    assert "bad%00.md" in messages[0]
    assert "ALSO-MISSING.md" in messages[1]


def test_anchor_inside_an_html_comment_is_not_reported(tmp_path):
    # A commented-out example is not a destination, and failing a mandatory hook on one would
    # reject a document for documenting itself.
    _write(tmp_path, "README.md", '# Readme\n\n<!-- <a href="MISSING.md">example</a> -->\n')

    assert check_repository_links(tmp_path) == []


def test_anchor_inside_a_script_block_is_not_reported(tmp_path):
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n<script>\nvar s = '<a href=\"MISSING.md\">x</a>';\n</script>\n",
    )

    assert check_repository_links(tmp_path) == []


def test_raw_text_state_does_not_carry_across_a_block_boundary(tmp_path):
    # A known limit, pinned rather than fixed. A blank line ends the type-6 ``html_block`` at
    # ``<script>``, so markdown-it emits the opening tag as a block and the string literal as a
    # paragraph of ``html_inline`` children. Each token gets its own parser, the CDATA mode the
    # opening tag entered is lost, and the literal is read as a live anchor.
    #
    # Carrying one parser across the whole document would close it, and would also make
    # ``getpos()`` cumulative over the fed text, which is what reports an anchor at its own
    # line inside a ``<details>`` block. The trade buys an unreachable case -- GitHub strips
    # ``<script>`` from rendered Markdown, so such a block is already inert in the only place
    # these documents are read -- at the cost of a working one, and the failure is a loud false
    # positive rather than the silent truncation this gate exists to prevent. Should the
    # attribution question ever be settled, this test is the one that says so.
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n<details>\n<script>\n\nvar s = '<a href=\"MISSING.md\">';\n"
        "</script>\n</details>\n",
    )

    assert check_repository_links(tmp_path) == [
        "'README.md':6: raw HTML anchor carries a destination this check cannot resolve; "
        "write it as a Markdown link"
    ]


def test_directory_target_is_accepted(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[vendor](vendor/)\n")
    (tmp_path / "vendor").mkdir()

    assert check_repository_links(tmp_path) == []


def test_messages_are_ordered_by_document_then_by_link(tmp_path):
    _write(tmp_path, "ZULU.md", "# Zulu\n\n[a](MISSING-Z.md)\n")
    _write(tmp_path, "ALPHA.md", "# Alpha\n\n[a](MISSING-A1.md)\n\n[b](MISSING-A2.md)\n")

    messages = check_repository_links(tmp_path)

    # Sources are spelled the AD-34 way, so the prefix carries the display quotes.
    assert len(messages) == 3
    assert messages[0].startswith("'ALPHA.md'")
    assert "MISSING-A1.md" in messages[0]
    assert messages[1].startswith("'ALPHA.md'")
    assert "MISSING-A2.md" in messages[1]
    assert messages[2].startswith("'ZULU.md'")


def test_uppercase_markdown_suffix_is_still_fragment_checked(tmp_path):
    # GitHub renders any case of the suffix as Markdown, so a case-sensitive suffix test would
    # accept a dead anchor on GUIDE.MD -- the silent skip this gate exists to prevent.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.MD#no-such-heading)\n")
    _write(tmp_path, "GUIDE.MD", "# Guide\n\n## Section\n")

    message = _only_message(tmp_path)
    assert "no-such-heading" in message


def test_alternate_markdown_suffix_is_fragment_checked(tmp_path):
    # GitHub renders .markdown with the same heading grammar as .md, so exempting it from
    # fragment validation is the same silent skip a case-sensitive suffix test was.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.markdown#no-such-heading)\n")
    _write(tmp_path, "GUIDE.markdown", "# Guide\n\n## Section\n")

    message = _only_message(tmp_path)
    assert "no-such-heading" in message


def test_alternate_markdown_suffix_accepts_a_resolving_fragment(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.mkdn#section)\n")
    _write(tmp_path, "GUIDE.mkdn", "# Guide\n\n## Section\n")

    assert check_repository_links(tmp_path) == []


def test_fragment_on_a_differently_rendered_suffix_is_not_heading_checked(tmp_path):
    # An .mdx heading can come from a component the pinned CommonMark adapter never sees, so
    # validating it against this grammar would fail a link that works.
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.mdx#generated-heading)\n")
    _write(tmp_path, "GUIDE.mdx", "# Guide\n\n<Toc />\n")

    assert check_repository_links(tmp_path) == []


def test_links_and_raw_anchors_are_ordered_by_line_within_a_document(tmp_path):
    # One scan collects both forms, so they interleave by position rather than being grouped
    # by kind. Collecting anchors in a second pass would print line 7 first.
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\n<a href="MISSING.md">g</a>\n\npara\n\n[x](ALSO-MISSING.md)\n',
    )

    messages = check_repository_links(tmp_path)

    assert len(messages) == 2
    assert messages[0].startswith("'README.md':3:")
    assert "HTML anchor" in messages[0]
    assert messages[1].startswith("'README.md':7:")
    assert "ALSO-MISSING.md" in messages[1]


# The duplicate-attribute rule lives in the tokenizer's attribute-name state: a repeated
# name is a parse error and the *new* attribute is dropped, so the first wins whatever it
# holds. These cases are the anchor-shaped equivalents of the html5lib-tests tokenizer
# expectations (MIT), which pin the rule directly:
#   test1.test  <h a='b' a='d'>            -> {"a": "b"}
#   test3.test  <a a A> / <a a a>          -> {"a": ""}
#   test3.test  <a a=''A> / <a a=''a>      -> {"a": ""}
#   test4.test  <x x=1 x=2 X=3>            -> {"x": "1"}   (names case-fold first)
#   test4.test  </x x x>                   -> end tag, attributes dropped entirely
_DUPLICATE_ATTRIBUTE_CASES = [
    ('<a href="b" href="d">x</a>', ["b"]),
    ("<a href HREF>x</a>", []),
    ("<a href href>x</a>", []),
    ("<a href=''HREF>x</a>", []),
    ("<a href=''href>x</a>", []),
    ("<a href=1 href=2 HREF=3>x</a>", ["1"]),
    # The case that actually discriminates the fold: an implementation matching the literal
    # name would skip HREF and resolve B.md, which no browser would navigate to.
    ('<a HREF="A.md" href="B.md">x</a>', ["A.md"]),
    ('<A HREF="OK.md">x</A>', ["OK.md"]),
    ('</a href="M.md">', []),
]


@pytest.mark.parametrize(("html", "expected"), _DUPLICATE_ATTRIBUTE_CASES)
def test_first_href_attribute_wins(html, expected):
    assert [href for _, href in _anchor_hrefs(html)] == expected


def test_staged_docs_are_not_link_sources_but_may_be_targets(tmp_path):
    _write(tmp_path, "docs/staging.md", "# Staged\n\n[gone](MISSING.md)\n")
    _write(tmp_path, "README.md", "# Readme\n\n[staged](docs/staging.md)\n")

    assert check_repository_links(tmp_path) == []


def test_links_are_reported_at_the_line_of_the_containing_block():
    links = _links("# Title\n\npara\n\nSee [a](one.md)\nand [b](two.md)\n")

    assert [(link.href, link.line) for link in links] == [("one.md", 5), ("two.md", 5)]


def test_a_link_with_an_empty_destination_is_ignored():
    assert _links("[a]()\n") == []


def test_findings_are_typed_records(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[gone](MISSING.md)\n")

    findings = check_links(tmp_path, _root_sources(tmp_path))

    assert findings == [LinkFinding("README.md", 3, "link target 'MISSING.md' does not exist")]


def test_check_links_sorts_a_copy_of_its_sources_without_mutating_them(tmp_path):
    _write(tmp_path, "B.md", "# B\n\n[gone](MISSING.md)\n")
    _write(tmp_path, "A.md", "# A\n\n[gone](MISSING.md)\n")
    sources = [tmp_path / "B.md", tmp_path / "A.md"]

    findings = check_links(tmp_path, sources)

    assert [finding.path for finding in findings] == ["A.md", "B.md"]
    assert sources == [tmp_path / "B.md", tmp_path / "A.md"]


def test_check_links_rechecks_containment_before_reading_a_source(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# Outside\n\n[gone](MISSING.md)\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)

    findings = check_links(tmp_path, [tmp_path / "escape.md"])

    assert findings == [LinkFinding("escape.md", None, link_check_module.ESCAPING_SOURCE_MESSAGE)]


def test_a_source_that_will_not_decode_is_reported_and_the_run_continues(tmp_path):
    (tmp_path / "A.md").write_bytes(b"\xff\xfe# not utf-8\n")
    _write(tmp_path, "B.md", "# B\n\n[gone](MISSING.md)\n")

    findings = check_links(tmp_path, _root_sources(tmp_path))

    assert [(finding.path, finding.line) for finding in findings] == [("A.md", None), ("B.md", 3)]
    assert findings[0].message == link_check_module.UNPARSEABLE_SOURCE_MESSAGE


@_requires_permission_enforcement
def test_an_unreadable_source_is_a_tool_error_not_a_finding(tmp_path):
    source = tmp_path / "README.md"
    _write(tmp_path, "README.md", "# Readme\n")
    source.chmod(0)
    try:
        with pytest.raises(UnreadableDocError) as info:
            check_links(tmp_path, [source])
    finally:
        source.chmod(0o644)
    assert info.value.source == source
    assert "README.md" in str(info.value)


@_requires_permission_enforcement
def test_an_unreadable_target_is_a_tool_error_not_a_finding(tmp_path):
    # Staged rather than root, so this stays a statement about targets: a root GUIDE.md is also
    # a source here, and the source read would refuse it first and never reach the target read.
    _write(tmp_path, "README.md", "# Readme\n\n[t](docs/GUIDE.md#intro)\n")
    target = tmp_path / "docs" / "GUIDE.md"
    _write(tmp_path, "docs/GUIDE.md", "# Intro\n")
    target.chmod(0)
    try:
        with pytest.raises(UnreadableDocError) as info:
            check_links(tmp_path, _root_sources(tmp_path))
    finally:
        target.chmod(0o644)
    assert info.value.source == target


@_requires_permission_enforcement
def test_a_target_that_cannot_be_inspected_is_a_tool_error(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[t](locked/GUIDE.md)\n")
    _write(tmp_path, "locked/GUIDE.md", "# Guide\n")
    locked = tmp_path / "locked"
    locked.chmod(0)
    try:
        with pytest.raises(UnreadableDocError):
            check_links(tmp_path, _root_sources(tmp_path))
    finally:
        locked.chmod(0o755)


def test_a_parser_invariant_failure_propagates(tmp_path, monkeypatch):
    _write(tmp_path, "README.md", "# Readme\n\n[t](GUIDE.md#intro)\n")
    _write(tmp_path, "GUIDE.md", "# Intro\n")

    def broken(_text: str):
        msg = "malformed heading token pair"
        raise RuntimeError(msg)

    monkeypatch.setattr(link_check_module, "full_heading_inventory", broken)
    with pytest.raises(RuntimeError, match="malformed"):
        check_links(tmp_path, _root_sources(tmp_path))


def test_same_line_findings_keep_links_before_raw_anchors(tmp_path):
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\n<a href="X.md">a</a> and [b](MISSING.md) and <a href="Y.md">c</a>\n',
    )

    findings = check_links(tmp_path, _root_sources(tmp_path))

    assert [finding.line for finding in findings] == [3, 3, 3]
    assert "MISSING.md" in findings[0].message
    assert findings[1].message == link_check_module.HTML_ANCHOR_MESSAGE
    assert findings[2].message == link_check_module.HTML_ANCHOR_MESSAGE


def test_the_target_inventory_is_the_engines_full_heading_inventory(tmp_path):
    # Setext, an indented ATX, and a heading inside a list item all resolve, and the
    # document-order dedup suffix is the one GitHub assigns.
    _write(
        tmp_path,
        "README.md",
        "# Readme\n\n[a](GUIDE.md#setext)\n[b](GUIDE.md#indented)\n[c](GUIDE.md#nested)\n"
        "[d](GUIDE.md#twice-1)\n",
    )
    _write(
        tmp_path,
        "GUIDE.md",
        "Setext\n======\n\n   ## Indented\n\n- ## Nested\n\n## Twice\n\n## Twice\n",
    )

    assert check_links(tmp_path, _root_sources(tmp_path)) == []


def _relative(root: Path, sources: list[Path]) -> list[str]:
    return [path.relative_to(root.resolve()).as_posix() for path in sources]


def test_a_recursive_selector_matches_at_every_depth_and_a_root_one_only_at_the_root(tmp_path):
    _write(tmp_path, "README.md", "# R\n")
    _write(tmp_path, "docs/a.md", "# A\n")
    _write(tmp_path, "docs/deep/b.md", "# B\n")
    _write(tmp_path, "docs/notes.txt", "x\n")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["docs/**/*.md"])) == [
        "docs/a.md",
        "docs/deep/b.md",
    ]
    assert _relative(tmp_path, select_link_sources(tmp_path, ["*.md"])) == ["README.md"]


def test_selection_is_sorted_and_lexically_deduplicated_across_selectors(tmp_path):
    _write(tmp_path, "b.md", "# b\n")
    _write(tmp_path, "a.md", "# a\n")
    _write(tmp_path, "docs/c.md", "# c\n")

    selected = select_link_sources(tmp_path, ["docs/**/*.md", "*.md", "b.md", "**/*.md"])

    assert _relative(tmp_path, selected) == ["a.md", "b.md", "docs/c.md"]


def test_every_selector_must_match_at_least_one_path(tmp_path):
    _write(tmp_path, "ARCHITECTURE.md", "# A\n")

    with pytest.raises(ConfigError, match=r"docs/\*\*/\*\.md"):
        select_link_sources(tmp_path, ["ARCHITECTURE.md", "docs/**/*.md"])


def test_a_directory_never_satisfies_a_selector(tmp_path):
    (tmp_path / "docs").mkdir()

    with pytest.raises(ConfigError, match="matches no file"):
        select_link_sources(tmp_path, ["docs"])


def test_a_malformed_selector_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="backslash"):
        select_link_sources(tmp_path, ["docs\\a.md"])


def test_matching_is_case_sensitive(tmp_path):
    _write(tmp_path, "README.MD", "# R\n")

    with pytest.raises(ConfigError):
        select_link_sources(tmp_path, ["*.md"])


def test_hidden_directories_get_no_special_treatment(tmp_path):
    _write(tmp_path, ".hidden/a.md", "# a\n")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["**/*.md"])) == [".hidden/a.md"]


def test_a_trailing_recursive_segment_matches_every_file_beneath(tmp_path):
    _write(tmp_path, "docs/a.md", "# a\n")
    _write(tmp_path, "docs/deep/b.txt", "b\n")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["docs/**"])) == [
        "docs/a.md",
        "docs/deep/b.txt",
    ]


def test_contained_aliases_are_deduplicated_by_resolved_target_keeping_the_first_sorted(tmp_path):
    _write(tmp_path, "real.md", "# r\n")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")

    assert _relative(tmp_path, select_link_sources(tmp_path, ["*.md"])) == ["alias.md"]


def test_an_escaping_symlink_is_selected_and_then_reported_by_the_checker(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# o\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    _write(tmp_path, "ok.md", "# ok\n")

    selected = select_link_sources(tmp_path, ["*.md"])

    assert _relative(tmp_path, selected) == ["escape.md", "ok.md"]
    assert check_links(tmp_path, selected) == [
        LinkFinding("escape.md", None, link_check_module.ESCAPING_SOURCE_MESSAGE)
    ]


def test_a_symlinked_directory_is_never_entered(tmp_path):
    _write(tmp_path, "real/a.md", "# a\n")
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

    assert _relative(tmp_path, select_link_sources(tmp_path, ["**/*.md"])) == ["real/a.md"]
    with pytest.raises(ConfigError, match="matches no file"):
        select_link_sources(tmp_path, ["linked/*.md"])


def test_a_symlink_to_a_directory_matched_as_a_leaf_is_a_tool_error(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "linked.md").symlink_to(tmp_path / "real", target_is_directory=True)

    with pytest.raises(UnreadableDocError, match="not a regular file"):
        select_link_sources(tmp_path, ["*.md"])


def test_a_dangling_symlink_is_a_tool_error(tmp_path):
    (tmp_path / "gone.md").symlink_to(tmp_path / "nowhere.md")

    with pytest.raises(UnreadableDocError, match=r"gone\.md"):
        select_link_sources(tmp_path, ["*.md"])


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are a POSIX shape")
def test_a_special_file_is_a_tool_error_without_being_opened(tmp_path):
    os.mkfifo(tmp_path / "pipe.md")

    with pytest.raises(UnreadableDocError, match="not a regular file"):
        select_link_sources(tmp_path, ["*.md"])


@_requires_permission_enforcement
def test_an_unscannable_directory_is_a_config_error(tmp_path):
    _write(tmp_path, "docs/a.md", "# a\n")
    locked = tmp_path / "docs"
    locked.chmod(0)
    try:
        with pytest.raises(ConfigError, match="could not scan"):
            select_link_sources(tmp_path, ["docs/**/*.md"])
    finally:
        locked.chmod(0o755)
