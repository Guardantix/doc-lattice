"""Tests for the relative-link and heading-fragment guard script."""

from pathlib import Path
from runpy import run_path

import pytest
from workflow_helpers import _commands, _invocations, _invokes, _load_workflow

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = run_path(str(_ROOT / "scripts" / "check_doc_links.py"))
check_repository_links = _SCRIPT["check_repository_links"]
maintained_documents = _SCRIPT["maintained_documents"]
extract_links = _SCRIPT["extract_links"]
_anchor_hrefs = _SCRIPT["_anchor_hrefs"]
_split_destination = _SCRIPT["_split_destination"]

_SCRIPT_PATH = "scripts/check_doc_links.py"


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
    assert messages[0] == "'EVIL.md': maintained document leaves the repository through a symlink"
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
    assert "leaves the repository through a symlink" in message


def test_source_symlinked_inside_the_repository_is_still_checked(tmp_path):
    # Containment must refuse only the sources that leave: an in-repository alias renders on
    # GitHub and its links are the maintained document's links.
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


def test_inline_anchor_after_a_soft_break_reports_its_own_line(tmp_path):
    # Markdown-it gives an inline child no source map, so the anchor would otherwise inherit
    # the paragraph's opening line. It sits on line 4, one soft break in.
    _write(tmp_path, "README.md", '# Readme\n\npara\n<a href="MISSING.md">x</a>\n')

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':4:")


def test_inline_anchor_after_a_code_span_crossing_a_break_reports_its_own_line(tmp_path):
    # The code span folds its newline to a space and emits no softbreak, so counting break
    # children would place the anchor a line early. It sits on line 5.
    _write(
        tmp_path,
        "README.md",
        '# Readme\n\nfirst `code\nspan` then\n<a href="MISSING.md">x</a>\n',
    )

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':5:")


def test_markdown_link_still_reports_its_containing_block(tmp_path):
    # A link_open carries no content to locate, so its documented contract is unchanged: the
    # line the containing block starts on, here line 3 rather than the link's own line 4.
    _write(tmp_path, "README.md", "# Readme\n\npara\n[x](MISSING.md)\n")

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':3:")


# _split_destination is the URL-normalizing boundary. Neither behaviour below is reachable
# through a maintained document now that a raw anchor's href is reported rather than resolved:
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


# The anchor's label is deliberately the same as the link's label. Stepping over the title at
# link_open instead of link_close overshoots: the label child then matches this second `x`
# rather than the one in brackets, pushing the cursor past the raw tag so the search fails and
# the anchor falls back to line 1. A different label would pass either way.
_TITLE_REPEAT = '[x](GUIDE.md "<a href=MISSING.md>")\n<a href=MISSING.md>x</a>\n'


@pytest.mark.parametrize("label", ["x", "y"])
def test_anchor_repeated_inside_a_link_title_reports_its_own_line(tmp_path, label):
    # A link's title is source text with no child of its own. Without stepping over it the
    # search matches the copy inside the title and reports the anchor on line 1.
    _write(tmp_path, "README.md", _TITLE_REPEAT.replace(">x</a>", f">{label}</a>"))
    _write(tmp_path, "GUIDE.md", "# Guide\n")

    message = _only_message(tmp_path)
    assert message.startswith("'README.md':2:")


def test_an_unparseable_document_does_not_end_the_run(tmp_path):
    # A character reference wider than the interpreter's integer-conversion limit makes the
    # parsers raise rather than answer. AAA.md sorts first, so ZZZ.md is only reached if the
    # run reports and steps over it.
    reference = "&#" + "9" * 5000 + ";"
    _write(tmp_path, "AAA.md", f'# A\n\n<a href="{reference}">x</a>\n')
    _write(tmp_path, "ZZZ.md", "# Z\n\n[later](ALSO-MISSING.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 2
    assert messages[0] == "'AAA.md': maintained document could not be parsed for destinations"
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


def test_maintained_documents_are_the_sorted_root_markdown_files(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n")
    _write(tmp_path, "AGENTS.md", "# Agents\n")
    _write(tmp_path, "notes.txt", "text\n")
    _write(tmp_path, "docs/staging.md", "# Staged\n")

    assert [path.name for path in maintained_documents(tmp_path)] == ["AGENTS.md", "README.md"]


def test_staged_docs_are_not_link_sources_but_may_be_targets(tmp_path):
    _write(tmp_path, "docs/staging.md", "# Staged\n\n[gone](MISSING.md)\n")
    _write(tmp_path, "README.md", "# Readme\n\n[staged](docs/staging.md)\n")

    assert check_repository_links(tmp_path) == []


def test_extract_links_reports_the_line_of_the_containing_block():
    links = extract_links("# Title\n\npara\n\nSee [a](one.md)\nand [b](two.md)\n")

    assert [(link.href, link.line) for link in links] == [("one.md", 5), ("two.md", 5)]


def test_extract_links_ignores_an_empty_destination():
    assert extract_links("[a]()\n") == []


def test_repository_maintained_documents_have_no_broken_links():
    assert check_repository_links(_ROOT) == []


def test_pre_commit_runs_the_link_check():
    config = _load_workflow(_ROOT / ".pre-commit-config.yaml")
    entries = [
        hook["entry"] for repo in config["repos"] for hook in repo["hooks"] if "entry" in hook
    ]

    assert any(_invokes(argv, _SCRIPT_PATH) for entry in entries for argv in _invocations(entry)), (
        f"no pre-commit hook runs {_SCRIPT_PATH}"
    )


def test_ci_code_quality_job_runs_the_link_check():
    # CI enumerates its checks directly and never invokes pre-commit, so the hook alone would
    # leave a renamed heading green on a pull request.
    job = _load_workflow(_ROOT / ".github/workflows/ci.yml")["jobs"]["code-quality"]

    assert any(
        _invokes(argv, _SCRIPT_PATH)
        for step in job["steps"]
        for argv in _invocations(_commands(step))
    ), f"the code-quality job does not run {_SCRIPT_PATH}"
