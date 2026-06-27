"""Tests for the io boundary."""

import pytest
from pydantic import ValidationError

from game_lattice.io_boundary import parse_settings


def test_parse_settings_valid():
    settings = parse_settings({"name": "demo", "retries": 5})
    assert settings.name == "demo"
    assert settings.retries == 5


def test_parse_settings_rejects_bad_type():
    with pytest.raises(ValidationError):
        parse_settings({"name": "demo", "retries": "many"})
