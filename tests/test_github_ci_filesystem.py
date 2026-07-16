"""Tests for managed GitHub CI artifact filesystem operations."""

import stat
from pathlib import Path, PurePosixPath

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci import filesystem
from doc_lattice.github_ci.filesystem import (
    MAX_WORKFLOW_BYTES,
    apply_changes,
    inspect_installed_artifacts,
    preflight_create,
    preflight_refresh,
    render_diff,
)
from doc_lattice.github_ci.model import ArtifactChange, ManagedArtifact
from doc_lattice.github_ci.render import CANONICAL_ARTIFACT_TARGETS, render_managed_artifacts


def _write_artifacts(root: Path, artifacts: tuple[ManagedArtifact, ...]) -> None:
    """Write rendered artifacts directly for filesystem preflight setup."""
    for artifact in artifacts:
        destination = root / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.text, encoding="utf-8")


def _artifact_bytes(root: Path, artifacts: tuple[ManagedArtifact, ...]) -> list[bytes]:
    """Read every artifact's exact bytes in canonical order."""
    return [(root / artifact.relative_path).read_bytes() for artifact in artifacts]


def test_preflight_create_on_empty_root_returns_three_creates_without_making_directories(
    tmp_path: Path,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")

    changes = preflight_create(tmp_path, artifacts)

    assert [change.action for change in changes] == ["create", "create", "create"]
    assert [change.artifact for change in changes] == list(artifacts)
    assert [change.before for change in changes] == [None, None, None]
    assert [change.destination for change in changes] == [
        (tmp_path / artifact.relative_path).resolve() for artifact in artifacts
    ]
    assert not (tmp_path / ".github").exists()


@pytest.mark.parametrize("preflight", [preflight_create, preflight_refresh])
def test_exact_artifacts_are_current(tmp_path: Path, preflight):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, artifacts)

    changes = preflight(tmp_path, artifacts)

    assert [change.action for change in changes] == ["current", "current", "current"]
    assert [change.before for change in changes] == [
        artifact.text.encode("utf-8") for artifact in artifacts
    ]


def test_create_conflict_names_canonical_path_without_writing_any_artifact(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    conflict = tmp_path / artifacts[1].relative_path
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"user-owned workflow\n")

    with pytest.raises(ConfigError, match=r"\.github/workflows/doc-lattice-linear\.yml"):
        preflight_create(tmp_path, artifacts)

    assert not (tmp_path / artifacts[0].relative_path).exists()
    assert conflict.read_bytes() == b"user-owned workflow\n"
    assert not (tmp_path / artifacts[2].relative_path).exists()


def test_refresh_replaces_a_prior_valid_managed_set(tmp_path: Path):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)

    changes = preflight_refresh(tmp_path, new_artifacts)

    assert [change.action for change in changes] == ["replace", "replace", "replace"]
    assert [change.before for change in changes] == [
        artifact.text.encode("utf-8") for artifact in old_artifacts
    ]

    apply_changes(changes)

    assert _artifact_bytes(tmp_path, new_artifacts) == [
        artifact.text.encode("utf-8") for artifact in new_artifacts
    ]


