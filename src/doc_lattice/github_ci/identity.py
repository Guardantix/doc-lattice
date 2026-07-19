"""GitHub.com identity and final-release pin validation."""

import re
from urllib.parse import urlsplit

from doc_lattice.error_types import ConfigError

from .model import RepositoryIdentity

_MAX_REPOSITORY_NAME_CHARACTERS = 100
_OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPOSITORY_PATTERN = rf"[A-Za-z0-9_.-]{{1,{_MAX_REPOSITORY_NAME_CHARACTERS}}}"
_IDENTITY_RE = re.compile(rf"{_OWNER_PATTERN}/{_REPOSITORY_PATTERN}", flags=re.ASCII)
_SCP_ORIGIN_RE = re.compile(r"git@(?i:github\.com):(?P<identity>.+)", flags=re.ASCII)
_UNSAFE_ORIGIN_CHARACTER_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_FINAL_RELEASE_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
    flags=re.ASCII,
)


def parse_repository(value: str) -> RepositoryIdentity:
    """Parse an exact GitHub.com owner/repository identity.

    Args:
        value: Repository identity in ``OWNER/REPO`` form.

    Returns:
        The identity with its original display spelling and normalized comparison key.

    Raises:
        ConfigError: If the value is not one unambiguous ASCII GitHub identity.
    """
    if _IDENTITY_RE.fullmatch(value) is None:
        msg = f"repository {value!r} must be one ASCII GitHub OWNER/REPO identity"
        raise ConfigError(msg)

    repository = value.split("/", 1)[1]
    if repository in {".", ".."}:
        msg = f"repository {value!r} is ambiguous; use one ASCII GitHub OWNER/REPO identity"
        raise ConfigError(msg)

    return RepositoryIdentity(display=value, comparison_key=value.lower())


def parse_origin_repository(url: str) -> RepositoryIdentity:
    """Parse a supported GitHub.com origin URL into its repository identity.

    Args:
        url: An SCP-style Git URL, ``ssh://`` URL, or ``https://`` URL.

    Returns:
        The validated owner/repository identity named by the origin.

    Raises:
        ConfigError: If the URL is not one of the supported exact GitHub.com forms.
    """
    if _UNSAFE_ORIGIN_CHARACTER_RE.search(url) is not None or "?" in url or "#" in url:
        raise ConfigError(_origin_error())

    scp_match = _SCP_ORIGIN_RE.fullmatch(url)
    if scp_match is not None:
        identity = _strip_git_suffix(scp_match.group("identity")).removeprefix("/")
        return _parse_origin_identity(identity)

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError(_origin_error()) from None

    if hostname is None or hostname.lower() != "github.com":
        raise ConfigError(_origin_error())
    if port is not None or ":" in parsed.netloc.rsplit("@", 1)[-1]:
        raise ConfigError(_origin_error())

    if parsed.scheme == "ssh":
        if parsed.username != "git" or parsed.password is not None:
            raise ConfigError(_origin_error())
    elif parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError(_origin_error())
    else:
        raise ConfigError(_origin_error())

    if not parsed.path.startswith("/"):
        raise ConfigError(_origin_error())
    identity = _strip_git_suffix(parsed.path[1:])
    return _parse_origin_identity(identity)


def _parse_origin_identity(value: str) -> RepositoryIdentity:
    """Validate an origin identity without exposing its raw value in errors."""
    try:
        return parse_repository(value)
    except ConfigError:
        raise ConfigError(_origin_error()) from None


def validate_final_release_version(version: str) -> tuple[int, int, int]:
    """Validate and split an exact three-component final release version.

    Args:
        version: Version pin to validate.

    Returns:
        The major, minor, and patch components as integers.

    Raises:
        ConfigError: If the version is not an exact final release such as ``2.0.0``.
    """
    match = _FINAL_RELEASE_RE.fullmatch(version)
    if match is None:
        msg = f"version {version!r} must be a final release version such as 2.0.0"
        raise ConfigError(msg)
    major, minor, patch = match.groups()
    try:
        return int(major), int(minor), int(patch)
    except ValueError as exc:
        msg = f"version {version!r} must be a final release version such as 2.0.0"
        raise ConfigError(msg) from exc


def _strip_git_suffix(value: str) -> str:
    """Strip at most one case-insensitive ``.git`` suffix."""
    return value[:-4] if value.lower().endswith(".git") else value


def _origin_error() -> str:
    """Build the configuration error message for an unsupported origin URL."""
    return (
        "origin URL must be a GitHub.com SCP, ssh://git@github.com, "
        "or https://github.com repository URL"
    )
