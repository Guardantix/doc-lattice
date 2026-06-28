"""Custom exception types."""


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
    """Two lattice ids collide in the flat namespace."""

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