def test_refresh_rejects_unmarked_user_file(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    target = tmp_path / artifacts[0].relative_path
    target.parent.mkdir(parents=True)
    target.write_text("name: user workflow\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"ownership marker.*doc-lattice\.yml"):
        preflight_refresh(tmp_path, artifacts)

    assert target.read_text(encoding="utf-8") == "name: user workflow\n"


def test_refresh_rejects_non_utf8_existing_file(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    target = tmp_path / artifacts[0].relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfeuser-owned")

    with pytest.raises(ConfigError, match=r"UTF-8.*doc-lattice\.yml"):
        preflight_refresh(tmp_path, artifacts)

    assert target.read_bytes() == b"\xff\xfeuser-owned"


def test_preflight_rejects_symlinked_target(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    backing = tmp_path / "backing.yml"
    backing.write_text(artifacts[0].text, encoding="utf-8")
    target = tmp_path / artifacts[0].relative_path
    target.parent.mkdir(parents=True)
    target.symlink_to(backing)

    with pytest.raises(ConfigError, match=r"symlink.*doc-lattice\.yml"):
        preflight_create(tmp_path, artifacts)

    assert backing.read_text(encoding="utf-8") == artifacts[0].text


def test_preflight_rejects_symlinked_parent_that_escapes_root(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".github").symlink_to(outside, target_is_directory=True)
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")

    with pytest.raises(ConfigError, match=r"outside.*doc-lattice\.yml"):
        preflight_create(root, artifacts)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("preflight", [preflight_create, preflight_refresh])
def test_preflight_rejects_in_repo_symlinked_workflows_ancestor(tmp_path: Path, preflight):
    inside = tmp_path / "real-workflows"
    inside.mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/workflows").symlink_to(inside, target_is_directory=True)
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")

    with pytest.raises(ConfigError, match=r"symlink.*\.github/workflows"):
        preflight(tmp_path, artifacts)

    assert list(inside.iterdir()) == []


def test_inspect_rejects_in_repo_symlinked_workflows_ancestor(tmp_path: Path):
    inside = tmp_path / "real-workflows"
    inside.mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/workflows").symlink_to(inside, target_is_directory=True)

    with pytest.raises(ConfigError) as caught:
        inspect_installed_artifacts(tmp_path, CANONICAL_ARTIFACT_TARGETS)

    assert "symlink" in str(caught.value)
    assert ".github/workflows" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_apply_create_rechecks_symlinked_ancestor_after_mkdir(tmp_path: Path, monkeypatch):
    # A concurrent swap can replace a real ancestor directory with an in-repo symlink whose
    # resolved destination is unchanged, so the post-mkdir ancestor recheck must fail closed
    # even when containment resolution still matches the preflight destination.
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    changes = preflight_create(tmp_path, (artifacts[0],))
    stable_destination = changes[0].destination
    inside = tmp_path / "real-workflows"
    inside.mkdir()
    real_mkdir = Path.mkdir

    def _planting_mkdir(_self: Path, *_args: object, **_kwargs: object) -> None:
        github = tmp_path / ".github"
        if not github.exists():
            real_mkdir(github)
        workflows = tmp_path / ".github/workflows"
        if not workflows.is_symlink():
            workflows.symlink_to(inside, target_is_directory=True)

    monkeypatch.setattr(Path, "mkdir", _planting_mkdir)
    monkeypatch.setattr(filesystem, "safe_resolve", lambda _logical, _root: stable_destination)

    with pytest.raises(ConfigError, match=r"symlink.*\.github/workflows"):
        apply_changes(changes)

    assert list(inside.iterdir()) == []


def test_preflight_bounds_oversized_managed_file(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    target = tmp_path / artifacts[0].relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * (MAX_WORKFLOW_BYTES + 1))

    with pytest.raises(ConfigError, match=r"byte limit.*doc-lattice\.yml"):
        preflight_create(tmp_path, artifacts)


def test_apply_replace_bounds_oversized_destination_after_preflight(tmp_path: Path):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)
    changes = preflight_refresh(tmp_path, new_artifacts)
    target = tmp_path / old_artifacts[0].relative_path
    target.write_bytes(b"x" * (MAX_WORKFLOW_BYTES + 1))

    with pytest.raises(ConfigError, match=r"byte limit.*doc-lattice\.yml"):
        apply_changes(changes)


def test_apply_create_error_detail_hides_absolute_path(tmp_path: Path, monkeypatch):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    changes = preflight_create(tmp_path, artifacts)
    secret = tmp_path / "SECRET_ABSOLUTE_SEGMENT"

    def _fail_mkdir(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "Permission denied", str(secret))

    monkeypatch.setattr(Path, "mkdir", _fail_mkdir)

    with pytest.raises(ConfigError, match=r"Permission denied.*doc-lattice\.yml") as caught:
        apply_changes(changes)

    assert "SECRET_ABSOLUTE_SEGMENT" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_preflight_rejects_non_regular_existing_target(tmp_path: Path):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    target = tmp_path / artifacts[0].relative_path
    target.mkdir(parents=True)

    with pytest.raises(ConfigError, match=r"regular file.*doc-lattice\.yml"):
        preflight_create(tmp_path, artifacts)

    assert target.is_dir()


def test_refresh_creates_a_missing_artifact_while_replacing_prior_artifacts(tmp_path: Path):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts[:2])

    changes = preflight_refresh(tmp_path, new_artifacts)

    assert [change.action for change in changes] == ["replace", "replace", "create"]
    assert changes[2].before is None

    apply_changes(changes)

    assert _artifact_bytes(tmp_path, new_artifacts) == [
        artifact.text.encode("utf-8") for artifact in new_artifacts
    ]


@pytest.mark.parametrize(
    "old_repository",
    [
        "FormerOwner/former-repository",
        "guardantix/DOC-LATTICE",
    ],
)
def test_refresh_accepts_valid_old_repository_marker_difference(
    tmp_path: Path,
    old_repository: str,
):
    old_artifacts = render_managed_artifacts(old_repository, "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)

    changes = preflight_refresh(tmp_path, new_artifacts)

    assert [change.action for change in changes] == ["replace", "replace", "replace"]


def _replace_header_line(text: str, prefix: str, replacement: str) -> str:
    """Replace one ownership header line in rendered artifact text."""
    lines = text.splitlines(keepends=True)
    index = next(index for index, line in enumerate(lines) if line.startswith(prefix))
    lines[index] = replacement + "\n"
    return "".join(lines)


@pytest.mark.parametrize(
    ("prefix", "replacement"),
    [
        ("# doc-lattice-managed:", "# doc-lattice-managed: github-ci-v2"),
        ("# doc-lattice-version:", "# doc-lattice-version: 2.1"),
        ("# doc-lattice-version:", "# doc-lattice-version: 2.2.0"),
        ("# doc-lattice-artifact:", "# doc-lattice-artifact: linear"),
        ("# doc-lattice-repository:", "# doc-lattice-version: 2.0.0"),
        ("# doc-lattice-managed:", "# ordinary user comment"),
    ],
)
def test_refresh_rejects_malformed_future_duplicate_or_mismatched_marker(
    tmp_path: Path,
    prefix: str,
    replacement: str,
):
    old_artifact = render_managed_artifacts("FormerOwner/former-repository", "2.0.0")[0]
    desired_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    target = tmp_path / old_artifact.relative_path
    target.parent.mkdir(parents=True)
    target.write_text(
        _replace_header_line(old_artifact.text, prefix, replacement),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"ownership marker.*doc-lattice\.yml"):
        preflight_refresh(tmp_path, desired_artifacts)


def test_refresh_rejects_duplicate_ownership_marker_after_header(tmp_path: Path):
    old_artifact = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")[0]
    desired_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    target = tmp_path / old_artifact.relative_path
    target.parent.mkdir(parents=True)
    old_text = old_artifact.text.replace(
        "name: doc-lattice\n",
        "# doc-lattice-version: 2.0.0\nname: doc-lattice\n",
        1,
    )
    target.write_text(old_text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"ownership marker.*doc-lattice\.yml"):
        preflight_refresh(tmp_path, desired_artifacts)


@pytest.mark.parametrize(
    "separator",
    [
        b"\v",
        b"\f",
        "\u0085".encode(),
        "\u2028".encode(),
        "\u2029".encode(),
        b"\r",
    ],
    ids=["vertical-tab", "form-feed", "nel", "line-separator", "paragraph-separator", "cr"],
)
def test_refresh_rejects_non_lf_ownership_line_separators(
    tmp_path: Path,
    separator: bytes,
):
    old_artifact = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")[0]
    desired_artifact = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[0]
    target = tmp_path / old_artifact.relative_path
    target.parent.mkdir(parents=True)
    old_bytes = separator.join(old_artifact.text.encode("utf-8").split(b"\n"))
    target.write_bytes(old_bytes)

    with pytest.raises(ConfigError, match=r"ownership marker.*doc-lattice\.yml"):
        preflight_refresh(tmp_path, (desired_artifact,))

    assert target.read_bytes() == old_bytes


def test_refresh_accepts_crlf_ownership_header_lines(tmp_path: Path):
    old_artifact = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")[0]
    desired_artifact = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[0]
    target = tmp_path / old_artifact.relative_path
    target.parent.mkdir(parents=True)
    old_bytes = old_artifact.text.replace("\n", "\r\n").encode("utf-8")
    target.write_bytes(old_bytes)

    changes = preflight_refresh(tmp_path, (desired_artifact,))

    assert [change.action for change in changes] == ["replace"]
    assert changes[0].before == old_bytes


def test_preflight_rejects_artifact_role_path_mismatch(tmp_path: Path):
    rendered = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")[0]
    mismatched = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice-linear.yml"),
        text=rendered.text,
    )

    with pytest.raises(ConfigError, match=r"role.*canonical path"):
        preflight_create(tmp_path, (mismatched,))


def test_render_diff_is_stable_for_create():
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text="line one\nline two\n",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="create",
        before=None,
    )

    assert render_diff((change,)) == (
        "--- /dev/null\n"
        "+++ b/.github/workflows/doc-lattice.yml\n"
        "@@ -0,0 +1,2 @@\n"
        "+line one\n"
        "+line two\n"
    )


def test_render_diff_is_stable_for_replace_and_omits_current():
    replace_artifact = ManagedArtifact(
        role="linear",
        relative_path=PurePosixPath(".github/workflows/doc-lattice-linear.yml"),
        text="new first\nsame second\n",
    )
    current_artifact = ManagedArtifact(
        role="bootstrap",
        relative_path=PurePosixPath(".github/doc-lattice-bootstrap.sh"),
        text="unchanged\n",
    )
    changes = (
        ArtifactChange(
            artifact=replace_artifact,
            root=Path("/repo"),
            destination=Path("/repo/.github/workflows/doc-lattice-linear.yml"),
            action="replace",
            before=b"old first\nsame second\n",
        ),
        ArtifactChange(
            artifact=current_artifact,
            root=Path("/repo"),
            destination=Path("/repo/.github/doc-lattice-bootstrap.sh"),
            action="current",
            before=b"unchanged\n",
        ),
    )

    assert render_diff(changes) == (
        "--- a/.github/workflows/doc-lattice-linear.yml\n"
        "+++ b/.github/workflows/doc-lattice-linear.yml\n"
        "@@ -1,2 +1,2 @@\n"
        "-old first\n"
        "+new first\n"
        " same second\n"
    )
    assert render_diff((changes[1],)) == ""


def test_render_diff_preserves_crlf_difference_in_replacement_content():
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text="managed line\n",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="replace",
        before=b"managed line\r\n",
    )

    assert render_diff((change,)) == (
        "--- a/.github/workflows/doc-lattice.yml\n"
        "+++ b/.github/workflows/doc-lattice.yml\n"
        "@@ -1 +1 @@\n"
        "-managed line\r\n"
        "+managed line\n"
    )


