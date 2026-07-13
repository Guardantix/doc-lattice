"""Tests for deterministic github-slugger compatibility data generation."""

from pathlib import Path
from runpy import run_path


def test_render_pattern_uses_python_unicode_escapes() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    render_pattern = generator["render_pattern"]

    assert render_pattern([(0, 1), (0x41, 0x41), (0x10000, 0x10001)]) == (
        r"[\u0000-\u0001\u0041\U00010000-\U00010001]"
    )


def test_render_module_wraps_generated_pattern_for_lint() -> None:
    generator = run_path(
        str(Path(__file__).parents[1] / "scripts" / "generate_github_slugger_data.py")
    )
    render_module = generator["render_module"]
    pattern = "[" + r"\u0000" * 50 + "]"

    rendered = render_module(pattern, "2.0.0", "a" * 64, 50)
    namespace: dict[str, object] = {}
    exec(rendered, namespace)  # noqa: S102 -- generated module behavior is the subject

    assert max(map(len, rendered.splitlines())) <= 100
    assert namespace["SLUG_STRIP_PATTERN"] == pattern
    hash_line = next(line for line in rendered.splitlines() if '"' + "a" * 64 + '"' in line)
    assert hash_line.endswith("# pragma: allowlist secret")
