"""Tests for the version-consistency guard script."""

from pathlib import Path
from runpy import run_path

import pytest

from doc_lattice import __version__

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = run_path(str(_ROOT / "scripts" / "check_version_sync.py"))
check_version_consistency = _SCRIPT["check_version_consistency"]
maintained_documents = _SCRIPT["maintained_documents"]
PinPolicy = _SCRIPT["PinPolicy"]
PIN_POLICY = _SCRIPT["PIN_POLICY"]

_PYPROJECT = '[project]\nname = "doc-lattice"\nversion = "0.4.0"\n'
_CHANGELOG = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n### Added\n\n- thing\n"
_README = "# doc-lattice\n\nuvx --from doc-lattice==0.4.0 doc-lattice --help\n"

# Small stand-ins for the repository's own manifest, so a fixture document needs one pin rather
# than README.md's three. The real counts are exercised against the real tree by
# `test_the_repository_satisfies_its_own_manifest`. The default is the single-document surface
# most cases need; declaring a document the case does not supply would itself be a violation.
_MANIFEST = {"README.md": 1}
_BOTH_SURFACES = {"README.md": 1, "MANAGED_CI.md": 1}
_HISTORICAL = frozenset({"CHANGELOG.md"})


def _check(docs, *, init_version="0.4.0", pyproject=_PYPROJECT, changelog=_CHANGELOG, **policy):
    """Run the guard against ``docs`` under the small single-document test policy.

    Every case varies ``docs``; the rest are keyword overrides so a call site names only what it
    is actually exercising.

    Args:
        docs: The maintained documents to classify, mapping filename to text.
        init_version: The canonical version the sources must agree with.
        pyproject: The ``pyproject.toml`` text to read the version from.
        changelog: The ``CHANGELOG.md`` text to read the top heading from.
        **policy: ``manifest`` and/or ``historical`` overrides for the ``PinPolicy``, each
            defaulting to the small test policy. Any other keyword is refused rather than
            ignored, since a typo would otherwise silently restore the default and pass.

    Returns:
        The guard's messages for that combination.
    """
    unknown = set(policy) - {"manifest", "historical"}
    assert not unknown, f"_check got unknown policy override(s): {sorted(unknown)}"
    pin_policy = PinPolicy(
        manifest=policy.get("manifest", _MANIFEST),
        historical=policy.get("historical", _HISTORICAL),
    )
    return check_version_consistency(init_version, pyproject, changelog, docs, pin_policy)


def test_all_sources_agree_returns_empty():
    docs = {"README.md": _README}
    assert _check(docs) == []


def test_pyproject_disagrees_is_reported_naming_both_found_and_expected():
    pyproject = '[project]\nname = "doc-lattice"\nversion = "0.3.0"\n'
    messages = _check({"README.md": _README}, pyproject=pyproject)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]
    assert "0.3.0" in messages[0]  # the value actually found in pyproject
    assert "0.4.0" in messages[0]  # the expected (canonical) value


