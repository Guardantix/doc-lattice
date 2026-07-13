"""Tests for the distributable package metadata and source contents."""

import re
import subprocess
import tarfile
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_NORMALIZED_NAME = re.sub(r"[-_.]+", "_", _PYPROJECT["project"]["name"])
_DIST_PREFIX = f"{_NORMALIZED_NAME}-{_PYPROJECT['project']['version']}"


def _assert_sdist_members(members, expected_prefix):
    expected_root_files = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}
    names = [member.name for member in members]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert duplicates == [], f"duplicate sdist members: {duplicates}"
    repository_only_tests = {
        f"{expected_prefix}/tests/test_release_gate.py",
        f"{expected_prefix}/tests/test_release_workflow.py",
    }
    assert repository_only_tests.isdisjoint(names), (
        f"repository-only tests included: {sorted(repository_only_tests.intersection(names))}"
    )

    root_files = set()
    unexpected_paths = []
    for member in members:
        assert member.isfile(), f"non-regular sdist member: {member.name!r}"
        path = PurePosixPath(member.name)
        assert not path.is_absolute(), f"absolute sdist member: {member.name!r}"

        parts = member.name.split("/")
        invalid_parts = [part for part in parts if part in {"", ".", ".."}]
        assert invalid_parts == [], (
            f"invalid path components in sdist member {member.name!r}: {invalid_parts}"
        )
        assert parts[0] == expected_prefix, (
            f"unexpected sdist prefix in {member.name!r}: expected {expected_prefix!r}"
        )

        relative_parts = parts[1:]
        assert relative_parts, f"sdist prefix is not a file: {member.name!r}"
        relative_path = PurePosixPath(*relative_parts).as_posix()
        if len(relative_parts) == 1:
            root_files.add(relative_path)
        elif relative_parts[0] not in {"src", "tests"}:
            unexpected_paths.append(relative_path)

    assert root_files == expected_root_files, (
        f"unexpected root files: {sorted(root_files - expected_root_files)}; "
        f"missing root files: {sorted(expected_root_files - root_files)}"
    )
    assert unexpected_paths == [], f"unexpected sdist members: {sorted(unexpected_paths)}"


def _valid_members(prefix=_DIST_PREFIX):
    relative_names = [
        ".gitignore",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "src/doc_lattice/__init__.py",
        "tests/test_package_metadata.py",
    ]
    return [tarfile.TarInfo(f"{prefix}/{name}") for name in relative_names]


def test_sdist_has_an_explicit_minimal_include_set():
    sdist = _PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist["include"] == [
        "/src",
        "/tests",
        "/LICENSE",
        "/README.md",
        "/pyproject.toml",
    ]
    assert sdist["exclude"] == [
        "/tests/test_release_gate.py",
        "/tests/test_release_workflow.py",
    ]


def test_build_backend_is_pinned_and_available_in_dev_environment():
    assert _PYPROJECT["build-system"]["requires"] == ["hatchling==1.31.0"]
    assert "hatchling==1.31.0" in _PYPROJECT["dependency-groups"]["dev"]


@pytest.mark.parametrize(
    "member_name",
    [
        f"{_DIST_PREFIX}/src/../workflow.yml",
        f"{_DIST_PREFIX}/src/./module.py",
        f"{_DIST_PREFIX}/src//module.py",
        f"/{_DIST_PREFIX}/src/module.py",
    ],
)
def test_sdist_validation_rejects_unsafe_path_components(member_name):
    members = [*_valid_members(), tarfile.TarInfo(member_name)]
    with pytest.raises(AssertionError):
        _assert_sdist_members(members, _DIST_PREFIX)


def test_sdist_validation_rejects_wrong_distribution_prefix():
    with pytest.raises(AssertionError):
        _assert_sdist_members(_valid_members("wrong-9.9.9"), _DIST_PREFIX)


def test_sdist_validation_rejects_duplicate_member():
    members = [*_valid_members(), tarfile.TarInfo(f"{_DIST_PREFIX}/README.md")]
    with pytest.raises(AssertionError):
        _assert_sdist_members(members, _DIST_PREFIX)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE])
def test_sdist_validation_rejects_non_regular_member(member_type):
    member = tarfile.TarInfo(f"{_DIST_PREFIX}/src/doc_lattice/current.py")
    member.type = member_type
    member.linkname = "__init__.py"
    with pytest.raises(AssertionError):
        _assert_sdist_members([*_valid_members(), member], _DIST_PREFIX)


def test_built_sdist_contains_only_publishable_source_files(tmp_path):
    output_dir = tmp_path / "dist"
    try:
        result = subprocess.run(  # noqa: S603 - fixed command and pytest-owned output path
            [  # noqa: S607 - uv is the repository-standard build frontend
                "uv",
                "build",
                "--sdist",
                "--no-build-isolation",
                "--out-dir",
                str(output_dir),
            ],
            cwd=_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            "uv build timed out after 60 seconds\n"
            f"stdout:\n{error.stdout or ''}\n"
            f"stderr:\n{error.stderr or ''}"
        )
    assert result.returncode == 0, (
        f"uv build failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    archives = sorted(output_dir.glob("*.tar.gz"))
    assert len(archives) == 1, f"expected one sdist, found: {archives}"

    with tarfile.open(archives[0], "r:gz") as archive:
        _assert_sdist_members(archive.getmembers(), _DIST_PREFIX)


def test_pypi_metadata_links_to_maintainer_resources():
    assert _PYPROJECT["project"]["urls"] == {
        "Homepage": "https://github.com/Guardantix/doc-lattice",
        "Source": "https://github.com/Guardantix/doc-lattice",
        "Issues": "https://github.com/Guardantix/doc-lattice/issues",
        "Changelog": "https://github.com/Guardantix/doc-lattice/blob/main/CHANGELOG.md",
        "Releases": "https://github.com/Guardantix/doc-lattice/releases",
    }
