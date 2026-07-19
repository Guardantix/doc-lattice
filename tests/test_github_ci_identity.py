"""Tests for GitHub repository identity and generator-version validation."""

import traceback

import pytest

from doc_lattice.error_types import ConfigError
from doc_lattice.github_ci.identity import (
    parse_origin_repository,
    parse_repository,
    validate_final_release_version,
)


@pytest.mark.parametrize(
    "value",
    [
        "Guardantix/doc-lattice",
        "guardantix/DOC-LATTICE",
        "a/a",
        "owner/repo.name_with-punctuation",
    ],
)
def test_parse_repository_accepts_ascii_github_identity(value):
    identity = parse_repository(value)

    assert identity.display == value
    assert identity.comparison_key == value.lower()


def test_parse_repository_accepts_maximum_length_repository_name():
    value = f"owner/{'r' * 100}"

    identity = parse_repository(value)

    assert identity.display == value
    assert identity.comparison_key == value


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Guardantix/doc-lattice.git",
        "git@GITHUB.COM:Guardantix/doc-lattice",
        "git@github.com:/Guardantix/doc-lattice.git",
        "ssh://git@github.com/Guardantix/doc-lattice.git",
        "https://github.com/Guardantix/doc-lattice",
        "https://GITHUB.COM/Guardantix/doc-lattice.git",
    ],
)
def test_parse_origin_repository_accepts_supported_github_urls(url):
    identity = parse_origin_repository(url)

    assert identity.display == "Guardantix/doc-lattice"
    assert identity.comparison_key == "guardantix/doc-lattice"


@pytest.mark.parametrize(
    "url",
    [
        " https://github.com/Guardantix/doc-lattice.git",
        "\tssh://git@github.com/Guardantix/doc-lattice.git",
        "https://github.com/Guardantix/doc-\nlattice.git",
        "ssh://git@github.com/Guardantix/doc-\rlattice.git",
        "https://github.com/Guardantix/doc-lattice.git\t",
        "ssh://git@github.com/Guardantix/doc-lattice.git\n",
        " git@github.com:Guardantix/doc-lattice.git",
        "git@github.com:Guardantix/doc-\nlattice.git",
        "git@github.com:Guardantix/doc-lattice.git\t",
    ],
)
def test_parse_origin_repository_rejects_raw_whitespace_and_ascii_controls(url):
    with pytest.raises(ConfigError):
        parse_origin_repository(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:DO-NOT-ECHO@github.com/a/a",  # pragma: allowlist secret
        "https://github.com/Guardantix/doc-lattice.git?token=DO-NOT-ECHO",
        "https://github.com/Guardantix/doc-lattice.git#DO-NOT-ECHO",
        "git@github.com:Guardantix/DO-NOT-ECHO!",
    ],
)
def test_parse_origin_repository_does_not_echo_sensitive_input(url):
    with pytest.raises(ConfigError) as exc_info:
        parse_origin_repository(url)

    assert "DO-NOT-ECHO" not in str(exc_info.value)


def test_parse_origin_repository_traceback_hides_invalid_scp_identity():
    url = "git@github.com:Guardantix/doc-lattice/DO-NOT-TRACE"

    with pytest.raises(ConfigError) as exc_info:
        parse_origin_repository(url)

    formatted = "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb))
    assert "DO-NOT-TRACE" not in formatted


def test_parse_origin_repository_traceback_hides_malformed_port_origin():
    url = "https://github.com:not-a-port/Guardantix/DO-NOT-TRACE"

    with pytest.raises(ConfigError) as exc_info:
        parse_origin_repository(url)

    formatted = "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb))
    assert "DO-NOT-TRACE" not in formatted


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "owner/repo/extra",
        "owner/repo?ref=main",
        "owner/repo#fragment",
        "owner/repo name",
        "owner/repo\nname",
        "-owner/repo",
        "owner-/repo",
        "owner/.",
        "owner/..",
        f"owner/{'r' * 101}",
    ],
)
def test_parse_repository_rejects_invalid_identity(value):
    with pytest.raises(ConfigError):
        parse_repository(value)


@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.com:Guardantix/doc-lattice.git",
        "git://github.com/Guardantix/doc-lattice.git",
        "http://github.com/Guardantix/doc-lattice.git",
        "ssh://root@github.com/Guardantix/doc-lattice.git",
        "https://user@github.com/Guardantix/doc-lattice.git",
        "https://github.com/Guardantix/doc-lattice/extra",
        "https://github.com/Guardantix/doc-lattice.git?ref=main",
        "https://github.com/Guardantix/doc-lattice.git#fragment",
        "https://github.com/Guardantix/doc-lattice.git?",
        "https://github.com/Guardantix/doc-lattice.git#",
        "ssh://git@github.com/Guardantix/doc-lattice.git?",
        "ssh://git@github.com/Guardantix/doc-lattice.git#",
        "https://github.com:443/Guardantix/doc-lattice.git",
        "ssh://git@github.com:22/Guardantix/doc-lattice.git",
        "https://github.com:not-a-port/Guardantix/doc-lattice.git",
        "ssh://git@github.com:not-a-port/Guardantix/doc-lattice.git",
        "https://github.com/Guardantix/",
        "ssh://git@github.com/Guardantix/",
        "git@github.com:Guardantix/",
        "git@github.com://Guardantix/doc-lattice.git",
        f"https://github.com/owner/{'r' * 101}.git",
    ],
)
def test_parse_origin_repository_rejects_unsupported_urls(url):
    with pytest.raises(ConfigError):
        parse_origin_repository(url)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.0", (0, 1, 0)),
        ("2.0.0", (2, 0, 0)),
        ("10.24.301", (10, 24, 301)),
    ],
)
def test_validate_final_release_version_accepts_three_numeric_components(version, expected):
    assert validate_final_release_version(version) == expected


def test_validate_final_release_version_rejects_oversized_numeric_component():
    version = f"{'9' * 5000}.0.0"

    with pytest.raises(ConfigError, match="final release version"):
        validate_final_release_version(version)


@pytest.mark.parametrize(
    "version",
    ["2.0", "2.0.0.dev1", "2.1.0rc1", "2.0.0+local", "v2.0.0", "latest"],
)
def test_validate_final_release_version_rejects_non_final_versions(version):
    with pytest.raises(ConfigError, match="final release version"):
        validate_final_release_version(version)