def test_changelog_disagrees_is_reported():
    changelog = "# Changelog\n\n## [0.3.0] - 2026-06-28\n"
    messages = _check({"README.md": _README}, changelog=changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_both_disagree_returns_two_messages():
    pyproject = '[project]\nversion = "0.1.0"\n'
    changelog = "# Changelog\n\n## [0.2.0]\n"
    messages = _check({"README.md": _README}, pyproject=pyproject, changelog=changelog)
    assert len(messages) == 2


def test_unreleased_heading_is_skipped():
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-01\n"
    docs = {"README.md": _README}
    assert _check(docs, changelog=changelog) == []


def test_first_version_heading_wins_over_later_ones():
    # Two real release headings stacked newest-first; the TOP one is canonical.
    changelog = "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n## [0.3.0] - 2026-06-28\n"
    # Top heading 0.4.0 agrees with init + _PYPROJECT (both 0.4.0) -> consistent.
    docs = {"README.md": _README}
    assert _check(docs, changelog=changelog) == []
    # Make pyproject agree with 0.3.0 so ONLY the changelog can disagree; if the
    # function wrongly picked the bottom heading (0.3.0), this would be [].
    pyproject_030 = '[project]\nname = "doc-lattice"\nversion = "0.3.0"\n'
    readme_030 = "uvx --from doc-lattice==0.3.0 doc-lattice\n"
    docs_030 = {"README.md": readme_030}
    messages = _check(docs_030, init_version="0.3.0", pyproject=pyproject_030, changelog=changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]
    assert "0.4.0" in messages[0]  # matched the TOP heading, not 0.3.0


@pytest.mark.parametrize(
    "pyproject",
    [
        pytest.param('[project]\nname = "doc-lattice"\n', id="no-version-key"),
        pytest.param('name = "doc-lattice"\nversion = "0.4.0"\n', id="no-project-table"),
        # [project] parses to a string, not a table; must be reported, never crash.
        pytest.param('project = "doc-lattice"\n', id="project-is-not-a-table"),
        pytest.param("[project", id="unterminated-table-header"),
    ],
)
def test_an_unreadable_pyproject_version_is_a_mismatch_not_an_error(pyproject):
    messages = _check({"README.md": _README}, pyproject=pyproject)
    assert len(messages) == 1
    assert "pyproject.toml" in messages[0]


def test_changelog_without_version_heading_is_a_mismatch():
    changelog = "# Changelog\n\nNo releases yet.\n"
    messages = _check({"README.md": _README}, changelog=changelog)
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_readme_pypi_pin_matches_is_consistent():
    readme = "uvx --from doc-lattice==0.4.0 doc-lattice\n"
    docs = {"README.md": readme}
    assert _check(docs) == []


def test_readme_tagged_git_pin_matches_is_consistent():
    readme = "uvx --from git+https://github.com/Guardantix/doc-lattice@v0.4.0 doc-lattice\n"
    docs = {"README.md": readme}
    assert _check(docs) == []


def test_readme_stale_pypi_pin_is_reported():
    readme = "uvx --from doc-lattice==0.3.0 doc-lattice\n"
    messages = _check({"README.md": readme})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "0.3.0" in messages[0]
    assert "0.4.0" in messages[0]


def test_readme_stale_tagged_git_pin_is_reported():
    readme = "uvx --from git+https://github.com/Guardantix/doc-lattice@v0.3.0 doc-lattice\n"
    messages = _check({"README.md": readme})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "0.3.0" in messages[0]


def test_readme_duplicate_stale_version_across_pin_syntaxes_yields_one_stale_message():
    readme = (
        "uvx --from doc-lattice==0.3.0 doc-lattice init\n"
        "uvx --from git+https://github.com/Guardantix/"
        "doc-lattice@v0.3.0 doc-lattice --help\n"
    )
    messages = _check({"README.md": readme}, manifest={"README.md": 2})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "0.3.0" in messages[0]


def test_pinned_doc_ignores_pin_substrings_in_other_distribution_names():
    readme = (
        "uvx --from other-doc-lattice==0.3.0 other-doc-lattice\n"
        "uvx --from xdoc-lattice==0.3.0 xdoc-lattice\n"
        "uvx --from other-doc-lattice@v0.3.0 other-doc-lattice\n"
        "uvx --from xdoc-lattice@v0.3.0 xdoc-lattice\n"
    )
    docs = {"README.md": readme}
    assert _check(docs, manifest={"README.md": 0}) == []


def test_pinned_doc_ignores_extended_version_tokens():
    readme = (
        "uvx --from doc-lattice==0.3.0.1 doc-lattice\n"
        "uvx --from doc-lattice==0.3.0rc1 doc-lattice\n"
        "uvx --from doc-lattice@v0.3.0.1 doc-lattice\n"
        "uvx --from doc-lattice@v0.3.0rc1 doc-lattice\n"
    )
    docs = {"README.md": readme}
    assert _check(docs, manifest={"README.md": 0}) == []


def test_managed_ci_stale_pin_is_reported_by_that_name():
    managed_ci = "uvx --from doc-lattice==0.3.0 doc-lattice init --default-branch main\n"
    docs = {"README.md": _README, "MANAGED_CI.md": managed_ci}
    messages = _check(docs, manifest=_BOTH_SURFACES)
    assert len(messages) == 1
    assert "MANAGED_CI.md" in messages[0]
    assert "README.md" not in messages[0]
    assert "0.3.0" in messages[0]
    assert "0.4.0" in messages[0]


def test_every_stale_pinned_doc_gets_its_own_message():
    readme = "uvx --from doc-lattice==0.2.0 doc-lattice\n"
    managed_ci = "uvx --from doc-lattice==0.3.0 doc-lattice init --default-branch main\n"
    docs = {"README.md": readme, "MANAGED_CI.md": managed_ci}
    messages = _check(docs, manifest=_BOTH_SURFACES)
    assert len(messages) == 2
    # Docs are classified in the mapping's insertion order.
    assert "README.md" in messages[0]
    assert "0.2.0" in messages[0]
    assert "MANAGED_CI.md" in messages[1]
    assert "0.3.0" in messages[1]


def test_stale_pins_within_one_doc_report_in_first_appearance_order():
    readme = (
        "uvx --from doc-lattice==0.3.0 doc-lattice\n"
        "uvx --from doc-lattice==0.2.0 doc-lattice\n"
        "uvx --from doc-lattice==0.3.0 doc-lattice --help\n"
    )
    messages = _check({"README.md": readme}, manifest={"README.md": 3})
    assert len(messages) == 2
    assert "0.3.0" in messages[0]
    assert "0.2.0" in messages[1]


# --- The exact-count manifest -------------------------------------------------------------


def test_deleting_a_required_pin_fails_with_the_document_and_expected_count():
    readme = "uvx --from doc-lattice==0.4.0 doc-lattice\n"  # one of the two required pins gone
    messages = _check({"README.md": readme}, manifest={"README.md": 2})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "1" in messages[0]  # the count actually found
    assert "2" in messages[0]  # the count the manifest declares


def test_removing_every_pin_from_a_declared_surface_fails():
    readme = "# doc-lattice\n\nNo install instructions here.\n"
    messages = _check({"README.md": readme})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "expected exactly 1" in messages[0]


def test_adding_an_extra_pin_to_a_declared_surface_fails_the_count():
    readme = (
        "uvx --from doc-lattice==0.4.0 doc-lattice\n"
        "uvx --from doc-lattice==0.4.0 doc-lattice init\n"
    )
    # Both pins are current, so only the count can fail: a minimum would have passed here.
    messages = _check({"README.md": readme})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "expected exactly 1" in messages[0]


def test_a_compensating_add_nets_to_the_count_and_an_unrecognized_spelling_does_not():
    """What the exact count does and does not close, in one case.

    A deletion compensated by another recognized current pin in the same document keeps the
    total and passes, because the manifest counts occurrences rather than identifying sites.
    What the count does catch is any change that lowers the total, including one that reformats
    a required occurrence into a spelling ``_PINNED_REF`` never sees.
    """
    readme = (
        "uvx --from doc-lattice==0.4.0 doc-lattice\n"
        "uvx --from doc-lattice==0.4.0 doc-lattice init\n"
    )
    assert _check({"README.md": readme}, manifest={"README.md": 2}) == []
    # One required occurrence reformatted into a spelling `_PINNED_REF` does not recognize:
    # the total falls to one and the failure names the document.
    reformatted = (
        "uvx --from doc-lattice == 0.4.0 doc-lattice\n"
        "uvx --from doc-lattice==0.4.0 doc-lattice init\n"
    )
    messages = _check({"README.md": reformatted}, manifest={"README.md": 2})
    assert len(messages) == 1
    assert "README.md" in messages[0]


def test_a_document_declared_and_exempted_at_once_is_reported():
    """The exemption runs first, so an overlap would silence a declared count with no message."""
    messages = _check({"README.md": "# doc-lattice\n"}, historical=frozenset({"README.md"}))
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "PIN_MANIFEST" in messages[0]
    assert "HISTORICAL_PIN_DOCS" in messages[0]


def test_a_pin_outside_the_manifest_fails_as_an_unclassified_release_surface():
    docs = {"README.md": _README, "ROADMAP.md": "Install doc-lattice==0.4.0 to try it.\n"}
    messages = _check(docs)
    assert len(messages) == 1
    assert "ROADMAP.md" in messages[0]
    assert "not a declared release surface" in messages[0]
    assert "PIN_MANIFEST" in messages[0]


def test_a_current_pin_outside_the_manifest_still_fails():
    """Enrollment is the contract, not currency: a matching version is not a free pass."""
    docs = {"README.md": _README, "SECURITY.md": "doc-lattice==0.4.0\n"}
    messages = _check(docs)
    assert len(messages) == 1
    assert "SECURITY.md" in messages[0]


def test_a_maintained_document_with_no_pin_is_not_a_release_surface():
    docs = {"README.md": _README, "ROADMAP.md": "# Roadmap\n\nNothing pinned here.\n"}
    assert _check(docs) == []


def test_the_historical_exemption_passes_superseded_pins_unchanged():
    changelog_text = (
        "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n"
        "- Managed installs: run `uvx --from doc-lattice==0.3.0 doc-lattice ci refresh`.\n"
        "- Generated gates install an exact `doc-lattice==0.1.0` requirement.\n"
    )
    docs = {"README.md": _README, "CHANGELOG.md": changelog_text}
    assert _check(docs, changelog=changelog_text) == []


def test_without_the_exemption_the_same_changelog_would_fail():
    """Pins the exemption's load: it is what keeps preserved history out of the scan."""
    changelog_text = (
        "# Changelog\n\n## [0.4.0] - 2026-07-01\n\n"
        "- Managed installs: run `uvx --from doc-lattice==0.3.0 doc-lattice ci refresh`.\n"
    )
    docs = {"README.md": _README, "CHANGELOG.md": changelog_text}
    messages = _check(docs, changelog=changelog_text, historical=frozenset())
    assert len(messages) == 1
    assert "CHANGELOG.md" in messages[0]


def test_a_manifest_document_absent_from_the_maintained_set_is_reported():
    """Deleting a declared release surface must not read as compliance."""
    messages = _check({})
    assert len(messages) == 1
    assert "README.md" in messages[0]
    assert "not a maintained document" in messages[0]


def test_a_document_absent_from_the_mapping_is_not_scanned():
    managed_ci = "uvx --from doc-lattice==0.3.0 doc-lattice init --default-branch main\n"
    with_doc = _check({"MANAGED_CI.md": managed_ci}, manifest={"MANAGED_CI.md": 1})
    assert len(with_doc) == 1
    assert _check({}, manifest={}) == []


# --- Correspondence with the real repository ----------------------------------------------


def _repository_documents():
    return {path.name: path.read_text(encoding="utf-8") for path in maintained_documents(_ROOT)}


def test_the_repository_satisfies_its_own_manifest():
    """The shipped counts and the historical exemption, exercised against the real tree."""
    docs = _repository_documents()
    pyproject_text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert check_version_consistency(__version__, pyproject_text, docs["CHANGELOG.md"], docs) == []


def test_the_changelog_really_does_carry_superseded_pins():
    """Without this the exemption above could be passing only because nothing needs it."""
    superseded = [
        version
        for version in _SCRIPT["_recognized_pins"](_repository_documents()["CHANGELOG.md"])
        if version != __version__
    ]
    assert superseded, "CHANGELOG.md carries no superseded pins; HISTORICAL_PIN_DOCS is now moot"


def _write_selection_fixture(root):
    """Write the shapes that distinguish a maintained document from every near miss."""
    (root / "README.md").write_text("# Readme\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (root / "notes.txt").write_text("text\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "staging.md").write_text("# Staged\n", encoding="utf-8")
    (root / "directory.md").mkdir()


def test_maintained_documents_are_the_sorted_root_markdown_files(tmp_path):
    _write_selection_fixture(tmp_path)

    assert [path.name for path in maintained_documents(tmp_path)] == ["AGENTS.md", "README.md"]


def test_the_shipped_policy_keeps_its_two_classifications_exclusive():
    assert set(PIN_POLICY.manifest).isdisjoint(PIN_POLICY.historical)


def test_every_policy_document_is_a_maintained_document():
    names = set(_repository_documents())
    assert set(PIN_POLICY.manifest) <= names
    assert set(PIN_POLICY.historical) <= names


def test_maintained_documents_matches_the_doc_links_definition(tmp_path):
    """One definition of "maintained document", asserted rather than assumed.

    The two guards cannot import each other -- neither script is on the other's path -- so the
    selection is spelled twice and pinned here instead.
    """
    doc_links = run_path(str(_ROOT / "scripts" / "check_doc_links.py"))
    assert maintained_documents(_ROOT) == doc_links["maintained_documents"](_ROOT)
    # The real tree carries no staged directory and no ``.md`` directory, so agreeing on it
    # alone would leave the parts of the selection that differ untested.
    _write_selection_fixture(tmp_path)
    assert maintained_documents(tmp_path) == doc_links["maintained_documents"](tmp_path)
