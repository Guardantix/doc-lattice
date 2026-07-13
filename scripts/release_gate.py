"""Decide whether a release workflow should create or reuse its target tag."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_VERSION_PATH = "src/doc_lattice/__init__.py"
_VERSION_ASSIGNMENT = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


class GateError(RuntimeError):
    """An invalid release state or unexpected Git failure."""


def _git(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(("git", *args), check=False, capture_output=True, text=True)
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return result


def _resolve_commit(ref: str) -> str:
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _source_at(ref: str) -> str | None:
    listing = _git("ls-tree", "--name-only", ref, "--", _VERSION_PATH)
    if not listing.stdout.strip():
        return None
    return _git("show", f"{ref}:{_VERSION_PATH}").stdout


def _version_at(ref: str, label: str, *, may_be_missing: bool = False) -> str | None:
    source = _source_at(ref)
    if source is None:
        if may_be_missing:
            return None
        raise GateError(f"{label} is missing {_VERSION_PATH}")
    matches = _VERSION_ASSIGNMENT.findall(source)
    if len(matches) != 1:
        raise GateError(f"{label} has a malformed version declaration in {_VERSION_PATH}")
    return matches[0]


def _write_decision(output_path: str, *, proceed: bool, create_tag: bool) -> None:
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"proceed={str(proceed).lower()}\n")
        output.write(f"create_tag={str(create_tag).lower()}\n")


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise GateError(f"required environment variable {name} is missing")
    return value


def main() -> int:
    try:
        tag = _required_environment("TAG")
        version = _required_environment("VERSION")
        github_sha = _required_environment("GITHUB_SHA")
        github_output = _required_environment("GITHUB_OUTPUT")

        current_sha = _resolve_commit(github_sha)
        current_version = _version_at(current_sha, "current source")
        if current_version != version:
            raise GateError(f"current source declares version {current_version!r}, not {version}")

        tag_ref = f"refs/tags/{tag}"
        tag_check = _git(
            "rev-parse", "--verify", "--quiet", f"{tag_ref}^{{commit}}", allow_failure=True
        )
        if tag_check.returncode == 0:
            tagged_sha = tag_check.stdout.strip()
            tagged_version = _version_at(tag_ref, "tagged source")
            if tagged_version != version:
                raise GateError(f"tag {tag} points at version {tagged_version!r}, not {version}")
            if tagged_sha == current_sha:
                print(f"Tag {tag} already identifies this commit; retrying release work.")
                _write_decision(github_output, proceed=True, create_tag=False)
            else:
                print(f"Tag {tag} already exists at version {version}; ordinary no-op.")
                _write_decision(github_output, proceed=False, create_tag=False)
            return 0
        if tag_check.returncode != 1:
            detail = tag_check.stderr.strip() or "unknown error"
            raise GateError(f"could not inspect tag {tag}: {detail}")

        before_sha = _resolve_commit(_required_environment("GITHUB_BEFORE"))
        before_version = _version_at(before_sha, "pre-push source", may_be_missing=True)
        if before_version == version:
            print(
                f"Tag {tag} is absent but the pre-push source already declares {version}; "
                "ordinary no-op."
            )
            _write_decision(github_output, proceed=False, create_tag=False)
        else:
            previous = before_version if before_version is not None else "no version"
            print(
                f"Tag {tag} is absent and the pre-push source declares {previous}; "
                "starting release work."
            )
            _write_decision(github_output, proceed=True, create_tag=True)
        return 0
    except (GateError, OSError) as error:
        print(f"::error::{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
