"""Tests for error types."""

from game_lattice.error_types import (
    BrokenRefError,
    ConfigError,
    DuplicateIdError,
    LinearError,
    ProjectError,
    UnreadableDocError,
    ValidationError,
)


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


def test_new_errors_extend_project_error():
    for exc in (DuplicateIdError("x"), BrokenRefError("x"), UnreadableDocError("x")):
        assert isinstance(exc, ProjectError)


def test_error_codes():
    assert DuplicateIdError("x").code == "DUPLICATE_ID"
    assert BrokenRefError("x").code == "BROKEN_REF"
    assert UnreadableDocError("x").code == "UNREADABLE_DOC"


def test_linear_error_inherits_and_has_code():
    err = LinearError("network down")
    assert isinstance(err, ProjectError)
    assert err.code == "LINEAR_ERROR"
    assert str(err) == "network down"
