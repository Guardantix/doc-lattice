"""Tests for the relative-link and heading-fragment guard script."""

from pathlib import Path
from runpy import run_path

from ruamel.yaml import YAML
from workflow_helpers import _commands, _invocations, _invokes, _load_workflow

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = run_path(str(_ROOT / "scripts" / "check_doc_links.py"))
check_repository_links = _SCRIPT["check_repository_links"]
maintained_documents = _SCRIPT["maintained_documents"]
extract_links = _SCRIPT["extract_links"]

_SCRIPT_PATH = "scripts/check_doc_links.py"


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_relative_link_passes(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\nSee [arch](ARCHITECTURE.md).\n")
    _write(tmp_path, "ARCHITECTURE.md", "# Architecture\n")

    assert check_repository_links(tmp_path) == []


def test_missing_file_target_names_the_source_and_the_link(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\nSee [gone](MISSING.md).\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "MISSING.md" in messages[0]


def test_valid_anchor_passes(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[ad](ARCHITECTURE.md#ad-25-scanner).\n")
    _write(tmp_path, "ARCHITECTURE.md", "# Architecture\n\n## AD-25 scanner\n\nbody\n")

    assert check_repository_links(tmp_path) == []


def test_anchor_matching_no_heading_names_the_source_and_the_link(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[ad](ARCHITECTURE.md#ad-99-ghost).\n")
    _write(tmp_path, "ARCHITECTURE.md", "# Architecture\n\n## AD-25 scanner\n\nbody\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "ad-99-ghost" in messages[0]
    assert "ARCHITECTURE.md" in messages[0]


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

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "fixed-2" in messages[0]


def test_missing_target_does_not_also_report_a_missing_anchor(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[gone](MISSING.md#whatever).\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "MISSING.md" in messages[0]


def test_reference_style_link_is_checked(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\nSee [arch][ref].\n\n[ref]: MISSING.md\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "MISSING.md" in messages[0]


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

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "nope" in messages[0]
    assert "README.md" in messages[0]


def test_explicit_marker_heading_resolves_under_its_github_id(tmp_path):
    # GitHub has no {#id} syntax, so '## Notes {#n}' is addressable as '#notes-n', not '#n'.
    _write(tmp_path, "README.md", "# Readme\n\n[a](GUIDE.md#notes-n) [b](GUIDE.md#n).\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Notes {#n}\n\nbody\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "'#n'" in messages[0]


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

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "../outside.md" in messages[0]


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

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "MISSING.md?plain=1" in messages[0]


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

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "%2Fetc%2Fpasswd" in messages[0]


def test_percent_encoded_dot_segment_does_not_climb_out(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](%2E%2E/outside.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "outside.md" in messages[0]


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

    messages = check_repository_links(tmp_path)

    assert len(messages) == 1
    assert "no-such-heading" in messages[0]


def test_ordinary_query_accepts_a_resolving_fragment(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[x](GUIDE.md?utm_source=docs#section)\n")
    _write(tmp_path, "GUIDE.md", "# Guide\n\n## Section\n")

    assert check_repository_links(tmp_path) == []


def test_directory_target_is_accepted(tmp_path):
    _write(tmp_path, "README.md", "# Readme\n\n[vendor](vendor/)\n")
    (tmp_path / "vendor").mkdir()

    assert check_repository_links(tmp_path) == []


def test_messages_are_ordered_by_document_then_by_link(tmp_path):
    _write(tmp_path, "ZULU.md", "# Zulu\n\n[a](MISSING-Z.md)\n")
    _write(tmp_path, "ALPHA.md", "# Alpha\n\n[a](MISSING-A1.md)\n\n[b](MISSING-A2.md)\n")

    messages = check_repository_links(tmp_path)

    assert len(messages) == 3
    assert messages[0].startswith("ALPHA.md")
    assert "MISSING-A1.md" in messages[0]
    assert messages[1].startswith("ALPHA.md")
    assert "MISSING-A2.md" in messages[1]
    assert messages[2].startswith("ZULU.md")


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
    config = YAML(typ="safe").load((_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
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