def test_render_diff_marks_replacement_new_side_without_final_newline():
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text="managed line",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="replace",
        before=b"managed line\n",
    )

    assert render_diff((change,)) == (
        "--- a/.github/workflows/doc-lattice.yml\n"
        "+++ b/.github/workflows/doc-lattice.yml\n"
        "@@ -1 +1 @@\n"
        "-managed line\n"
        "+managed line\n"
        "\\ No newline at end of file\n"
    )


def test_render_diff_marks_replacement_old_side_without_final_newline():
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text="managed line\n",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="replace",
        before=b"managed line",
    )

    assert render_diff((change,)) == (
        "--- a/.github/workflows/doc-lattice.yml\n"
        "+++ b/.github/workflows/doc-lattice.yml\n"
        "@@ -1 +1 @@\n"
        "-managed line\n"
        "\\ No newline at end of file\n"
        "+managed line\n"
    )


def test_render_diff_marks_created_file_without_final_newline():
    artifact = ManagedArtifact(
        role="offline",
        relative_path=PurePosixPath(".github/workflows/doc-lattice.yml"),
        text="managed line",
    )
    change = ArtifactChange(
        artifact=artifact,
        root=Path("/repo"),
        destination=Path("/repo/.github/workflows/doc-lattice.yml"),
        action="create",
        before=None,
    )

    assert render_diff((change,)) == (
        "--- /dev/null\n"
        "+++ b/.github/workflows/doc-lattice.yml\n"
        "@@ -0,0 +1 @@\n"
        "+managed line\n"
        "\\ No newline at end of file\n"
    )


