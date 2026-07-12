"""Check that the package version agrees across its declared sources."""

import re
import tomllib

_VERSION_HEADING = re.compile(r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)
_PINNED_REF = re.compile(r"doc-lattice(?:==|@v)(?P<version>\d+\.\d+\.\d+)")
_ANY_HEADING = re.compile(r"^##\s*\[", re.MULTILINE)


def changelog_section(changelog_text: str, version: str) -> str | None:
    """Return the body of the ``## [version]`` changelog section, or None if absent.

    The body is everything between that heading and the next ``## [`` heading (or the
    end of the document for the final section), trimmed of leading and trailing blank
    lines. Only ``## [`` lines bound a section, so a fenced code block whose content
    starts with ``## `` does not truncate the notes. A section that exists but has no
    content returns the empty string, so the caller can distinguish a missing heading
    (None) from an empty one ("").

    Args:
        changelog_text: The full text of ``CHANGELOG.md``.
        version: The ``X.Y.Z`` version whose section to extract.

    Returns:
        The trimmed section body, "" if the heading exists but is empty, or None if
        no ``## [version]`` heading is present.
    """
    heading = re.compile(r"^##\s*\[" + re.escape(version) + r"\].*$", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        return None
    following = _ANY_HEADING.search(changelog_text, match.end())
    end = following.start() if following else len(changelog_text)
    return changelog_text[match.end() : end].strip()


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


def _stale_pinned_refs(readme_text: str, init_version: str) -> list[str]:
    """Return the distinct pinned versions in readme_text that differ from init_version.

    Order follows first appearance in the text; duplicates of the same stale version
    are collapsed to a single entry.
    """
    stale: list[str] = []
    for match in _PINNED_REF.finditer(readme_text):
        version = match.group("version")
        if version != init_version and version not in stale:
            stale.append(version)
    return stale


def check_version_consistency(
    init_version: str, pyproject_text: str, changelog_text: str, readme_text: str
) -> list[str]:
    """Return a message for each version source that disagrees with init_version.

    Args:
        init_version: The canonical package version, ``doc_lattice.__version__``.
        pyproject_text: The full text of ``pyproject.toml``.
        changelog_text: The full text of ``CHANGELOG.md``.
        readme_text: The full text of ``README.md``.

    Returns:
        One message per disagreeing source, naming the file and the expected value.
        An empty list means every source matches ``init_version``. A source that
        cannot be parsed is reported as a mismatch rather than raising. Each distinct
        stale ``doc-lattice==X.Y.Z`` or ``doc-lattice@vX.Y.Z`` pin found in the
        README produces one message, no matter how many times that stale version
        occurs; a README with no pinned refs is consistent.
    """
    messages: list[str] = []
    pyproject_version = _pyproject_version(pyproject_text)
    if pyproject_version != init_version:
        messages.append(
            f"pyproject.toml version is {pyproject_version!r}, expected {init_version!r}; "
            f"set [project] version to match doc_lattice.__version__."
        )
    changelog_version = _changelog_version(changelog_text)
    if changelog_version != init_version:
        messages.append(
            f"CHANGELOG.md top version heading is {changelog_version!r}, "
            f"expected {init_version!r}; add or fix the '## [{init_version}]' section."
        )
    for stale_version in _stale_pinned_refs(readme_text, init_version):
        messages.append(
            f"README.md pins doc-lattice version {stale_version}, "
            f"expected {init_version}; update the pinned install refs."
        )
    return messages
