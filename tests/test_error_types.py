"""Tests for error types."""

from pathlib import Path

import pytest

from doc_lattice.constants import VALID_ERROR_CODES
from doc_lattice.error_types import (
    BrokenRefError,
    ConfigError,
    DocumentError,
    DuplicateIdError,
    FrontmatterError,
    InitPersistenceError,
    LinearError,
    ProjectError,
    UnreadableDocError,
    ValidationError,
)

_SOURCE = Path("docs/down.md")


def _build(factory: type[ProjectError], message: str) -> ProjectError:
    """Construct one error type without knowing which arguments its tier requires.

    A document-scoped type requires the document it is about, so the tree walk below cannot
    build every subclass the same way. Branching on the base rather than on a hand-listed set
    of names is what keeps a new document-scoped type covered the day it is added.
    """
    if issubclass(factory, DocumentError):
        return factory(message, source=_SOURCE)
    return factory(message)


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
    for exc in (
        DuplicateIdError("x"),
        BrokenRefError("x"),
        UnreadableDocError("x", source=_SOURCE),
    ):
        assert isinstance(exc, ProjectError)


def test_error_codes():
    assert DuplicateIdError("x").code == "DUPLICATE_ID"
    assert BrokenRefError("x").code == "BROKEN_REF"
    assert UnreadableDocError("x", source=_SOURCE).code == "UNREADABLE_DOC"


def test_frontmatter_error_inherits_and_has_its_own_code():
    # A frontmatter defect is not a config defect: sharing CONFIG_ERROR sent users to the
    # config file for a broken document.
    err = FrontmatterError(
        "frontmatter in a.md declares 'derives_from' but has no 'id' key", source=_SOURCE
    )
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
    """Every raisable ProjectError subclass declared in ``error_types``.

    The walk is recursive rather than one level deep, because ``DocumentError`` sits between
    ``ProjectError`` and its two document-scoped types, and a direct-subclass listing would drop
    both of them silently. An intermediate base is then excluded from the result: it declares a
    capability, and the code belongs to the concrete type raised at a site. Membership is
    restricted to this module so a subclass defined by a test cannot join the domain checks.
    """
    found: list[type[ProjectError]] = []
    pending: list[type[ProjectError]] = [ProjectError]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if subclass.__module__ != ProjectError.__module__ or subclass in found:
                continue
            found.append(subclass)
            pending.append(subclass)
    return sorted(
        (cls for cls in found if not any(other.__base__ is cls for other in found)),
        key=lambda cls: cls.__name__,
    )


def test_the_subclass_walk_reaches_past_an_intermediate_base():
    # Guards the walk itself: a direct-subclass listing returns DocumentError and neither of the
    # two types actually raised under it, which every domain check below would then pass on.
    subclasses = _project_error_subclasses()

    assert DocumentError not in subclasses
    assert {UnreadableDocError, FrontmatterError} <= set(subclasses)


@pytest.mark.parametrize("factory", _project_error_subclasses(), ids=lambda cls: cls.__name__)
def test_subclass_carries_message_and_a_declared_code(factory: type[ProjectError]):
    # Derived from the class tree rather than hand-listed: the previous list had drifted and
    # omitted the three reconcile types, so a new subclass shipped with nothing asserting on it.
    err = _build(factory, "file foo.md is bad; do the fix")

    assert isinstance(err, ProjectError)
    assert str(err) == "file foo.md is bad; do the fix"  # message reaches Exception base
    assert err.code != "UNKNOWN"  # UNKNOWN is the base default, never a subclass's own code
    assert err.code in VALID_ERROR_CODES


def test_every_subclass_code_is_unique():
    # The code is what a caller matches on, so two types sharing one would make the printed
    # diagnostic ambiguous and a migration note unwritable.
    codes = [_build(factory, "x").code for factory in _project_error_subclasses()]

    assert len(codes) == len(set(codes))


def test_the_declared_domain_has_no_members_without_an_error_type():
    # Keeps constants.py from accumulating codes nothing raises, which would read as supported.
    claimed = {_build(factory, "x").code for factory in _project_error_subclasses()}

    assert claimed == VALID_ERROR_CODES - {"UNKNOWN"}


def test_document_error_carries_its_subject_as_a_path():
    err = DocumentError("boom", source=_SOURCE, code="UNREADABLE_DOC")

    assert err.source == _SOURCE
    assert isinstance(err, ProjectError)


def test_document_scoped_types_extend_the_document_base():
    # The renderer branches on this base, not on a union of the two concrete types, so a third
    # document-scoped type is annotated without the error boundary being edited again.
    assert issubclass(UnreadableDocError, DocumentError)
    assert issubclass(FrontmatterError, DocumentError)


@pytest.mark.parametrize(
    "factory", [UnreadableDocError, FrontmatterError], ids=lambda cls: cls.__name__
)
def test_document_scoped_types_require_their_subject(factory: type[DocumentError]):
    # Required rather than optional on purpose: a None default would let a raise site omit the
    # path and silently reproduce the missing-annotation defect this base exists to close.
    with pytest.raises(TypeError):
        factory("boom")  # ty: ignore[missing-argument]


def test_document_error_keeps_the_spelling_it_was_given():
    # Discovery keeps each document's unresolved path as its identity and annotation-root
    # containment is lexical, so resolving here would change which base an annotation uses.
    relative = Path("docs/../docs/down.md")

    assert UnreadableDocError("boom", source=relative).source == relative
