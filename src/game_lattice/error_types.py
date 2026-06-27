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
