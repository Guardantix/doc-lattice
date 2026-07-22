"""Compute and validate the successor helper's frozen semantic digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MANIFEST_PATH = Path("tests/fixtures/github_ci_successor_checkpoint/protocol/digest_manifest.json")
HELPER_PATH = Path("helper/doc-lattice-shell-parser")
EXPECTED_ORDERING = "path-lexicographic"
EXPECTED_DIGEST_DEFINITION = (
    "sha256 over newline-joined (path, file-sha256) pairs in path-lexicographic order"
)
EXPECTED_COMPLETENESS_RULE = (
    "CI recomputes the digest from this manifest and independently asserts that every non-test "
    ".go file under the helper module is covered by include minus exclude_globs; an uncovered "
    "compiled source fails the build."
)


class DigestManifestError(ValueError):
    """Report an invalid or incomplete helper digest manifest."""


@dataclass(frozen=True)
class DigestManifest:
    """Validated fields used from the frozen helper digest manifest."""

    include: tuple[str, ...]
    exclude_globs: tuple[str, ...]


def _string_tuple(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise DigestManifestError(f"manifest field {key!r} must be a non-empty string array")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DigestManifestError(f"manifest field {key!r} must be a non-empty string array")
        strings.append(item)
    return tuple(strings)


def _load_manifest(repo_root: Path) -> DigestManifest:
    manifest_path = repo_root / MANIFEST_PATH
    try:
        decoded: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DigestManifestError(f"cannot read {MANIFEST_PATH.as_posix()}: {error}") from error
    if not isinstance(decoded, dict):
        raise DigestManifestError("digest manifest must be a JSON object")
    if decoded.get("ordering") != EXPECTED_ORDERING:
        raise DigestManifestError(f"digest manifest must use {EXPECTED_ORDERING!r} ordering")
    if decoded.get("digest") != EXPECTED_DIGEST_DEFINITION:
        raise DigestManifestError("digest manifest has an unsupported digest definition")
    if decoded.get("completeness_rule") != EXPECTED_COMPLETENESS_RULE:
        raise DigestManifestError("digest manifest has an unsupported completeness_rule")
    return DigestManifest(
        include=_string_tuple(decoded.get("include"), "include"),
        exclude_globs=_string_tuple(decoded.get("exclude_globs"), "exclude_globs"),
    )


def _relative_manifest_path(value: str, *, field: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise DigestManifestError(f"{field} entry must be a contained relative path: {value!r}")
    return Path(*pure.parts)


def _included_files(repo_root: Path, entries: tuple[str, ...]) -> set[Path]:
    included: set[Path] = set()
    for entry in entries:
        relative = _relative_manifest_path(entry, field="include")
        candidate = repo_root / relative
        if candidate.is_file():
            included.add(relative)
        elif candidate.is_dir():
            included.update(
                path.relative_to(repo_root) for path in candidate.rglob("*") if path.is_file()
            )
        else:
            raise DigestManifestError(f"included path does not exist: {relative.as_posix()}")
    return included


def _excluded_files(repo_root: Path, patterns: tuple[str, ...]) -> set[Path]:
    excluded: set[Path] = set()
    for pattern in patterns:
        _relative_manifest_path(pattern, field="exclude_globs")
        excluded.update(
            path.relative_to(repo_root) for path in repo_root.glob(pattern) if path.is_file()
        )
    return excluded


def covered_paths(repo_root: Path) -> tuple[Path, ...]:
    """Return manifest-covered files in path-lexicographic order.

    Args:
        repo_root: Repository root containing the frozen digest manifest.

    Returns:
        Relative paths selected by include minus exclude_globs.
    """
    root = repo_root.resolve()
    manifest = _load_manifest(root)
    covered = _included_files(root, manifest.include) - _excluded_files(
        root, manifest.exclude_globs
    )
    return tuple(sorted(covered, key=lambda path: path.as_posix()))


def uncovered_go_sources(repo_root: Path) -> tuple[Path, ...]:
    """Return non-test helper Go sources omitted from the manifest-covered set.

    Args:
        repo_root: Repository root containing the helper module.

    Returns:
        Relative uncovered source paths in path-lexicographic order.
    """
    root = repo_root.resolve()
    helper = root / HELPER_PATH
    if not helper.is_dir():
        raise DigestManifestError(f"helper module does not exist: {HELPER_PATH.as_posix()}")
    covered = set(covered_paths(root))
    sources = {
        path.relative_to(root)
        for path in helper.rglob("*.go")
        if path.is_file() and not path.name.endswith("_test.go")
    }
    return tuple(sorted(sources - covered, key=lambda path: path.as_posix()))


def compute_digest(repo_root: Path) -> str:
    """Compute the frozen manifest digest for a repository tree.

    Each pair is encoded as the POSIX relative path, one NUL byte, and the lowercase
    file SHA-256. Pair encodings are joined by newlines with no trailing newline.

    Args:
        repo_root: Repository root containing all manifest inputs.

    Returns:
        The lowercase 64-hex semantic digest.
    """
    root = repo_root.resolve()
    pairs = (
        f"{path.as_posix()}\0{hashlib.sha256((root / path).read_bytes()).hexdigest()}"
        for path in covered_paths(root)
    )
    return hashlib.sha256("\n".join(pairs).encode()).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate completeness and print the deterministic helper digest.

    Args:
        argv: Optional command-line arguments for tests.

    Returns:
        Zero on success and two for an invalid or incomplete digest input tree.
    """
    arguments = _parser().parse_args(argv)
    try:
        uncovered = uncovered_go_sources(arguments.repo_root)
        if uncovered:
            paths = "\n".join(f"  {path.as_posix()}" for path in uncovered)
            raise DigestManifestError(f"uncovered non-test Go sources:\n{paths}")
        digest = compute_digest(arguments.repo_root)
    except (DigestManifestError, OSError) as error:
        print(f"helper digest: {error}", file=sys.stderr)
        return 2
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
