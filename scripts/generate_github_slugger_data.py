#!/usr/bin/env python3
"""Generate Python slug-strip data from pinned github-slugger behavior.

Node evaluates the exact upstream regular expression over every Unicode scalar value.
Python coalesces those results and deterministically renders the runtime artifact.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

UPSTREAM_VERSION = "2.0.0"
CHECKED_UNICODE_SCALARS = 1_112_064
_MAX_UNICODE = 0x10FFFF
_MAX_BMP = 0xFFFF
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _REPO_ROOT / "src" / "doc_lattice" / "_github_slugger_data.py"

_NODE_PROGRAM = r"""
import {pathToFileURL} from 'node:url'

const module = await import(pathToFileURL(process.argv[1]).href)
const regex = module.regex
const stripped = []
for (let codePoint = 0; codePoint <= 0x10FFFF; codePoint++) {
  if (codePoint >= 0xD800 && codePoint <= 0xDFFF) continue
  regex.lastIndex = 0
  if (regex.test(String.fromCodePoint(codePoint))) stripped.push(codePoint)
}
process.stdout.write(JSON.stringify(stripped))
"""


def coalesce(code_points: Iterable[int]) -> list[tuple[int, int]]:
    """Coalesce ordered code points into inclusive ranges.

    Args:
        code_points: Strictly increasing Unicode code points.

    Returns:
        Inclusive ``(start, end)`` ranges.
    """
    iterator = iter(code_points)
    try:
        start = previous = next(iterator)
    except StopIteration:
        return []
    ranges: list[tuple[int, int]] = []
    for code_point in iterator:
        if code_point == previous + 1:
            previous = code_point
            continue
        ranges.append((start, previous))
        start = previous = code_point
    ranges.append((start, previous))
    return ranges


def _escape_code_point(code_point: int) -> str:
    if code_point <= _MAX_BMP:
        return f"\\u{code_point:04X}"
    return f"\\U{code_point:08X}"


def render_pattern(ranges: Sequence[tuple[int, int]]) -> str:
    """Render inclusive ranges as one Python regular-expression character class.

    Args:
        ranges: Ordered inclusive Unicode ranges.

    Returns:
        A Python regular-expression pattern containing explicit Unicode escapes.
    """
    parts = ["["]
    for start, end in ranges:
        parts.append(_escape_code_point(start))
        if end != start:
            parts.extend(("-", _escape_code_point(end)))
    parts.append("]")
    return "".join(parts)


def render_module(pattern: str, version: str, regex_sha256: str, stripped_count: int) -> str:
    """Render the generated Python module.

    Args:
        pattern: Generated Python regular-expression pattern.
        version: Exact upstream github-slugger package version.
        regex_sha256: SHA-256 of the evaluated upstream ``regex.js``.
        stripped_count: Number of scalar values matched by the upstream regex.

    Returns:
        Complete deterministic Python module text.
    """
    chunks: list[str] = []
    offset = 0
    while offset < len(pattern):
        end = min(offset + 80, len(pattern))
        if pattern[end - 1] == "\\":
            end -= 1
        chunks.append(pattern[offset:end])
        offset = end
    pattern_lines = "".join(f'    r"{chunk}"\n' for chunk in chunks)
    return (
        '"""Generated strip data for github-slugger. Do not edit by hand."""\n\n'
        f'UPSTREAM_PACKAGE = "github-slugger@{version}"\n'
        "UPSTREAM_REGEX_SHA256 = (\n"
        f'    "{regex_sha256}"  # pragma: allowlist secret\n'
        ")\n"
        f"CHECKED_UNICODE_SCALARS = {CHECKED_UNICODE_SCALARS:_}\n"
        f"STRIPPED_UNICODE_SCALARS = {stripped_count:_}\n"
        f"SLUG_STRIP_PATTERN = (\n{pattern_lines})\n"
    )


def _evaluate_regex(regex_path: Path) -> list[int]:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", _NODE_PROGRAM, str(regex_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(result.stdout)
    if not isinstance(values, list) or not all(isinstance(value, int) for value in values):
        msg = "upstream regex evaluator returned a non-integer result"
        raise ValueError(msg)
    return values


def _render_from_package(package_root: Path) -> tuple[str, int, str]:
    package_data = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    version = package_data.get("version")
    if version != UPSTREAM_VERSION:
        msg = f"expected github-slugger@{UPSTREAM_VERSION}, found {version!r}"
        raise ValueError(msg)

    regex_path = package_root / "regex.js"
    regex_bytes = regex_path.read_bytes()
    regex_sha256 = hashlib.sha256(regex_bytes).hexdigest()
    stripped = _evaluate_regex(regex_path)
    if len(stripped) > CHECKED_UNICODE_SCALARS:
        msg = "upstream regex matched more values than the Unicode scalar set"
        raise ValueError(msg)
    if any(_SURROGATE_START <= value <= _SURROGATE_END for value in stripped):
        msg = "upstream evaluator unexpectedly returned a surrogate code point"
        raise ValueError(msg)
    if stripped and (stripped[0] < 0 or stripped[-1] > _MAX_UNICODE):
        msg = "upstream evaluator returned a value outside the Unicode range"
        raise ValueError(msg)

    pattern = render_pattern(coalesce(stripped))
    return render_module(pattern, version, regex_sha256, len(stripped)), len(stripped), regex_sha256


def _install_package(working_dir: Path) -> Path:
    subprocess.run(
        [
            "npm",
            "install",
            "--ignore-scripts",
            "--no-package-lock",
            "--no-save",
            f"github-slugger@{UPSTREAM_VERSION}",
        ],
        cwd=working_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return working_dir / "node_modules" / "github-slugger"


def _write_or_check(package_root: Path, output: Path, *, check: bool) -> tuple[int, str]:
    rendered, stripped_count, regex_sha256 = _render_from_package(package_root)
    if check:
        current = output.read_text(encoding="utf-8") if output.exists() else ""
        if current != rendered:
            print(f"generated slug data is stale: {output}")
            return 1, regex_sha256
        action = "verified"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        action = "wrote"
    print(
        f"{action} {output}: github-slugger@{UPSTREAM_VERSION}, "
        f"checked={CHECKED_UNICODE_SCALARS}, stripped={stripped_count}, "
        f"regex_sha256={regex_sha256}"
    )
    return 0, regex_sha256


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale")
    parser.add_argument(
        "--package-root",
        type=Path,
        help="existing github-slugger package directory; otherwise install the exact pin",
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    """Generate or verify the pinned slug compatibility artifact."""
    args = _parse_args()
    if args.package_root is not None:
        status, _ = _write_or_check(args.package_root, args.output, check=args.check)
        return status
    with tempfile.TemporaryDirectory(prefix="doc-lattice-slugger-") as tmp:
        package_root = _install_package(Path(tmp))
        status, _ = _write_or_check(package_root, args.output, check=args.check)
        return status


if __name__ == "__main__":
    raise SystemExit(main())
