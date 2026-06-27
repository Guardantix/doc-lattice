"""Shared test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Provide a clean working directory."""
    return tmp_path
