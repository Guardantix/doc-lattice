"""Tests for deterministic github-slugger compatibility data generation."""

from pathlib import Path
from runpy import run_path

import pytest

from doc_lattice._github_slugger_data import (
    CHECKED_SLUG_OPERATIONS,
    CHECKED_UNICODE_SCALARS,
    JAVASCRIPT_UNICODE_VERSION,
    LOWERCASE_PATCH_MAPPINGS,
    LOWERCASE_PATCH_TRANSLATION,
    PYTHON_BASELINE_UNICODE_VERSION,
    UPSTREAM_LOWERCASE_MAPPINGS,
    UPSTREAM_PACKAGE,
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


def test_install_package_reports_missing_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )

    def missing_npm(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("npm")

    monkeypatch.setattr(generator["subprocess"], "run", missing_npm)

    with pytest.raises(RuntimeError, match="npm executable not found"):
        generator["_install_package"](tmp_path)
