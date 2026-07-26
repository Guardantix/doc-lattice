#!/usr/bin/env python3
"""Publish the release target version and tag as GitHub Actions step outputs."""

import os
import sys
from pathlib import Path

from doc_lattice import __version__


def write_target(version: str, output_path: str) -> None:
    """Append the target version and tag to a GitHub Actions output file.

    Args:
        version: The declared distribution version.
        output_path: Path of the ``GITHUB_OUTPUT`` file to append to.
    """
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"version={version}\n")
        output.write(f"tag=v{version}\n")


def main() -> None:
    """Write the release target outputs and exit non-zero when unavailable."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        print("GITHUB_OUTPUT is not set", file=sys.stderr)
        sys.exit(1)
    write_target(__version__, output_path)


if __name__ == "__main__":
    main()
