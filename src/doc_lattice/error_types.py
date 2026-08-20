"""Custom exception types."""

from .constants import ErrorCode


def exception_details(error: BaseException) -> str:
    """Join an exception's message and its diagnostic notes into one string.

    Only the top-level pieces are joined, with ``"; "``. Line breaks inside the message or
    inside a note are preserved, so the result is one line only when every piece is: a
    multi-line diagnostic stays multi-line all the way to the terminal.

    Args:
        error: The exception to render.

    Returns:
        The exception's message, followed by each of its ``__notes__`` in order.
    """
    details = [str(error)]
    details.extend(str(note) for note in getattr(error, "__notes__", ()))
    return "; ".join(details)


def copy_exception_notes(target: BaseException, source: BaseException) -> None:
    """Copy diagnostic notes from a lower-level exception to its typed wrapper."""
    for note in getattr(source, "__notes__", ()):
        target.add_note(str(note))


class ProjectError(Exception):
    """Base exception for this project."""

    def __init__(self, message: str, code: ErrorCode = "UNKNOWN") -> None:
        super().__init__(message)
        self.code: ErrorCode = code


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


class FrontmatterError(ProjectError):
    """A doc's frontmatter fails schema validation or declares lattice intent with no id."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="FRONTMATTER_ERROR")


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


class InitPersistenceError(ProjectError):
    """The ``init`` scaffold cannot be written to the working directory."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INIT_PERSISTENCE")


class EscalatedWarningError(ProjectError):
    """A warning filter turned an advisory into an exception, ending the run.

    The engine never raises this: it is constructed at the command-line boundary from a
    ``Warning`` that reached it as an exception, which is what ``PYTHONWARNINGS=error`` and
    ``-W error`` make every warning do. It lives here rather than in the CLI package because
    every code in the printed domain belongs to a type in this module, and a library consumer
    matching on ``WARNING_AS_ERROR`` matches on this one.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="WARNING_AS_ERROR")


def escalated_warning_error(exc: Warning) -> EscalatedWarningError:
    """Restate a warning that a filter escalated to an exception as a coded project error.

    ``PYTHONWARNINGS=error`` and ``-W error`` raise the warning instance itself, and CPython
    does that before the replaceable ``showwarning`` stage, so AD-29's stderr renderer never
    sees one and cannot present it. Without this, the escalated advisory leaves the entry point
    as an unhandled traceback naming this package's own source and exits 1, the code ``check``
    reserves for drift. Restating it here is what puts it back on the ``error (CODE)`` contract
    the rest of the boundary prints.

    This lives beside the exception rather than in ``cli/errors.py``, where the rest of the
    boundary's rendering does, because ``cli/errors.py`` reaches ``cli/runtime.py`` and through
    it the whole engine and its dependencies. The entry point has to be able to report a warning
    escalated while importing exactly that chain, so the message it prints then cannot be built
    by it. This module imports only ``constants``, which imports only ``typing``.

    The category name leads the message even though ``CliRuntime._render_warning`` deliberately
    discards it for a displayed warning. The two diagnostics answer different questions: a
    displayed advisory is addressed to someone reading about their documents, while this one is
    addressed to someone who configured the filter that stopped the run, and the category is the
    handle that configuration is written against.

    Args:
        exc: The warning instance a filter raised in place of displaying it.

    Returns:
        The coded project error to render and exit on.
    """
    error = EscalatedWarningError(f"{type(exc).__name__}: {exception_details(exc).strip()}")
    error.add_note(
        "a warning filter escalated this advisory to an error, so the run stopped here instead "
        "of continuing past it"
    )
    return error
