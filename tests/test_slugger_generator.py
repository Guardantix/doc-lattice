"""Tests for deterministic github-slugger compatibility data generation."""

import hashlib
import json
import subprocess
from pathlib import Path
from runpy import run_path

import pytest

from doc_lattice._github_slugger_data import (
    CHECKED_SLUG_OPERATIONS,
    CHECKED_UNICODE_SCALARS,
    GENERATED_NODE_VERSION,
    JAVASCRIPT_UNICODE_VERSION,
    LOWERCASE_PATCH_MAPPINGS,
    LOWERCASE_PATCH_TRANSLATION,
    PYTHON_BASELINE_UNICODE_VERSION,
    UPSTREAM_LOWERCASE_MAPPINGS,
    UPSTREAM_PACKAGE,
    UPSTREAM_REGEX_SHA256,
)
from doc_lattice.markdown_compat import SLUG_COMPAT_VERSION, SLUG_UNICODE_VERSION


def test_render_pattern_uses_python_unicode_escapes() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    render_pattern = generator["render_pattern"]

    assert render_pattern([(0, 1), (0x41, 0x41), (0x10000, 0x10001)]) == (
        r"[\u0000-\u0001\u0041\U00010000-\U00010001]"
    )


def test_render_module_includes_lowercase_data_and_wraps_for_lint() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    render_module = generator["render_module"]
    metadata_type = generator["ArtifactMetadata"]
    pattern = "[" + r"\u0000" * 50 + "]"

    rendered = render_module(
        pattern,
        [(0x0130, (0x0069, 0x0307)), (0xA7CB, (0x0264,))],
        metadata_type(
            version="2.0.0",
            regex_sha256="a" * 64,
            stripped_count=50,
            node_version="24.13.1",
            javascript_unicode="17.0",
            python_baseline_unicode="15.1.0",
            upstream_lowercase_count=1_488,
            slug_operation_count=1_112_067,
            cased_count=2,
            case_ignorable_count=1,
        ),
        cased_pattern=r"[\u0041\uA7CB]",
        case_ignorable_pattern=r"[\u0307]",
    )
    namespace: dict[str, object] = {}
    exec(rendered, namespace)  # noqa: S102 -- generated module behavior is the subject

    assert max(map(len, rendered.splitlines())) <= 100
    assert namespace["SLUG_STRIP_PATTERN"] == pattern
    assert namespace["GENERATED_NODE_VERSION"] == "24.13.1"
    assert namespace["JAVASCRIPT_UNICODE_VERSION"] == "17.0"
    assert namespace["PYTHON_BASELINE_UNICODE_VERSION"] == "15.1.0"
    assert namespace["LOWERCASE_PATCH_TRANSLATION"] == {
        0xA7CB: "\u0264",
        0x0130: "i\u0307",
    }
    assert namespace["LOWERCASE_PATCH_PATTERN"] == r"[\u0130\uA7CB]"
    assert namespace["CASED_PATTERN"] == r"[\u0041\uA7CB]"
    assert namespace["CASE_IGNORABLE_PATTERN"] == r"[\u0307]"
    assert namespace["CASED_UNICODE_SCALARS"] == 2
    assert namespace["CASE_IGNORABLE_UNICODE_SCALARS"] == 1
    assert namespace["UPSTREAM_LOWERCASE_MAPPINGS"] == 1_488
    assert namespace["CHECKED_SLUG_OPERATIONS"] == 1_112_067
    hash_line = next(line for line in rendered.splitlines() if '"' + "a" * 64 + '"' in line)
    assert hash_line.endswith("# pragma: allowlist secret")


def test_generated_provenance_matches_runtime_version_pins() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    assert (
        UPSTREAM_PACKAGE
        == SLUG_COMPAT_VERSION
        == (f"github-slugger@{generator['UPSTREAM_VERSION']}")
    )
    assert (
        JAVASCRIPT_UNICODE_VERSION
        == SLUG_UNICODE_VERSION
        == (generator["UPSTREAM_JAVASCRIPT_UNICODE"])
    )
    assert generator["PYTHON_BASELINE_UNICODE"] == PYTHON_BASELINE_UNICODE_VERSION
    assert len(LOWERCASE_PATCH_TRANSLATION) == LOWERCASE_PATCH_MAPPINGS
    assert UPSTREAM_LOWERCASE_MAPPINGS > LOWERCASE_PATCH_MAPPINGS
    assert CHECKED_SLUG_OPERATIONS == CHECKED_UNICODE_SCALARS + 6
    assert generator["UPSTREAM_NODE_VERSION"] == GENERATED_NODE_VERSION


def test_nvmrc_pins_the_exact_generating_node_version() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    nvmrc = (Path(__file__).parents[1] / ".nvmrc").read_text(encoding="utf-8").strip()

    # nvm resolves a partial version to the latest matching patch, which is not a pin. Require
    # all three components so the ICU table the artifact was generated against cannot drift.
    assert nvmrc == f"v{generator['UPSTREAM_NODE_VERSION']}"
    assert len(nvmrc.removeprefix("v").split(".")) == 3


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (range(CHECKED_UNICODE_SCALARS + 1), "exceeds the Unicode scalar set"),
        ([1, 0], "not unique and ordered"),
        ([-1], "outside the Unicode range"),
        ([0xD800], "contains a surrogate"),
    ],
)
def test_validate_unicode_property_values_rejects_invalid_data(
    values: object, message: str
) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    with pytest.raises(ValueError, match=message):
        generator["_validate_unicode_property_values"](values, property_name="cased")


