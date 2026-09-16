"""Tests for the sidecar manifest validation boundary (AD-51)."""

import os
from pathlib import Path

import pytest
import typer
from cli.helpers import _contents, _stub_runtime
from link_gate_helpers import _write

from doc_lattice.cli.errors import EXIT_TOOL_ERROR, exit_on_project_error
from doc_lattice.config import load_sidecar_config
from doc_lattice.error_types import ManifestError, ProjectError, RegistrationConflictError
from doc_lattice.path_utils import format_path_for_display
from doc_lattice.sidecar_manifest import build_registration_index

_MANIFEST = "meta/sidecars.yml"
_VALID_RECORD = "  - path: skills/a.md\n    meta: {id: skill-a}\n"


def _project(tmp_path: Path) -> Path:
    """A project root holding two registrable Markdown files, apart from the test's cwd."""
    root = tmp_path / "project"
    _write(root, "skills/a.md", "# A\n")
    _write(root, "skills/b.md", "# B\n")
    return root


def _manifest_error(root: Path, text: str, manifests: tuple[str, ...] = (_MANIFEST,)) -> str:
    _write(root, _MANIFEST, text)
    with pytest.raises(ManifestError) as excinfo:
        build_registration_index(manifests, root)
    assert excinfo.value.code == "MANIFEST_ERROR"
    return str(excinfo.value)


# --- A valid manifest --------------------------------------------------------------------------


def test_a_valid_manifest_yields_typed_registrations_with_both_sides_of_provenance(
    tmp_path: Path,
):
    root = _project(tmp_path)
    _write(
        root,
        _MANIFEST,
        "nodes:\n"
        "  - path: ./skills/a.md\n"
        "    meta:\n"
        "      id: skill-a\n"
        "      derives_from:\n"
        "        - {ref: spec#rule, seen: abc}\n"
        "  - path: skills/b.md\n"
        "    meta: {id: skill-b, layer: production}\n",
    )

    index = build_registration_index([_MANIFEST], root)

    manifest = index.manifests[0]
    assert manifest.declared == _MANIFEST
    assert manifest.resolved == (root / _MANIFEST).resolve()
    first, second = index.registrations
    # The declared spelling is identity and is kept as written, `./` included.
    assert first.declared_path == "./skills/a.md"
    assert first.target == (root / "skills/a.md").resolve()
    assert first.manifest is manifest
    assert first.position == 0
    assert first.meta.id == "skill-a"
    assert first.meta.derives_from[0].seen == "abc"
    assert second.position == 1
    assert second.meta.layer == "production"
    assert index.by_target == {first.target: first, second.target: second}


def test_record_paths_resolve_against_the_project_root_not_the_manifest(tmp_path: Path):
    # Moving a manifest never re-points its records: `skills/a.md` in a nested manifest still
    # names the root's file, not one beside the manifest.
    root = _project(tmp_path)
    _write(root, "deep/er/skills/a.md", "# decoy\n")
    _write(root, "deep/er/sidecars.yml", f"nodes:\n{_VALID_RECORD}")

    index = build_registration_index(["deep/er/sidecars.yml"], root)

    assert index.registrations[0].target == (root / "skills/a.md").resolve()


def test_the_private_seam_resolves_from_the_config_parent_whatever_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Config parent, working directory, and manifest directory all differ.
    root = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write(elsewhere, "skills/a.md", "# decoy\n")
    monkeypatch.chdir(elsewhere)
    config = root / ".doc-lattice.yml"
    _write(root, ".doc-lattice.yml", f"lattice_format: 2\nsidecar_manifests: [{_MANIFEST}]\n")
    _write(root, _MANIFEST, f"nodes:\n{_VALID_RECORD}")

    loaded = load_sidecar_config(config, elsewhere)
    index = build_registration_index(loaded.sidecar_manifests, loaded.project.project_root)

    assert index.registrations[0].target == (root / "skills/a.md").resolve()
    assert index.manifests[0].resolved == (root / _MANIFEST).resolve()


def test_quoted_text_spelling_reuse_characters_is_ordinary_text(tmp_path: Path):
    root = _project(tmp_path)
    _write(
        root,
        _MANIFEST,
        "nodes:\n"
        "  - path: skills/a.md\n"
        '    meta: {id: skill-a, title: \'a & *b << c\', tickets: ["&x", "<<"]}\n',
    )

    index = build_registration_index([_MANIFEST], root)

    assert index.registrations[0].meta.title == "a & *b << c"
    assert index.registrations[0].meta.tickets == ["&x", "<<"]


# --- Manifest-level failures -------------------------------------------------------------------


def test_a_missing_manifest_names_the_manifest(tmp_path: Path):
    root = _project(tmp_path)

    with pytest.raises(ManifestError) as excinfo:
        build_registration_index([_MANIFEST], root)

    assert format_path_for_display(_MANIFEST) in str(excinfo.value)
    assert "does not exist" in str(excinfo.value)


