"""Custom exception types."""


def exception_details(error: BaseException) -> str:
    """Flatten an exception message and its diagnostic notes into one line."""
    details = [str(error)]
    details.extend(str(note) for note in getattr(error, "__notes__", ()))
    return "; ".join(details)


def copy_exception_notes(target: BaseException, source: BaseException) -> None:
    """Copy diagnostic notes from a lower-level exception to its typed wrapper."""
    for note in getattr(source, "__notes__", ()):
        target.add_note(str(note))


class ProjectError(Exception):
    """Base exception for this project."""

    def __init__(self, message: str, code: str = "UNKNOWN") -> None:
        super().__init__(message)
        self.code = code


class ConfigError(ProjectError):
    """Configuration error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="CONFIG_ERROR")


class ValidationError(ProjectError):
    """Input validation error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="VALIDATION_ERROR")


class DuplicateIdError(ProjectError):
    """Two file ids collide, or two headings in one file resolve to the same anchor id."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="DUPLICATE_ID")


class BrokenRefError(ProjectError):
    """A derives_from ref resolves to no id in the index."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="BROKEN_REF")


class UnreadableDocError(ProjectError):
    """A doc cannot be read as UTF-8 or its YAML cannot be parsed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNREADABLE_DOC")


class LinearError(ProjectError):
    """A Linear network, credential, or response error."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="LINEAR_ERROR")


class ReconcileInProgressError(ProjectError):
    """A reconcile process already holds the project lock."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RECONCILE_IN_PROGRESS")


class ReconcileConflictError(ProjectError):
    """A destination changed after reconcile validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RECONCILE_CONFLICT")


class ReconcilePersistenceError(ProjectError):
    """A reconcile transaction cannot be persisted or safely recovered."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RECONCILE_PERSISTENCE")
