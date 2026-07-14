"""Tests for the representative section derivation benchmark."""

from pathlib import Path
from runpy import run_path

import pytest

from doc_lattice.loader import derive_file_sections

_BENCHMARK = run_path(str(Path(__file__).parents[1] / "scripts" / "bench_sections.py"))
benchmark = _BENCHMARK["benchmark"]
build_document = _BENCHMARK["build_document"]
exceeds_regression = _BENCHMARK["exceeds_regression"]
regression_percent = _BENCHMARK["regression_percent"]


def test_build_document_has_requested_addressable_heading_count() -> None:
    body = build_document(100)
    sections = derive_file_sections(body)

    assert len(sections.sections) == 100
    assert "## Hidden fenced heading" in body
    assert all("hidden" not in section.anchor for section in sections.sections)
    assert "Привет" in body


def test_regression_threshold_is_strictly_greater_than_limit() -> None:
    assert regression_percent(100.0, 120.0) == pytest.approx(20.0)
    assert not exceeds_regression(100.0, 120.0, 20.0)
    assert exceeds_regression(100.0, 120.01, 20.0)


def test_benchmark_reports_samples_and_derived_heading_count() -> None:
    result = benchmark("## One {#one}\nbody\n", runs=2, warmups=0)

    assert result.heading_count == 1
    assert len(result.samples_ms) == 2
    assert result.median_ms > 0


def test_benchmark_counts_only_lf_as_line_break() -> None:
    result = benchmark("## One\nbody\vmore\n", runs=1, warmups=0)

    assert result.line_count == 2