def test_a_directory_manifest_is_not_a_regular_file(tmp_path: Path):
    root = _project(tmp_path)
    (root / _MANIFEST).mkdir(parents=True)

    with pytest.raises(ManifestError, match="not a regular file") as excinfo:
        build_registration_index([_MANIFEST], root)

    assert format_path_for_display(_MANIFEST) in str(excinfo.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_manifest_is_refused_before_it_is_opened(tmp_path: Path):
    # Opening a FIFO with no writer blocks, so reaching the read would hang this test.
    root = _project(tmp_path)
    (root / "meta").mkdir()
    os.mkfifo(root / _MANIFEST)

    with pytest.raises(ManifestError, match="not a regular file"):
        build_registration_index([_MANIFEST], root)


def test_a_manifest_escaping_the_project_root_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    _write(tmp_path, "outside.yml", f"nodes:\n{_VALID_RECORD}")

    with pytest.raises(ManifestError, match="outside the project root") as excinfo:
        build_registration_index(["../outside.yml"], root)

    assert format_path_for_display("../outside.yml") in str(excinfo.value)


def test_a_manifest_escaping_through_a_symlink_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    outside = tmp_path / "outside.yml"
    _write(tmp_path, "outside.yml", f"nodes:\n{_VALID_RECORD}")
    (root / "meta").mkdir()
    (root / _MANIFEST).symlink_to(outside)

    with pytest.raises(ManifestError, match="outside the project root") as excinfo:
        build_registration_index([_MANIFEST], root)

    assert format_path_for_display(_MANIFEST) in str(excinfo.value)


def test_a_manifest_symlink_inside_the_project_is_followed(tmp_path: Path):
    root = _project(tmp_path)
    real = root / "real.yml"
    _write(root, "real.yml", f"nodes:\n{_VALID_RECORD}")
    (root / "meta").mkdir()
    (root / _MANIFEST).symlink_to(real)

    index = build_registration_index([_MANIFEST], root)

    assert index.manifests[0].declared == _MANIFEST
    assert index.manifests[0].resolved == real.resolve()


def test_a_non_utf8_manifest_is_a_read_failure(tmp_path: Path):
    root = _project(tmp_path)
    (root / "meta").mkdir()
    (root / _MANIFEST).write_bytes(b"nodes: [\xff]\n")

    with pytest.raises(ManifestError, match="cannot read manifest") as excinfo:
        build_registration_index([_MANIFEST], root)

    assert format_path_for_display(_MANIFEST) in str(excinfo.value)


def test_a_read_failure_after_the_file_check_is_a_manifest_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _project(tmp_path)
    _write(root, _MANIFEST, f"nodes:\n{_VALID_RECORD}")
    original = Path.read_text

    def refuse(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "sidecars.yml":
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Path, "read_text", refuse)

    with pytest.raises(ManifestError, match=r"cannot read manifest.*Permission denied"):
        build_registration_index([_MANIFEST], root)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("nodes: [\n", "cannot parse manifest", id="unparseable"),
        pytest.param(
            "nodes:\n  - path: skills/a.md\n    path: skills/b.md\n    meta: {id: x}\n",
            "cannot parse manifest",
            id="duplicate-key",
        ),
        pytest.param("- nodes\n", "does not hold a YAML mapping", id="sequence"),
        pytest.param("just text\n", "does not hold a YAML mapping", id="scalar"),
        pytest.param("", "does not hold a YAML mapping", id="empty-document"),
        pytest.param("other: 1\n", "unknown top-level keys 'other'", id="no-nodes-key"),
        pytest.param("{}\n", "non-empty list", id="empty-mapping"),
        pytest.param("nodes: []\n", "non-empty list", id="empty-nodes"),
        pytest.param("nodes:\n", "non-empty list", id="null-nodes"),
        pytest.param("nodes: {path: a.md}\n", "non-empty list", id="nodes-not-a-list"),
        pytest.param(
            f"nodes:\n{_VALID_RECORD}version: 2\n",
            "unknown top-level keys 'version'",
            id="extra-top-level-key",
        ),
    ],
)
def test_manifest_shape_failures_name_the_manifest(tmp_path: Path, text: str, expected: str):
    message = _manifest_error(_project(tmp_path), text)

    assert expected in message
    assert format_path_for_display(_MANIFEST) in message


def test_a_parse_failure_carries_the_parser_source_location(tmp_path: Path):
    message = _manifest_error(_project(tmp_path), "nodes:\n  - path: [unclosed\n")

    assert "line 3" in message


