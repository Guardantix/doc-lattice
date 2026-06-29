"""Check that the package version agrees across its declared sources."""

import re
import tomllib

_VERSION_HEADING = re.compile(r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)


def _pyproject_version(pyproject_text: str) -> str | None:
    """Return the [project] version declared in pyproject text, or None if absent."""
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def _changelog_version(changelog_text: str) -> str | None:
    """Return the first versioned ``## [X.Y.Z]`` heading in changelog text, or None.

    A non-version heading such as ``## [Unreleased]`` does not match and is skipped,
    so the first real release heading is returned.
    """
    match = _VERSION_HEADING.search(changelog_text)
    return match.group("version") if match else None


def check_version_consistency(
    init_version: str, pyproject_text: str, changelog_text: str
) -> list[str]:
    """Return a message for each version source that disagrees with init_version.

    Args:
        init_version: The canonical package version, ``game_lattice.__version__``.
        pyproject_text: The full text of ``pyproject.toml``.
        changelog_text: The full text of ``CHANGELOG.md``.

    Returns:
        One message per disagreeing source, naming the file and the expected value.
        An empty list means every source matches ``init_version``. A source that
        cannot be parsed is reported as a mismatch rather than raising.
    """
    messages: list[str] = []
    pyproject_version = _pyproject_version(pyproject_text)
    if pyproject_version != init_version:
        messages.append(
            f"pyproject.toml version is {pyproject_version!r}, expected {init_version!r}; "
            f"set [project] version to match game_lattice.__version__."
        )
    changelog_version = _changelog_version(changelog_text)
    if changelog_version != init_version:
        messages.append(
            f"CHANGELOG.md top version heading is {changelog_version!r}, "
            f"expected {init_version!r}; add or fix the '## [{init_version}]' section."
        )
    return messages
