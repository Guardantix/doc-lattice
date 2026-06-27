"""Tests for error types."""

from game_lattice.error_types import ConfigError, ProjectError, ValidationError


def test_project_error_has_code():
    err = ProjectError("test", code="TEST")
    assert str(err) == "test"
    assert err.code == "TEST"


def test_config_error_inherits():
    err = ConfigError("bad config")
    assert isinstance(err, ProjectError)
    assert err.code == "CONFIG_ERROR"


def test_validation_error_inherits():
    err = ValidationError("bad input")
    assert isinstance(err, ProjectError)
    assert err.code == "VALIDATION_ERROR"