def test_vendored_tarball_is_the_default_input_and_needs_no_network() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    vendored = generator["_VENDORED_TARBALL"]

    # The acceptance command passes no flags, so the default must already be the repository-owned
    # input. An implicit npm fallback would silently put the network back on that path.
    assert vendored == Path(__file__).parents[1] / "vendor" / "github-slugger-2.0.0.tgz"
    assert vendored.is_file()
    assert "_install_package" not in generator
    assert generator["verify_vendored_tarball"](vendored) == vendored.read_bytes()


def test_vendored_tarball_matches_the_pinned_digest_and_carries_the_pinned_version(
    tmp_path: Path,
) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    package_root = generator["_extract_vendored_package"](generator["_VENDORED_TARBALL"], tmp_path)

    package_data = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    assert package_data["version"] == generator["UPSTREAM_VERSION"]
    # The Node evaluator imports index.js, which imports regex.js, so both must be present for
    # the tarball digest to stand as the complete upstream-input identity.
    assert (package_root / "index.js").is_file()
    regex_bytes = (package_root / "regex.js").read_bytes()
    assert hashlib.sha256(regex_bytes).hexdigest() == UPSTREAM_REGEX_SHA256


def test_tarball_digest_mismatch_fails_before_extraction(tmp_path: Path) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    tampered = tmp_path / "github-slugger-2.0.0.tgz"
    tampered.write_bytes(generator["_VENDORED_TARBALL"].read_bytes() + b"tampered")
    working_dir = tmp_path / "work"
    working_dir.mkdir()

    with pytest.raises(ValueError, match="digest mismatch"):
        generator["_extract_vendored_package"](tampered, working_dir)

    # Verification precedes extraction, so unverified bytes never reach disk.
    assert list(working_dir.iterdir()) == []


def test_missing_vendored_tarball_is_reported_by_path(tmp_path: Path) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    absent = tmp_path / "github-slugger-2.0.0.tgz"

    with pytest.raises(FileNotFoundError, match="vendored upstream tarball is missing"):
        generator["verify_vendored_tarball"](absent)


@pytest.mark.parametrize(
    ("node_version", "unicode_version", "message"),
    [
        ("24.14.0", "17.0", r"expected Node 24\.13\.1"),
        ("25.0.0", "18.0", r"expected JavaScript Unicode 17\.0"),
    ],
    ids=["patch-drift-same-unicode", "icu-advanced"],
)
def test_generation_rejects_a_runtime_other_than_the_nvmrc_pin(
    node_version: str,
    unicode_version: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    package_root = generator["_extract_vendored_package"](generator["_VENDORED_TARBALL"], tmp_path)
    payload = json.dumps(
        {
            "node": node_version,
            "unicode": unicode_version,
            "stripped": [],
            "lowercase": [],
            "cased": [],
            "caseIgnorable": [],
            "slugOperations": CHECKED_UNICODE_SCALARS,
        }
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(generator["subprocess"], "run", fake_run)

    # The Unicode check alone is not enough: a Node whose ICU still reports 17.0 would otherwise
    # be accepted and its version rendered into the artifact, changing the bytes.
    with pytest.raises(ValueError, match=message):
        generator["_render_from_package"](package_root, "python3.13")


@pytest.mark.parametrize(
    ("code_points", "expected"),
    [
        ([], []),
        ([5], [(5, 5)]),
        ([1, 2, 3], [(1, 3)]),
        ([1, 2, 4, 5, 9], [(1, 2), (4, 5), (9, 9)]),
        ([0, 2, 4], [(0, 0), (2, 2), (4, 4)]),
    ],
    ids=["empty", "singleton", "adjacent", "split", "disjoint"],
)
def test_coalesce_groups_only_consecutive_code_points(
    code_points: list[int], expected: list[tuple[int, int]]
) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    assert generator["coalesce"](iter(code_points)) == expected


def test_derive_lowercase_patches_covers_added_and_transitive_mappings() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    # U+A7CB lowercases upstream but not in the baseline table: a straight added mapping.
    # U+1C89 lowercases to U+1C8A in the baseline and straight to U+0463 upstream, so the patch
    # has to be keyed on the intermediate the baseline produces, not on the original scalar.
    patches = generator["derive_lowercase_patches"](
        [(0x0130, (0x0069, 0x0307)), (0x1C89, (0x0463,)), (0x1C8A, (0x0463,)), (0xA7CB, (0x0264,))],
        [(0x0130, (0x0069, 0x0307)), (0x1C89, (0x1C8A,))],
    )

    assert patches == [(0x1C8A, (0x0463,)), (0xA7CB, (0x0264,))]


def test_derive_lowercase_patches_rejects_an_irreproducible_baseline() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    # The baseline lowercases U+0041 to U+0061, upstream to U+0062, and no scalar translation
    # applied after the baseline operation can bridge that: U+0061 has no upstream mapping.
    with pytest.raises(ValueError, match=r"cannot patch Python lowercase to upstream at U\+0041"):
        generator["derive_lowercase_patches"]([(0x0041, (0x0062,))], [(0x0041, (0x0061,))])
