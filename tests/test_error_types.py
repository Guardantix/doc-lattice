"""Tests for error types."""

import pytest

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


def test_project_error_default_code():
    err = ProjectError("boom")
    assert str(err) == "boom"
    assert err.code == "UNKNOWN"


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (ConfigError, "CONFIG_ERROR"),
        (ValidationError, "VALIDATION_ERROR"),
        (DuplicateIdError, "DUPLICATE_ID"),
        (BrokenRefError, "BROKEN_REF"),
        (UnreadableDocError, "UNREADABLE_DOC"),
        (LinearError, "LINEAR_ERROR"),
    ],
)
def test_subclass_carries_message_and_code(factory, code):
    err = factory("file foo.md is bad; do the fix")
    assert isinstance(err, ProjectError)
    assert err.code == code
    assert str(err) == "file foo.md is bad; do the fix"  # message reaches Exception base
