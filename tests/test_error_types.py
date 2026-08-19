"""Tests for error types."""

import pytest

from doc_lattice.constants import VALID_ERROR_CODES
from doc_lattice.error_types import (
    BrokenRefError,
    ConfigError,
    DuplicateIdError,
    FrontmatterError,
    InitPersistenceError,
    LinearError,
    ProjectError,
    UnreadableDocError,
    ValidationError,
)


def test_project_error_has_code():
    err = ProjectError("test", code="CONFIG_ERROR")
    assert str(err) == "test"
    assert err.code == "CONFIG_ERROR"


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


def test_frontmatter_error_inherits_and_has_its_own_code():
    # A frontmatter defect is not a config defect: sharing CONFIG_ERROR sent users to the
    # config file for a broken document.
    err = FrontmatterError("frontmatter in a.md declares 'derives_from' but has no 'id' key")
    assert isinstance(err, ProjectError)
    assert not isinstance(err, ConfigError)
    assert err.code == "FRONTMATTER_ERROR"


def test_init_persistence_error_inherits_and_has_its_own_code():
    # init's scaffold write is a filesystem boundary, not a config one: sharing CONFIG_ERROR
    # sent users to the config file for a read-only or permission-denied working directory.
    err = InitPersistenceError("cannot write .doc-lattice.yml: Read-only file system")
    assert isinstance(err, ProjectError)
    assert not isinstance(err, ConfigError)
    assert err.code == "INIT_PERSISTENCE"


def test_linear_error_inherits_and_has_code():
    err = LinearError("network down")
    assert isinstance(err, ProjectError)
    assert err.code == "LINEAR_ERROR"
    assert str(err) == "network down"


def test_project_error_default_code():
    err = ProjectError("boom")
    assert str(err) == "boom"
    assert err.code == "UNKNOWN"


def _project_error_subclasses() -> list[type[ProjectError]]:
    return sorted(ProjectError.__subclasses__(), key=lambda cls: cls.__name__)


@pytest.mark.parametrize("factory", _project_error_subclasses(), ids=lambda cls: cls.__name__)
def test_subclass_carries_message_and_a_declared_code(factory: type[ProjectError]):
    # Derived from the class tree rather than hand-listed: the previous list had drifted and
    # omitted the three reconcile types, so a new subclass shipped with nothing asserting on it.
    err = factory("file foo.md is bad; do the fix")

    assert isinstance(err, ProjectError)
    assert str(err) == "file foo.md is bad; do the fix"  # message reaches Exception base
    assert err.code != "UNKNOWN"  # UNKNOWN is the base default, never a subclass's own code
    assert err.code in VALID_ERROR_CODES


def test_every_subclass_code_is_unique():
    # The code is what a caller matches on, so two types sharing one would make the printed
    # diagnostic ambiguous and a migration note unwritable.
    codes = [factory("x").code for factory in _project_error_subclasses()]

    assert len(codes) == len(set(codes))


def test_the_declared_domain_has_no_members_without_an_error_type():
    # Keeps constants.py from accumulating codes nothing raises, which would read as supported.
    claimed = {factory("x").code for factory in _project_error_subclasses()}

    assert claimed == VALID_ERROR_CODES - {"UNKNOWN"}