def test_apply_create_refuses_atomic_publish_collision_without_overwrite(
    tmp_path: Path,
    monkeypatch,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    changes = preflight_create(tmp_path, artifacts)
    collision = tmp_path / artifacts[0].relative_path
    winner = b"concurrent user file\n"
    real_create = filesystem.atomic_create_bytes

    def _collide_during_publish(path: Path, data: bytes, *, prefix: str) -> None:
        path.write_bytes(winner)
        try:
            real_create(path, data, prefix=prefix)
        except FileExistsError as error:
            error.add_note("concurrent winner must remain untouched")
            raise

    monkeypatch.setattr(filesystem, "atomic_create_bytes", _collide_during_publish)

    with pytest.raises(
        ConfigError,
        match=r"appeared after preflight.*doc-lattice\.yml",
    ) as caught:
        apply_changes(changes)

    assert "concurrent winner must remain untouched" in getattr(caught.value, "__notes__", ())
    assert collision.read_bytes() == winner
    assert list(collision.parent.glob(f".{collision.name}.doc-lattice-create.*.tmp")) == []
    assert not (tmp_path / artifacts[1].relative_path).exists()
    assert not (tmp_path / artifacts[2].relative_path).exists()


def test_apply_replace_preserves_existing_mode(tmp_path: Path):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)
    bootstrap = tmp_path / old_artifacts[2].relative_path
    bootstrap.chmod(0o751)

    apply_changes(preflight_refresh(tmp_path, new_artifacts))

    assert stat.S_IMODE(bootstrap.stat().st_mode) == 0o751