@pytest.mark.parametrize(
    ("text", "kind", "line", "column"),
    [
        pytest.param(
            "nodes:\n  - path: skills/a.md\n    meta: {id: skill-a, title: &unused t}\n",
            "anchor",
            3,
            32,
            id="unused-scalar-anchor",
        ),
        pytest.param(
            "defaults: &d {layer: production}\nnodes:\n" + _VALID_RECORD,
            "anchor",
            1,
            11,
            id="collection-anchor",
        ),
        pytest.param(
            "nodes:\n"
            "  - path: skills/a.md\n"
            "    meta:\n"
            "      id: skill-a\n"
            "      derives_from:\n"
            "        - {ref: spec, seen: *s}\n",
            "alias",
            6,
            29,
            id="nested-undefined-alias",
        ),
        pytest.param(
            "nodes:\n  - path: skills/a.md\n    meta: {<<: {id: skill-a}}\n",
            "merge key",
            3,
            12,
            id="merge-key-without-alias",
        ),
        pytest.param(
            "nodes:\n  - path: skills/a.md\n    meta: {!!merge m: {id: skill-a}}\n",
            "merge key",
            3,
            12,
            id="explicit-merge-tag",
        ),
    ],
)
def test_reuse_spellings_are_refused_even_when_the_result_would_validate(
    tmp_path: Path, text: str, kind: str, line: int, column: int
):
    message = _manifest_error(_project(tmp_path), text)

    assert f"spells a YAML {kind} at line {line}, column {column}" in message
    assert format_path_for_display(_MANIFEST) in message


def test_a_defined_alias_is_refused_at_its_anchor(tmp_path: Path):
    # The anchor comes first in document order, so it is what the refusal names.
    text = (
        "nodes:\n"
        "  - path: skills/a.md\n"
        "    meta: &m {id: skill-a}\n"
        "  - path: skills/b.md\n"
        "    meta: *m\n"
    )

    message = _manifest_error(_project(tmp_path), text)

    assert "spells a YAML anchor at line 3" in message


# --- Record-level failures ---------------------------------------------------------------------


def _record_error(tmp_path: Path, records: str) -> str:
    return _manifest_error(_project(tmp_path), f"nodes:\n{_VALID_RECORD}{records}")


@pytest.mark.parametrize(
    ("record", "spelling", "expected"),
    [
        pytest.param("  - skills/b.md\n", None, "is not a mapping", id="not-a-mapping"),
        pytest.param(
            "  - meta: {id: skill-b}\n", None, "wrong keys (missing 'path')", id="missing-path"
        ),
        pytest.param(
            "  - path: skills/b.md\n", "skills/b.md", "wrong keys (missing 'meta')", id="no-meta"
        ),
        pytest.param(
            "  - {path: skills/b.md, meta: {id: b}, id: b}\n",
            "skills/b.md",
            "wrong keys (unknown 'id')",
            id="extra-key",
        ),
        pytest.param(
            "  - {path: 7, meta: {id: b}}\n", None, "'path' that is not a string", id="non-str"
        ),
        pytest.param(
            "  - {path: skills/b.txt, meta: {id: b}}\n",
            "skills/b.txt",
            "does not name a '.md' file",
            id="non-markdown",
        ),
        pytest.param(
            "  - {path: /abs/skills/b.md, meta: {id: b}}\n",
            "/abs/skills/b.md",
            "is absolute",
            id="absolute",
        ),
        # Relative on POSIX but absolute once joined on Windows, so refused on every host to
        # keep a manifest's validity and identity independent of where it is loaded.
        pytest.param(
            "  - {path: 'C:/skills/b.md', meta: {id: b}}\n",
            "C:/skills/b.md",
            "is absolute",
            id="drive-prefixed",
        ),
        pytest.param(
            "  - {path: 'skills\\b.md', meta: {id: b}}\n",
            "skills\\b.md",
            "uses a backslash",
            id="backslash",
        ),
        pytest.param(
            '  - {path: "skills/\\u001bb.md", meta: {id: b}}\n',
            "skills/\x1bb.md",
            "contains a control character (U+001B at index 7)",
            id="control-character",
        ),
        pytest.param(
            "  - {path: skills/b.md, meta: {id: 'b#x'}}\n",
            "skills/b.md",
            "must not contain '#'",
            id="invalid-meta",
        ),
        pytest.param(
            "  - {path: skills/b.md, meta: {title: no id}}\n",
            "skills/b.md",
            "id: Field required",
            id="meta-without-id",
        ),
        pytest.param(
            "  - {path: skills/b.md, meta: [id]}\n",
            "skills/b.md",
            "<meta>:",
            id="meta-not-a-mapping",
        ),
        pytest.param(
            "  - {path: skills/missing.md, meta: {id: b}}\n",
            "skills/missing.md",
            "does not exist",
            id="missing-target",
        ),
        pytest.param(
            "  - {path: ../outside.md, meta: {id: b}}\n",
            "../outside.md",
            "resolves outside the project root",
            id="escaping-target",
        ),
    ],
)
def test_record_failures_name_the_manifest_position_and_spelling(
    tmp_path: Path, record: str, spelling: str | None, expected: str
):
    _write(tmp_path, "outside.md", "# outside\n")

    message = _record_error(tmp_path, record)

    assert expected in message
    assert "record nodes[1]" in message
    assert f"in manifest {format_path_for_display(_MANIFEST)}" in message
    if spelling is None:
        assert "(path " not in message
    else:
        assert f"(path {format_path_for_display(spelling)})" in message