def test_preflight_wraps_read_oserror_with_notes_and_canonical_path(
    tmp_path: Path,
    monkeypatch,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, artifacts)

    def _fail_open(_self: Path, *_args: object, **_kwargs: object):
        error = OSError("synthetic read failure")
        error.add_note("exact read remediation")
        raise error

    monkeypatch.setattr(Path, "open", _fail_open)

    with pytest.raises(ConfigError, match=r"synthetic read failure.*doc-lattice\.yml") as caught:
        preflight_create(tmp_path, artifacts)

    assert "exact read remediation" in getattr(caught.value, "__notes__", ())


@pytest.mark.parametrize("phase", ["resolve", "mkdir", "write"])
def test_apply_wraps_oserror_with_notes_and_canonical_path(
    tmp_path: Path,
    monkeypatch,
    phase: str,
):
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    changes = preflight_create(tmp_path, artifacts)

    def _failure() -> OSError:
        error = OSError(f"synthetic {phase} failure")
        error.add_note(f"exact {phase} remediation")
        return error

    if phase == "resolve":
        monkeypatch.setattr(
            filesystem, "safe_resolve", lambda *_args: (_ for _ in ()).throw(_failure())
        )
    elif phase == "mkdir":
        monkeypatch.setattr(
            Path, "mkdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(_failure())
        )
    else:
        monkeypatch.setattr(
            filesystem,
            "atomic_create_bytes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(_failure()),
        )

    with pytest.raises(
        ConfigError,
        match=rf"synthetic {phase} failure.*doc-lattice\.yml",
    ) as caught:
        apply_changes(changes)

    assert f"exact {phase} remediation" in getattr(caught.value, "__notes__", ())
    assert any("without rollback" in note for note in getattr(caught.value, "__notes__", ()))