def test_a_record_target_that_is_a_directory_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    (root / "skills/dir.md").mkdir()

    message = _manifest_error(root, "nodes:\n  - {path: skills/dir.md, meta: {id: d}}\n")

    assert "not a regular file" in message
    assert "record nodes[0] (path 'skills/dir.md')" in message


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_record_target_that_is_a_fifo_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    os.mkfifo(root / "skills/pipe.md")

    message = _manifest_error(root, "nodes:\n  - {path: skills/pipe.md, meta: {id: p}}\n")

    assert "not a regular file" in message


def test_a_record_escaping_through_a_symlink_is_refused(tmp_path: Path):
    root = _project(tmp_path)
    outside = tmp_path / "outside.md"
    _write(tmp_path, "outside.md", "# outside\n")
    (root / "skills/link.md").symlink_to(outside)

    message = _manifest_error(root, "nodes:\n  - {path: skills/link.md, meta: {id: l}}\n")

    assert "resolves outside the project root" in message
    assert "record nodes[0] (path 'skills/link.md')" in message


def test_a_dangling_record_symlink_is_a_missing_target(tmp_path: Path):
    root = _project(tmp_path)
    (root / "skills/dangling.md").symlink_to(root / "skills/gone.md")

    message = _manifest_error(root, "nodes:\n  - {path: skills/dangling.md, meta: {id: d}}\n")

    assert "does not exist" in message


# --- Singular ownership ------------------------------------------------------------------------


def _conflict(root: Path, manifests: tuple[str, ...]) -> str:
    with pytest.raises(RegistrationConflictError) as excinfo:
        build_registration_index(manifests, root)
    assert excinfo.value.code == "REGISTRATION_CONFLICT"
    return str(excinfo.value)


def test_two_records_in_one_manifest_aliasing_one_target_name_both(tmp_path: Path):
    root = _project(tmp_path)
    _write(
        root,
        _MANIFEST,
        "nodes:\n"
        "  - {path: skills/a.md, meta: {id: one}}\n"
        "  - {path: skills/../skills/a.md, meta: {id: two}}\n",
    )

    message = _conflict(root, (_MANIFEST,))

    assert "record nodes[0] (path 'skills/a.md') in manifest 'meta/sidecars.yml'" in message
    assert (
        "record nodes[1] (path 'skills/../skills/a.md') in manifest 'meta/sidecars.yml'" in message
    )


def test_two_records_across_manifests_aliasing_through_a_symlink_name_both(tmp_path: Path):
    root = _project(tmp_path)
    (root / "skills/alias.md").symlink_to(root / "skills/a.md")
    _write(root, "one.yml", "nodes:\n  - {path: skills/b.md, meta: {id: b}}\n" + _VALID_RECORD)
    _write(root, "two.yml", "nodes:\n  - {path: skills/alias.md, meta: {id: alias}}\n")

    message = _conflict(root, ("one.yml", "two.yml"))

    assert "record nodes[1] (path 'skills/a.md') in manifest 'one.yml'" in message
    assert "record nodes[0] (path 'skills/alias.md') in manifest 'two.yml'" in message


def test_distinct_targets_across_manifests_are_all_registered(tmp_path: Path):
    root = _project(tmp_path)
    _write(root, "one.yml", f"nodes:\n{_VALID_RECORD}")
    _write(root, "two.yml", "nodes:\n  - {path: skills/b.md, meta: {id: skill-b}}\n")

    index = build_registration_index(["one.yml", "two.yml"], root)

    assert [r.declared_path for r in index.registrations] == ["skills/a.md", "skills/b.md"]
    assert [r.manifest.declared for r in index.registrations] == ["one.yml", "two.yml"]


# --- Exit mapping and the user-facing refusal --------------------------------------------------


@pytest.mark.parametrize(
    "error", [ManifestError("bad manifest"), RegistrationConflictError("two claims")]
)
def test_the_existing_cli_handler_exits_2_with_the_code(tmp_path: Path, error: ProjectError):
    runtime = _stub_runtime(tmp_path, "the manifest error handler")

    with pytest.raises(typer.Exit) as excinfo, exit_on_project_error(runtime):
        raise error

    assert excinfo.value.exit_code == EXIT_TOOL_ERROR
    assert f"error ({error.code}): {error}" in _contents(runtime.stderr)