def test_apply_replace_refuses_target_changed_after_preflight(tmp_path: Path):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)
    changes = preflight_refresh(tmp_path, new_artifacts)
    target = tmp_path / old_artifacts[0].relative_path
    target.write_bytes(b"post-preview user edit\n")

    with pytest.raises(ConfigError, match=r"changed after preflight.*doc-lattice\.yml"):
        apply_changes(changes)

    assert target.read_bytes() == b"post-preview user edit\n"
    assert (tmp_path / old_artifacts[1].relative_path).read_text(encoding="utf-8") == (
        old_artifacts[1].text
    )


def test_apply_replace_refuses_target_type_changed_after_preflight(tmp_path: Path):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)
    changes = preflight_refresh(tmp_path, new_artifacts)
    target = tmp_path / old_artifacts[0].relative_path
    target.unlink()
    target.mkdir()

    with pytest.raises(ConfigError, match=r"regular file.*doc-lattice\.yml"):
        apply_changes(changes)

    assert target.is_dir()


def test_apply_create_refuses_parent_containment_change_after_preflight(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    changes = preflight_create(root, artifacts)
    (root / ".github").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match=r"outside.*doc-lattice\.yml"):
        apply_changes(changes)

    assert list(outside.iterdir()) == []


def test_interrupted_replace_does_not_roll_back_and_rerun_converges(
    tmp_path: Path,
    monkeypatch,
):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    _write_artifacts(tmp_path, old_artifacts)
    changes = preflight_refresh(tmp_path, new_artifacts)
    real_replace = filesystem.atomic_replace_bytes
    calls = 0

    def _interrupt_after_one(path: Path, data: bytes, *, prefix: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic interruption")
        real_replace(path, data, prefix=prefix)

    with monkeypatch.context() as context:
        context.setattr(filesystem, "atomic_replace_bytes", _interrupt_after_one)
        with pytest.raises(ConfigError, match="synthetic interruption") as caught:
            apply_changes(changes)

    assert any("without rollback" in note for note in getattr(caught.value, "__notes__", ()))
    assert _artifact_bytes(tmp_path, new_artifacts) == [
        new_artifacts[0].text.encode("utf-8"),
        old_artifacts[1].text.encode("utf-8"),
        old_artifacts[2].text.encode("utf-8"),
    ]

    retry_changes = preflight_refresh(tmp_path, new_artifacts)
    assert [change.action for change in retry_changes] == ["current", "replace", "replace"]

    apply_changes(retry_changes)

    assert _artifact_bytes(tmp_path, new_artifacts) == [
        artifact.text.encode("utf-8") for artifact in new_artifacts
    ]


def test_interrupted_replace_note_and_prefix_follow_requested_input_order(
    tmp_path: Path,
    monkeypatch,
):
    old_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.0.0")
    new_artifacts = render_managed_artifacts("Guardantix/doc-lattice", "2.1.0")
    requested = (new_artifacts[2], new_artifacts[0], new_artifacts[1])
    _write_artifacts(tmp_path, old_artifacts)
    changes = preflight_refresh(tmp_path, requested)
    real_replace = filesystem.atomic_replace_bytes
    calls = 0

    def _interrupt_second_requested_change(path: Path, data: bytes, *, prefix: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic requested-order interruption")
        real_replace(path, data, prefix=prefix)

    monkeypatch.setattr(
        filesystem,
        "atomic_replace_bytes",
        _interrupt_second_requested_change,
    )

    with pytest.raises(ConfigError, match="synthetic requested-order interruption") as caught:
        apply_changes(changes)

    notes = getattr(caught.value, "__notes__", ())
    assert any("input order" in note for note in notes)
    assert all("canonical order" not in note for note in notes)
    assert _artifact_bytes(tmp_path, new_artifacts) == [
        old_artifacts[0].text.encode("utf-8"),
        old_artifacts[1].text.encode("utf-8"),
        new_artifacts[2].text.encode("utf-8"),
    ]
